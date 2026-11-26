"""Feature engineering: turn a setup + SMC context into a numeric vector.

11 setup-specific features + 4 per-symbol context features that let the
global model learn pair-specific quirks without per-symbol models.
"""
import math

import pandas as pd

FEATURES = [
    # ----- setup-specific (12) -----
    "rr",                  # risk:reward of the setup
    "atr_pct",             # volatility (ATR / price * 100)
    "atr_regime_ratio",    # current atr_pct / symbol_avg_atr_pct (volatility expansion)
    "range_position",      # entry position in the dealing range (0=bottom, 1=top)
    "zone_dist_atr",       # distance from current close to limit entry (ATR units)
    "sweep_depth_atr",     # sweep wick beyond the level (ATR units)
    "sweep_recency",       # 0 = sweep just happened, 1 = old/none
    "confluence_count",    # number of SMC confluences
    "zone_freshness",      # 1 = zone just formed, 0 = old
    "trend_alignment",     # 1 if structure trend matches direction
    "hour_sin",            # time-of-day encoding (session effects)
    "hour_cos",

    # ----- per-symbol context (3) -----
    "symbol_avg_atr_pct",      # mean volatility of this pair (near-constant per symbol)
    "symbol_recent_win_rate",  # rolling win rate over recent setups (None on cold-start)
    "symbol_avg_rr_realized",  # mean realized RR on this pair (None on cold-start)

    # ----- MTF & CSM confluence (5) — H1/H4 & currency strength context -----
    "htf_trend_align",         # mean(1 HTF trend matches direction, 0 opposed); 0.5 = no ctx
    "htf_pd_alignment",        # mean(1 entry in correct HTF premium/discount half)
    "htf_tp_distance_atr",     # entry -> nearest HTF liquidity pool (ATR units, cap 20)
    "rr_x_htf_align",          # interaction: target RR * HTF trend alignment
    "csm_aligned",             # +1 CSM aligns with direction, -1 opposed, 0 neutral/unknown

    # ----- rejection-bar family (RejectionProto port; neutral for zone setups) -----
    "is_rejection",            # 1 = rejection-bar setup family, 0 = OB/FVG zone setup
    "rej_exp",                 # expansion: body vs 20-bar avg body (x); 0 = not a rejection
    "rej_clv",                 # close location value % (50 = neutral)

    # ----- Phase B geometry, reachability & regime context (11) -----
    "is_limit_entry",          # 1 = limit/retrace order, 0 = close/market order
    "zone_age_ratio",          # POI age normalized by family max setup age [0, 1]
    "stop_dist_atr",           # |entry - stop_loss| in ATR units
    "tp_dist_atr",             # |take_profit - entry| in ATR units
    "tp_vs_dr",                # target travel / dealing-range span
    "tp_vs_htf_pool",          # HTF liquidity pool distance / target travel (cap 5)
    "fill_adv",                # (zone_dist_atr / tp_dist_atr) * (0.5 + in_correct_half)
    "avg_body_atr",            # 20-bar mean candle body / ATR (displacement efficiency)
    "dir_ema_slope",           # directional 10-bar EMA50 slope in ATR units
    "volatility_z",            # 20-bar normalized range std dev (volatility clustering)
    "fresh_dir_zones",         # count of fresh same-direction OB + FVG zones
]


