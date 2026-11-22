"""Premium / Discount zones derived from the current dealing range."""
from typing import List, Optional


def compute_dealing_range(
    df,
    swings: List[dict],
    events: Optional[List[dict]] = None,
    end_idx: Optional[int] = None,
) -> dict:
    """Dealing range anchored to the active impulse leg when structure events
    exist (origin swing -> running impulse extreme through `end_idx`), falling
    back to the most recent confirmed swing bracket (or 200-bar rolling extremes)
    when neutral. Equilibrium is the 50% midpoint.

    Premium  = upper half of the range (sell territory)
    Discount = lower half of the range (buy territory)
    """
    n = len(df)
    tip = (n - 1) if end_idx is None else min(max(int(end_idx), 0), n - 1)
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["time"].values
    close = float(closes[tip])

    vis_swings = [sw for sw in swings if int(sw["confirmed_at"]) <= tip]
    vis_events = [ev for ev in (events or []) if int(ev["index"]) <= tip]

    if vis_events:
        ev = vis_events[-1]
        ev_idx = int(ev["index"])
        prev_idx = int(vis_events[-2]["index"]) if len(vis_events) >= 2 else 0
        lvl_idx = int(ev.get("level_index", prev_idx))
        win_start = min(prev_idx, lvl_idx)

        if ev["direction"] == "bullish":
            cand_lows = [
                sw for sw in vis_swings
                if sw["type"] == "low" and win_start <= int(sw["index"]) <= ev_idx
            ]
            if cand_lows:
                origin = min(cand_lows, key=lambda s: float(s["price"]))
                bottom = float(origin["price"])
                lo_i = int(origin["index"])
                lo_t = int(origin["time"])
            else:
                seg_start = max(0, min(lvl_idx, ev_idx))
                rel_lo = int(lows[seg_start:ev_idx + 1].argmin())
                lo_i = seg_start + rel_lo
                bottom = float(lows[lo_i])
                lo_t = int(times[lo_i])

            rel_hi = int(highs[lo_i:tip + 1].argmax())
            hi_i = lo_i + rel_hi
            top = float(highs[hi_i])
            hi_t = int(times[hi_i])
        else:
            cand_highs = [
                sw for sw in vis_swings
                if sw["type"] == "high" and win_start <= int(sw["index"]) <= ev_idx
            ]
            if cand_highs:
                origin = max(cand_highs, key=lambda s: float(s["price"]))
                top = float(origin["price"])
                hi_i = int(origin["index"])
                hi_t = int(origin["time"])
            else:
                seg_start = max(0, min(lvl_idx, ev_idx))
                rel_hi = int(highs[seg_start:ev_idx + 1].argmax())
                hi_i = seg_start + rel_hi
                top = float(highs[hi_i])
                hi_t = int(times[hi_i])

            rel_lo = int(lows[hi_i:tip + 1].argmin())
            lo_i = hi_i + rel_lo
            bottom = float(lows[lo_i])
            lo_t = int(times[lo_i])

        if bottom > top:
            top, bottom = bottom, top
            hi_t, hi_i, lo_t, lo_i = lo_t, lo_i, hi_t, hi_i
    else:
        last_high: Optional[dict] = None
        last_low: Optional[dict] = None
        for sw in vis_swings:
            if sw["type"] == "high":
                last_high = sw
            else:
                last_low = sw

        if last_high is None or last_low is None:  # fallback: rolling extremes
            win = min(tip + 1, 200)
            start = tip + 1 - win
            top = float(highs[start:tip + 1].max())
            bottom = float(lows[start:tip + 1].min())
            hi_t = lo_t = int(times[tip])
            hi_i = lo_i = tip
        else:
            top, bottom = float(last_high["price"]), float(last_low["price"])
            hi_t, hi_i = int(last_high["time"]), int(last_high["index"])
            lo_t, lo_i = int(last_low["time"]), int(last_low["index"])
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
