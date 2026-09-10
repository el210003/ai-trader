"""Fair Value Gaps (FVG / imbalance): a 3-candle pattern where candle 1's wick
and candle 3's wick leave a gap around candle 2's body. Unfilled gaps act as
magnet/reaction zones when price returns to them."""
from typing import List


def find_fvgs(df, max_age_bars: int = 200) -> dict:
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

    fresh = [
        f for f in fvgs
        if not f["filled"] and (n - 1) - f["origin_index"] <= max_age_bars
    ]
    return {"all": fvgs[-14:], "fresh": fresh[-8:]}
