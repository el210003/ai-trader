"""Order blocks: the last opposite-direction candle before the impulsive leg
that breaks market structure. Fresh (unmitigated) OBs are entry candidates."""
from typing import List


def find_order_blocks(df, events: List[dict], max_age_bars: int = 300, impulse_walkback: int = 50) -> dict:
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["time"].values
    n = len(df)

    obs: List[dict] = []
    for ev in events:
        i = ev["index"]
        direction = ev["direction"]

        # walk back from the break candle to the last opposite-colour candle
        ob_idx = None
        for j in range(i - 1, max(i - impulse_walkback - 1, -1), -1):
            if direction == "bullish" and closes[j] < opens[j]:
                ob_idx = j
                break
            if direction == "bearish" and closes[j] > opens[j]:
                ob_idx = j
                break
        if ob_idx is None:
            continue

        top = float(highs[ob_idx])
        bottom = float(lows[ob_idx])

        # mitigation: price must first LEAVE the zone (impulse), then RETURN into it.
        # (Starting the scan at the break candle would flag every OB instantly, since the
        # impulse candle's wick usually still overlaps the zone.)
        mitigated_index = None
        left_at = None
        for k in range(i, n):
            if direction == "bullish" and lows[k] > top:
                left_at = k
                break
            if direction == "bearish" and highs[k] < bottom:
                left_at = k
                break
        if left_at is None:
            mitigated_index = i   # price never left the zone -> zone consumed immediately
        else:
            for k in range(left_at + 1, n):
                if direction == "bullish" and lows[k] <= top:
                    mitigated_index = k
                    break
                if direction == "bearish" and highs[k] >= bottom:
                    mitigated_index = k
                    break



        obs.append({
            "type": "bullish_ob" if direction == "bullish" else "bearish_ob",
            "direction": direction,
            "top": top, "bottom": bottom,
            "origin_index": int(ob_idx), "origin_time": int(times[ob_idx]),
            "event_type": ev["type"], "event_index": int(i), "event_time": ev["time"],
            "mitigated_index": None if mitigated_index is None else int(mitigated_index),
            "mitigated": mitigated_index is not None,
        })

    fresh = [
        ob for ob in obs
        if not ob["mitigated"] and (n - 1) - ob["origin_index"] <= max_age_bars
    ]
    return {"all": obs[-14:], "fresh": fresh[-8:]}
