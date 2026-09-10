"""Premium / Discount zones derived from the current dealing range."""
from typing import List, Optional


def compute_dealing_range(df, swings: List[dict]) -> dict:
    """Dealing range = bracket between the most recent confirmed swing high and
    the most recent confirmed swing low. Equilibrium is the 50% midpoint.

    Premium  = upper half of the range (sell territory)
    Discount = lower half of the range (buy territory)
    """
    last_high: Optional[dict] = None
    last_low: Optional[dict] = None
    for sw in swings:  # swings are sorted by confirmation time
        if sw["type"] == "high":
            last_high = sw
        else:
            last_low = sw

    close = float(df["close"].iloc[-1])

    if last_high is None or last_low is None:  # fallback: rolling extremes
        win = min(len(df), 200)
        seg = df.iloc[-win:]
        top, bottom = float(seg["high"].max()), float(seg["low"].min())
        hi_t, lo_t = int(seg["time"].iloc[-1]), int(seg["time"].iloc[-1])
        hi_i, lo_i = len(df) - 1, len(df) - 1
    else:
        top, bottom = last_high["price"], last_low["price"]
        hi_t, hi_i = last_high["time"], last_high["index"]
        lo_t, lo_i = last_low["time"], last_low["index"]
        if bottom > top:  # defensive: swap if structure is inverted
            top, bottom = bottom, top
            hi_t, hi_i, lo_t, lo_i = lo_t, lo_i, hi_t, hi_i

    eq = (top + bottom) / 2.0
    span = top - bottom
    position = (close - bottom) / span if span > 0 else 0.5
    zone = "premium" if position > 0.5 else ("discount" if position < 0.5 else "equilibrium")

    return {
        "top": top, "bottom": bottom, "equilibrium": eq,
        "high_time": int(hi_t), "low_time": int(lo_t),
        "high_index": int(hi_i), "low_index": int(lo_i),
        "position": float(min(max(position, 0.0), 1.0)),
        "zone": zone,
    }
