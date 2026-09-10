"""Market structure: swing points, Break of Structure (BOS) and Change of Character (CHoCH)."""
from typing import List, Tuple


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
    """Walk candles chronologically from each swing's confirmation point.
    A candle CLOSE beyond the last swing high/low is a structural break:
      - break in trend direction      -> BOS (continuation)
      - break against trend direction -> CHoCH (potential reversal)
    Returns (events, current_trend)."""
    closes = df["close"].values
    times = df["time"].values
    n = len(df)
    events: List[dict] = []
    trend = "neutral"

    for k, sw in enumerate(swings):
        start = sw["confirmed_at"]
        end = swings[k + 1]["confirmed_at"] if k + 1 < len(swings) else n
        for i in range(start, end):
            if sw["type"] == "high" and closes[i] > sw["price"]:
                etype = "choch" if trend == "bearish" else "bos"
                events.append({
                    "type": etype, "direction": "bullish", "level": sw["price"],
                    "level_time": sw["time"], "index": i, "time": int(times[i]),
                })
                trend = "bullish"
                break
            if sw["type"] == "low" and closes[i] < sw["price"]:
                etype = "choch" if trend == "bullish" else "bos"
                events.append({
                    "type": etype, "direction": "bearish", "level": sw["price"],
                    "level_time": sw["time"], "index": i, "time": int(times[i]),
                })
                trend = "bearish"
                break
    return events, trend