def make_features(df, setup: dict, smc: dict, cfg: dict,
                  symbol_static: dict = None,
                  symbol_dynamic: dict = None,
                  htf_ctx: dict = None,
                  csm_dir: str = None) -> dict:
    """Build the full feature dict for one setup.

    Args:
        df: candle history (for ATR% and time-of-day)
        setup: the setup dict
        smc: the SMC context dict
        cfg: smc config
        symbol_static: optional SymbolStats.static_features() output
        symbol_dynamic: optional SymbolStats.dynamic_features() output
    """
    n = len(df)
    atr_v = smc["atr"] or 1e-9
    close = smc["last_close"]
    direction = setup["direction"]
    want_side = "sellside" if direction == "long" else "buyside"
    sweep_lb = int(cfg.get("sweep_lookback_bars", 30))
    max_age = max(1, int(cfg.get("ob_max_age_bars", 300)))

    aligned_sweeps = [s for s in smc["sweeps"] if s["side"] == want_side
                      and (n - 1) - s["index"] <= sweep_lb]

    depth = 0.0
    if aligned_sweeps:
        depths = []
        for s in aligned_sweeps:
            if s["side"] == "sellside":
                wick = float(s["level"] - df["low"].iat[s["index"]])
            else:
                wick = float(df["high"].iat[s["index"]] - s["level"])
            depths.append(max(wick, 0.0) / atr_v)
        depth = max(depths)

    recency = 1.0
    if aligned_sweeps:
        last = aligned_sweeps[-1]
        recency = min(1.0, (n - 1 - last["index"]) / max(1, sweep_lb))

    zone_age = n - 1 - setup["entry_zone"]["origin_index"]
    freshness = max(0.0, 1.0 - zone_age / max_age)

    ts = pd.to_datetime(df["time"].iat[-1], unit="s", utc=True)
    hour = ts.hour + ts.minute / 60.0

    # ---- MTF confluence metrics (computed by the setup builder, stored on the setup)
    htf = setup.get("htf_metrics") or {}
    trend_align = htf.get("trend_align")
    pd_align = htf.get("pd_alignment")

    # entry -> nearest HTF liquidity pool in the profit direction (ATR units)
    entry = float(setup["entry"])
    cap = 20.0
    tp_dist_atr = cap
    if htf_ctx:
        want_pools = "buyside" if direction == "long" else "sellside"
        pools = []
        for c in htf_ctx.values():
            pools.extend(c.get(want_pools, []))
        if direction == "long":
            beyond = [p for p in pools if p > entry]
            if beyond:
                tp_dist_atr = (min(beyond) - entry) / atr_v
        else:
            beyond = [p for p in pools if p < entry]
            if beyond:
                tp_dist_atr = (entry - max(beyond)) / atr_v

    # ---- rejection-bar metrics (None -> neutral priors for zone setups)
    rej = setup.get("rejection") or {}
    if rej:
        rej_exp = float(rej.get("exp") or 0.0)
        rej_clv = float(rej["clv"]) if rej.get("clv") is not None else 50.0
        rej_bucket = int(rej.get("bucket", 2))
    else:
        rej_exp, rej_clv, rej_bucket = 0.0, 50.0, 2

    rr_val = float(setup["rr"])
    atr_pct_val = float(atr_v / close * 100.0)
    sym_avg_atr = float((symbol_static or {}).get("symbol_avg_atr_pct", 0.0))
    atr_ratio = (atr_pct_val / sym_avg_atr) if sym_avg_atr > 1e-9 else 1.0
    htf_ta_val = float(trend_align) if trend_align is not None else 0.5

    # CSM alignment: csm_dir is "up" | "down" | None (or "long" | "short")
    csm_val = 0.0
    if csm_dir in ("up", "long"):
        csm_val = 1.0 if direction == "long" else -1.0
    elif csm_dir in ("down", "short"):
        csm_val = -1.0 if direction == "long" else 1.0
    else:
        for line in setup.get("confluences") or ():
            if "CSM aligned" in line:
                csm_val = 1.0
                break

    # ---- Phase B geometry, reachability & local regime features
    ez = setup.get("entry_zone") or {}
    ztype = ez.get("type", "")
    raw_style = (setup.get("entry_style") or "limit").strip().lower()
    is_limit_entry = 1.0 if raw_style in ("limit", "retrace") else 0.0
    if ztype == "order_block":
        max_z_age = max(1, int(cfg.get("ob_setup_max_age_bars", 150)))
    elif ztype == "fvg":
        max_z_age = max(1, int(cfg.get("fvg_setup_max_age_bars", 48)))
    else:
        max_z_age = max(1, int((cfg.get("rejection") or {}).get("trigger_lookback_bars", 2)))
    zone_age_ratio = min(1.0, max(0.0, zone_age / max_z_age))

    stop_dist_atr = abs(entry - float(setup["stop_loss"])) / atr_v
    tp_dist_atr_geom = abs(float(setup["take_profit"]) - entry) / atr_v
    dr = smc.get("dealing_range") or {}
    dr_top = dr.get("top")
    dr_bot = dr.get("bottom")
    dr_span_atr = ((float(dr_top) - float(dr_bot)) / atr_v
                   if (dr_top is not None and dr_bot is not None and float(dr_top) > float(dr_bot))
                   else 0.0)
    tp_vs_dr = tp_dist_atr_geom / max(dr_span_atr, 0.5)
    htf_tp_capped = float(min(cap, max(0.0, tp_dist_atr)))
    tp_vs_htf_pool = min(5.0, max(0.0, htf_tp_capped / max(tp_dist_atr_geom, 0.1)))

    zone_dist_val = float(setup.get("entry_distance_atr", abs(close - entry) / atr_v))
    rp_clamped = min(1.5, max(-0.5, float(setup["range_position"])))
    in_correct_half = 1.0 if ((direction == "long" and rp_clamped <= 0.5) or (direction == "short" and rp_clamped >= 0.5)) else 0.0
    fill_vs_tp = zone_dist_val / max(tp_dist_atr_geom, 0.1)
    fill_adv = fill_vs_tp * (0.5 + in_correct_half)

    bodies = (df["close"].iloc[-20:] - df["open"].iloc[-20:]).abs()
    avg_body_atr = float(bodies.mean()) / atr_v if len(bodies) else 0.0

    dir_sign = 1.0 if direction == "long" else -1.0
    if len(df) >= 11:
        ema50 = df["close"].ewm(span=50, adjust=False).mean()
        ema50_slope_atr = (float(ema50.iat[-1]) - float(ema50.iat[-11])) / atr_v
    else:
        ema50_slope_atr = 0.0
    dir_ema_slope = dir_sign * ema50_slope_atr

    rng20 = (df["high"].iloc[-20:] - df["low"].iloc[-20:]) / atr_v
    volatility_z = float(rng20.std()) if len(rng20) > 1 else 0.0

    want_zone_dir = "bullish" if direction == "long" else "bearish"
    fresh_dir_zones = float(
        sum(1 for z in (smc.get("order_blocks") or {}).get("fresh", []) if z.get("direction") == want_zone_dir)
        + sum(1 for z in (smc.get("fvgs") or {}).get("fresh", []) if z.get("direction") == want_zone_dir)
    )

    return {
        "rr": rr_val,
        "atr_pct": atr_pct_val,
        "atr_regime_ratio": float(min(5.0, max(0.0, atr_ratio))),
        "range_position": float(setup["range_position"]),
        "zone_dist_atr": zone_dist_val,
        "sweep_depth_atr": float(depth),
        "sweep_recency": float(recency),
        "confluence_count": float(len(setup["confluences"])),
        "zone_freshness": float(freshness),
        "trend_alignment": 1.0 if setup.get("trend_aligned") else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2 * math.pi * hour / 24.0),
        "symbol_avg_atr_pct": sym_avg_atr,
        "symbol_recent_win_rate": (symbol_dynamic or {}).get("symbol_recent_win_rate"),
        "symbol_avg_rr_realized": (symbol_dynamic or {}).get("symbol_avg_rr_realized"),
        "htf_trend_align": htf_ta_val,
        "htf_pd_alignment": float(pd_align) if pd_align is not None else 0.5,
        "htf_tp_distance_atr": htf_tp_capped,
        "rr_x_htf_align": float(rr_val * htf_ta_val),
        "csm_aligned": csm_val,
        "is_rejection": 1.0 if (setup.get("setup_kind") == "rejection" or bool(rej)) else 0.0,
        "rej_exp": rej_exp,
        "rej_clv": rej_clv,
        "is_limit_entry": float(is_limit_entry),
        "zone_age_ratio": float(zone_age_ratio),
        "stop_dist_atr": float(stop_dist_atr),
        "tp_dist_atr": float(tp_dist_atr_geom),
        "tp_vs_dr": float(tp_vs_dr),
        "tp_vs_htf_pool": float(tp_vs_htf_pool),
        "fill_adv": float(fill_adv),
        "avg_body_atr": float(avg_body_atr),
        "dir_ema_slope": float(dir_ema_slope),
        "volatility_z": float(volatility_z),
        "fresh_dir_zones": float(fresh_dir_zones),
    }


