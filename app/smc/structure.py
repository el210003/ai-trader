"""Market structure: swing points, Break of Structure (BOS) and Change of Character (CHoCH)."""
from typing import List, Optional, Tuple


def find_swings(df, lookback: int = 3) -> List[dict]:
    """Fractal pivots. A swing high at bar i is the unique max of `high` within
    [i-lookback, i+lookback]; it is *confirmed* only at bar i+lookback."""
    highs = df["high"].values
    lows = df["low"].values
    times = df["time"].values
    n = len(df)
    swings: List[dict] = []
    for i in range(lookback, n - lookback):
        seg_h = highs[i - lookback:i + lookback + 1]
        seg_l = lows[i - lookback:i + lookback + 1]
        if highs[i] >= seg_h.max() and (seg_h == highs[i]).sum() == 1:
            swings.append({
                "type": "high", "index": i, "price": float(highs[i]),
                "time": int(times[i]), "confirmed_at": i + lookback,
            })
        if lows[i] <= seg_l.min() and (seg_l == lows[i]).sum() == 1:
            swings.append({
                "type": "low", "index": i, "price": float(lows[i]),
                "time": int(times[i]), "confirmed_at": i + lookback,
            })
    swings.sort(key=lambda s: s["confirmed_at"])
    return swings


def detect_structure(df, swings: List[dict]) -> Tuple[List[dict], str]:
    """Walk candles chronologically while tracking the active un-broken
    swing high and swing low independently.
    A candle CLOSE beyond the active swing high/low is a structural break:
      - break in trend direction      -> BOS (continuation)
      - break against trend direction -> CHoCH (potential reversal)
    Returns (events, current_trend)."""
    closes = df["close"].values
    times = df["time"].values
    n = len(df)
    events: List[dict] = []
    trend = "neutral"
    active_high: Optional[dict] = None
    active_low: Optional[dict] = None
    ptr = 0

    for i in range(n):
        while ptr < len(swings) and int(swings[ptr]["confirmed_at"]) <= i:
            sw = swings[ptr]
            if sw["type"] == "high":
                active_high = sw
            else:
                active_low = sw
            ptr += 1

        if active_high is not None and closes[i] > active_high["price"]:
            etype = "choch" if trend == "bearish" else "bos"
            events.append({
                "type": etype,
                "direction": "bullish",
                "level": float(active_high["price"]),
                "level_time": int(active_high["time"]),
                "level_index": int(active_high["index"]),
                "index": int(i),
                "time": int(times[i]),
            })
            trend = "bullish"
            active_high = None

        if active_low is not None and closes[i] < active_low["price"]:
            etype = "choch" if trend == "bullish" else "bos"
            events.append({
                "type": etype,
                "direction": "bearish",
                "level": float(active_low["price"]),
                "level_time": int(active_low["time"]),
                "level_index": int(active_low["index"]),
                "index": int(i),
                "time": int(times[i]),
            })
            trend = "bearish"
            active_low = None

    return events, trend
