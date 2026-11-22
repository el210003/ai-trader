"""Market structure: swing points, Break of Structure (BOS) and Change of Character (CHoCH)."""
from typing import List, Optional, Tuple


def find_swings(df, lookback: int = 3) -> List[dict]:
    """Fractal pivots matching detect_pivots' flank-asymmetric tie-break:
    equality allowed against the OLDER flank (`[i-lookback, i-1]`), strict
    against the NEWER flank (`[i+1, i+lookback]`), so on equal highs/lows
    the newest touch keeps the swing reference. Confirmed at bar i+lookback."""
    lookback = max(1, int(lookback))
    highs = df["high"].values
    lows = df["low"].values
    times = df["time"].values
    n = len(df)
    swings: List[dict] = []
    for i in range(lookback, n - lookback):
        phv = float(highs[i])
        plv = float(lows[i])
        is_high = True
        is_low = True
        for k in range(1, lookback + 1):
            if highs[i - k] > phv or highs[i + k] >= phv:
                is_high = False
            if lows[i - k] < plv or lows[i + k] <= plv:
                is_low = False
            if not (is_high or is_low):
                break
        if is_high:
            swings.append({
                "type": "high", "index": i, "price": phv,
                "time": int(times[i]), "confirmed_at": i + lookback,
            })
        if is_low:
            swings.append({
                "type": "low", "index": i, "price": plv,
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