def feature_vector(feats: dict):
    """Numeric vector aligned with FEATURES. None (cold-start) is imputed
    with a neutral prior: 0.5 for win rate, 1.0 for realized RR."""
    out = []
    for f in FEATURES:
        v = feats.get(f)
        if v is None:
            if f == "symbol_recent_win_rate":
                out.append(0.5)
            elif f in ("symbol_avg_rr_realized", "atr_regime_ratio"):
                out.append(1.0)
            elif f in ("htf_trend_align", "htf_pd_alignment"):
                out.append(0.5)
            elif f == "htf_tp_distance_atr":
                out.append(20.0)
            elif f == "rej_clv":
                out.append(50.0)
            elif f in ("is_limit_entry", "stop_dist_atr", "tp_vs_htf_pool", "fresh_dir_zones"):
                out.append(1.0)
            elif f in ("zone_age_ratio", "tp_vs_dr", "avg_body_atr"):
                out.append(0.5)
            elif f == "tp_dist_atr":
                out.append(2.0)
            elif f == "fill_adv":
                out.append(0.25)
            elif f == "volatility_z":
                out.append(0.3)
            elif f == "rr_x_htf_align":
                rr_f = feats.get("rr")
                ta_f = feats.get("htf_trend_align")
                out.append(float(rr_f) * (float(ta_f) if ta_f is not None else 0.5) if rr_f is not None else 1.0)
            else:
                out.append(0.0)
        else:
            out.append(float(v))
    return out
