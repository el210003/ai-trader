"""Trade setup builder.

Combines SMC context into concrete setups:
  bias (structure) + liquidity sweep trigger + fresh OB/FVG entry zone
  located in the correct premium/discount half -> entry / SL / TP with RR.
"""
from typing import List, Optional


def _targets_above(levels: List[float], floor: float) -> List[float]:
    return sorted({p for p in levels if p > floor})


def _target_liquidity(direction: str, entry: float, close: float, risk: float,
                      smc: dict, min_rr: float, default_rr: float) -> tuple:
    """Nearest opposing liquidity pool / swing level beyond current price that
    offers at least min_rr. Falls back to default_rr multiple of risk."""
    if direction == "long":
        pool_levels = [p["price"] for p in smc["liquidity_pools"] if p["side"] == "buyside"]
        swing_levels = [s["price"] for s in smc["swings"] if s["type"] == "high"]
        floor = max(entry, close)
        cands = _targets_above(pool_levels + swing_levels, floor)
        for t in cands:
            rr = (t - entry) / risk
            if min_rr <= rr:
                return t, rr
        return entry + default_rr * risk, default_rr
    else:
        pool_levels = [p["price"] for p in smc["liquidity_pools"] if p["side"] == "sellside"]
        swing_levels = [s["price"] for s in smc["swings"] if s["type"] == "low"]
        ceil_ = min(entry, close)
        cands = sorted({p for p in pool_levels + swing_levels if p < ceil_}, reverse=True)
        for t in cands:
            rr = (entry - t) / risk
            if min_rr <= rr:
                return t, rr
        return entry - default_rr * risk, default_rr


def build_setups(symbol: str, tf: str, df, smc: dict, cfg: dict) -> List[dict]:
    close = smc["last_close"]
    atr_v = smc["atr"]
    rng = smc["dealing_range"]
    trend = smc["trend"]
    n = len(df)
    sweep_lb = int(cfg.get("sweep_lookback_bars", 30))
    min_rr = float(cfg.get("min_rr", 1.5))
    default_rr = float(cfg.get("default_rr", 2.0))
    max_rr = float(cfg.get("max_rr", 5.0))
    buffer_atr = float(cfg.get("sl_buffer_atr", 0.25))
    min_risk = float(cfg.get("min_risk_atr", 0.75)) * atr_v
    setups: List[dict] = []

    if atr_v <= 0:
        return setups

    for direction in ("long", "short"):
        conf: List[str] = []
        want_side = "sellside" if direction == "long" else "buyside"
        aligned = (direction == "long" and trend == "bullish") or \
                  (direction == "short" and trend == "bearish")

        recent = [s for s in smc["sweeps"]
                  if s["side"] == want_side and (n - 1) - s["index"] <= sweep_lb]

        if not aligned and not recent:
            continue
        if aligned:
            conf.append("market structure aligned")
        if recent:
            last = recent[-1]
            kind = last["kind"].replace("_", " ")
            conf.append(f"{last['side']} liquidity sweep ({kind}) {n - 1 - last['index']} bars ago")

        # ---- entry zone candidates: fresh order blocks / FVGs in the right direction
        want_zone_dir = "bullish" if direction == "long" else "bearish"
        zones = ([("order_block", ob) for ob in smc["order_blocks"]["fresh"] if ob["direction"] == want_zone_dir]
                 + [("fvg", fg) for fg in smc["fvgs"]["fresh"] if fg["direction"] == want_zone_dir])

        cands = []
        for ztype, z in zones:
            mid = (z["top"] + z["bottom"]) / 2.0
            if direction == "long":
                good_side = z["top"] <= close            # retest entry below price
                correct_half = mid <= rng["equilibrium"]  # discount entry
            else:
                good_side = z["bottom"] >= close
                correct_half = mid >= rng["equilibrium"]  # premium entry
            if not good_side:
                continue
            dist_atr = abs(close - mid) / atr_v
            if dist_atr > 12.0:                           # absurdly far zone
                continue
            score = (2 if correct_half else 0) + (1 if ztype == "order_block" else 0) \
                    + (1 if correct_half and ztype == "order_block" else 0)
            cands.append((score, -dist_atr, ztype, z, correct_half))

        if not cands:
            continue
        cands.sort(key=lambda t: (t[0], t[1]), reverse=True)
        _, _, ztype, z, correct_half = cands[0]

        if correct_half:
            conf.append(f"entry in {'discount' if direction == 'long' else 'premium'} zone")
        conf.append(f"fresh {ztype.replace('_', ' ')}")
        if len(zones) > 1:
            conf.append(f"{len(zones)} fresh zones stacked")

        # ---- entry / SL / TP
        if direction == "long":
            entry = min(z["top"], close)
            sweep_lows = [df["low"].iat[s["index"]] for s in recent]
            sl_base = min([z["bottom"]] + sweep_lows)
            sl = sl_base - buffer_atr * atr_v
            risk = entry - sl
            if risk < min_risk:          # volatility floor: never a razor-thin stop
                sl = entry - min_risk
                risk = min_risk
            if risk <= 0:
                continue
            tp, rr = _target_liquidity("long", entry, close, risk, smc, min_rr, default_rr)
        else:
            entry = max(z["bottom"], close)
            sweep_highs = [df["high"].iat[s["index"]] for s in recent]
            sl_base = max([z["top"]] + sweep_highs)
            sl = sl_base + buffer_atr * atr_v
            risk = sl - entry
            if risk < min_risk:
                sl = entry + min_risk
                risk = min_risk
            if risk <= 0:
                continue
            tp, rr = _target_liquidity("short", entry, close, risk, smc, min_rr, default_rr)

        if rr > max_rr:                  # cap fantasy RR (target too far to be meaningful)
            tp = entry + max_rr * risk if direction == "long" else entry - max_rr * risk
            rr = max_rr
        if rr < min_rr:
            continue

        range_span = rng["top"] - rng["bottom"]
        range_pos = (entry - rng["bottom"]) / range_span if range_span > 0 else 0.5

        setups.append({
            "symbol": symbol, "tf": tf, "direction": direction, "status": "active",
            "entry": float(entry), "stop_loss": float(sl), "take_profit": float(tp),
            "rr": round(float(rr), 2),
            "entry_zone": {"type": ztype, "top": float(z["top"]), "bottom": float(z["bottom"]),
                           "origin_time": int(z["origin_time"]), "origin_index": int(z["origin_index"])},
            "range_position": round(float(range_pos), 3),
            "trend_aligned": bool(aligned),
            "has_recent_sweep": bool(recent),
            "confluences": conf,
            "formed_index": n - 1,
            "formed_at": int(df["time"].iat[-1]),
            "last_close": close,
        })
    return setups
