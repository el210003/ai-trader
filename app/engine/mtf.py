"""Multi-timeframe (MTF) confluence context.

For an entry timeframe (e.g. M15) every configured higher timeframe (H1, H4)
is analyzed with the same SMC engine and projected onto the entry chart:

  - HTF trend            -> bias confluence / `htf_trend_align` feature
  - HTF premium/discount -> `htf_pd_alignment` feature
  - HTF order blocks+FVGs -> `entry_in_htf_zone` feature
  - HTF liquidity pools  -> structural TP candidates + `htf_tp_distance_atr`

No look-ahead: an HTF bar is used only when its CLOSE happened at or before
the entry-TF bar that the setup forms on (`htf_open + htf_sec <= up_to_time`).
The same rule runs in live analysis and in the training replay.
"""
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30,
              "H1": 60, "H4": 240, "D1": 1440}


def higher_timeframes(tf: str, cfg: dict) -> list:
    """Configured timeframes strictly above `tf`, ascending (nearest first)."""
    mins = TF_MINUTES.get(tf)
    if mins is None:
        return []
    out = [t for t in cfg.get("timeframes", []) if TF_MINUTES.get(t, 0) > mins]
    return sorted(out, key=lambda t: TF_MINUTES[t])


def _closed_slice(df, tf: str, up_to_time: int):
    """HTF bars whose close time <= up_to_time (bar open + tf seconds)."""
    sec = TF_MINUTES.get(tf, 0) * 60
    if sec <= 0:
        return df
    return df[df["time"] + sec <= up_to_time].reset_index(drop=True)


def build_htf_context(symbol: str, tf: str, cfg: dict, up_to_time: int,
                      load_df, df_cache: dict = None,
                      ctx_cache: dict = None) -> dict:
    """Analyze every configured HTF for `symbol` using only bars closed by
    `up_to_time` (the entry-TF's last bar open time).

    load_df(symbol, htf) -> full HTF candle DataFrame (ascending).
    df_cache caches the raw HTF frames, ctx_cache the analyzed contexts
    (keyed by the last closed HTF bar, so replay steps inside the same HTF
    bar reuse the context).

    Returns {htf: {trend, atr, equilibrium, zones, buyside, sellside}}.
    """
    from ..smc import analyze as smc_analyze

    if not cfg.get("mtf", {}).get("enabled", True):
        return {}
    out = {}
    for htf in higher_timeframes(tf, cfg):
        if df_cache is not None and (symbol, htf) in df_cache:
            full = df_cache[(symbol, htf)]
        else:
            full = load_df(symbol, htf)
            if df_cache is not None:
                df_cache[(symbol, htf)] = full
        if full is None or len(full) < 60:
            continue
        sub = _closed_slice(full, htf, up_to_time)
        if len(sub) < 60:
            continue
        key = (symbol, htf, int(sub["time"].iat[-1]))
        if ctx_cache is not None and key in ctx_cache:
            out[htf] = ctx_cache[key]
            continue
        try:
            smc = smc_analyze(sub, cfg["smc"])
            rng = smc.get("dealing_range") or {}
            if not rng.get("equilibrium"):
                continue
            zones = []
            for ob in (smc["order_blocks"].get("fresh") or [])[:20]:
                zones.append({"type": "order_block", "direction": ob["direction"],
                              "top": float(ob["top"]), "bottom": float(ob["bottom"])})
            for fg in (smc["fvgs"].get("fresh") or [])[:20]:
                zones.append({"type": "fvg", "direction": fg["direction"],
                              "top": float(fg["top"]), "bottom": float(fg["bottom"])})
            pools = smc["liquidity_pools"]
            ctx = {
                "trend": smc["trend"],
                "atr": float(smc["atr"]),
                "equilibrium": float(rng["equilibrium"]),
                "zones": zones,
                "buyside": [float(p["price"]) for p in pools if p["side"] == "buyside"],
                "sellside": [float(p["price"]) for p in pools if p["side"] == "sellside"],
            }
        except Exception:
            continue
        out[htf] = ctx
        if ctx_cache is not None:
            ctx_cache[key] = ctx
    return out
