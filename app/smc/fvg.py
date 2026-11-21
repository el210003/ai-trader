"""Fair Value Gaps (FVG / imbalance): a 3-candle pattern where candle 1's wick
and candle 3's wick leave a gap around candle 2's body. Unfilled gaps act as
magnet/reaction zones when price returns to them."""
from typing import List


def find_fvgs(df) -> list:
    highs = df["high"].values
    lows = df["low"].values
    times = df["time"].values
    n = len(df)

    fvgs: List[dict] = []
    for i in range(2, n):
        if lows[i] > highs[i - 2]:            # bullish imbalance (gap up)
            direction, top, bottom = "bullish", float(lows[i]), float(highs[i - 2])
        elif highs[i] < lows[i - 2]:          # bearish imbalance (gap down)
            direction, top, bottom = "bearish", float(lows[i - 2]), float(highs[i])
        else:
            continue

        # fully filled when price trades completely through the gap
        filled_index = None
        for k in range(i + 1, n):
            if direction == "bullish" and lows[k] <= bottom:
                filled_index = k
                break
            if direction == "bearish" and highs[k] >= top:
                filled_index = k
                break

        fvgs.append({
            "type": "bullish_fvg" if direction == "bullish" else "bearish_fvg",
            "direction": direction,
            "top": top, "bottom": bottom,
            "origin_index": int(i), "origin_time": int(times[i]),
            "filled_index": None if filled_index is None else int(filled_index),
            "filled": filled_index is not None,
        })

    return fvgs                       # untrimmed; callers apply fvg_view()


def fvg_view(fvgs: list, i: int, max_age_bars: int = 200) -> dict:
    """As-of-bar-i view of the untrimmed FVG list (i = n-1 reproduces the
    original trimmed output exactly)."""
    all_i = [f for f in fvgs if f["origin_index"] <= i][-14:]
    fresh = []
    for f in fvgs:
        if f["origin_index"] > i or i - f["origin_index"] > max_age_bars:
            continue
        if f["filled_index"] is not None and f["filled_index"] <= i:
            continue                      # already filled as of i
        if f["filled_index"] is not None:
            f = dict(f, filled_index=None, filled=False)   # fill happens after i
        fresh.append(f)
    return {"all": all_i, "fresh": fresh[-8:]}
