"""SMC analysis orchestrator: runs all Smart Money Concepts detectors over a
candle DataFrame and returns one combined context dict."""
import pandas as pd

from .structure import find_swings, detect_structure
from .liquidity import cluster_equal_levels, detect_sweeps
from .pd_zones import compute_dealing_range
from .order_blocks import find_order_blocks, ob_view
from .fvg import find_fvgs, fvg_view
from .rejection import detect_rejections, detect_pivots


def atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def analyze(df: pd.DataFrame, cfg: dict) -> dict:
    """df must be ascending by time with columns time,open,high,low,close."""
    lookback = int(cfg.get("swing_lookback", 3))
    swings = find_swings(df, lookback)
    events, trend = detect_structure(df, swings)

    close = float(df["close"].iloc[-1])
    tol_pct = float(cfg.get("eq_tolerance_pct", 0.0006))
    tol = close * tol_pct
    confirmed_swings = [s for s in swings if s["confirmed_at"] < len(df)]
    pools = (cluster_equal_levels(confirmed_swings, "buyside", tol)
             + cluster_equal_levels(confirmed_swings, "sellside", tol))
    use_day_extremes = bool((cfg.get("rejection") or {}).get("use_day_extremes", True))
    sweeps = detect_sweeps(df, swings, pools, tol_pct=tol_pct, use_day_extremes=use_day_extremes)
    dealing_range = compute_dealing_range(df, swings, events=events)
    n = len(df)
    obs = ob_view(find_order_blocks(df, events, int(cfg.get("ob_max_age_bars", 300))),
                  n - 1, int(cfg.get("ob_max_age_bars", 300)))
    fvgs = fvg_view(find_fvgs(df), n - 1, int(cfg.get("fvg_max_age_bars", 200)))
    atr_val = atr(df, int(cfg.get("atr_period", 14)))
    rejections = detect_rejections(df, None, atr_val, cfg, tol_pct=tol_pct)
    swing_pivots = detect_pivots(df, lookback)   # EA IfPivotAdd book (chart dots)

    return {
        "trend": trend,
        "swings": swings[-60:],
        "events": events,
        "sweeps": sweeps,
        "rejections": rejections,
        "swing_pivots": swing_pivots,
        "liquidity_pools": pools,
        "dealing_range": dealing_range,
        "order_blocks": obs,
        "fvgs": fvgs,
        "atr": atr_val,
        "last_close": close,
        "n_bars": len(df),
    }
