"""SMC analysis orchestrator: runs all Smart Money Concepts detectors over a
candle DataFrame and returns one combined context dict."""
import pandas as pd

from .structure import find_swings, detect_structure
from .liquidity import cluster_equal_levels, detect_sweeps
from .pd_zones import compute_dealing_range
from .order_blocks import find_order_blocks
from .fvg import find_fvgs


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
    tol = close * float(cfg.get("eq_tolerance_pct", 0.0006))
    pools = (cluster_equal_levels(swings, "buyside", tol)
             + cluster_equal_levels(swings, "sellside", tol))
    sweeps = detect_sweeps(df, swings, pools)
    dealing_range = compute_dealing_range(df, swings)
    obs = find_order_blocks(df, events, int(cfg.get("ob_max_age_bars", 300)))
    fvgs = find_fvgs(df, int(cfg.get("fvg_max_age_bars", 200)))
    atr_val = atr(df, int(cfg.get("atr_period", 14)))

    return {
        "trend": trend,
        "swings": swings[-60:],
        "events": events,
        "sweeps": sweeps,
        "liquidity_pools": pools,
        "dealing_range": dealing_range,
        "order_blocks": obs,
        "fvgs": fvgs,
        "atr": atr_val,
        "last_close": close,
        "n_bars": len(df),
    }
