"""Liquidity: equal highs/lows pools (resting liquidity) and liquidity sweeps (stop hunts)."""
from typing import List

import numpy as np


def cluster_equal_levels(swings: List[dict], side: str, tol: float, min_touches: int = 2) -> List[dict]:
    """Cluster swing highs ('buyside' liquidity above) or swing lows ('sellside'
    liquidity below) whose prices sit within `tol` of each other into pools."""
    want = "high" if side == "buyside" else "low"
    levels = sorted((s for s in swings if s["type"] == want), key=lambda s: s["price"])
    pools: List[dict] = []
    used = [False] * len(levels)
    for i in range(len(levels)):
        if used[i]:
            continue
        group = [levels[i]]
        used[i] = True
        for j in range(i + 1, len(levels)):
            if used[j]:
                continue
            if abs(levels[j]["price"] - group[0]["price"]) <= tol:
                group.append(levels[j])
                used[j] = True
        if len(group) >= min_touches:
            pools.append({
                "side": side,
                "price": float(np.mean([g["price"] for g in group])),
                "touches": len(group),
                "last_time": int(max(g["time"] for g in group)),
                "members": [int(g["index"]) for g in group],
            })
    pools.sort(key=lambda p: p["price"])
    return pools


def detect_sweeps(df, swings: List[dict], pools: List[dict]) -> List[dict]:
    """Single pass over candles.

    A sweep happens when price *wicks* beyond a liquidity level but the candle
    CLOSES back on the original side (the stops got hit, price rejected):
      - high[i] > buyside level and close[i] < level  -> buy-side sweep  (bearish signal)
      - low[i]  < sellside level and close[i] > level -> sell-side sweep (bullish signal)

    Levels checked: the most recent confirmed swing high/low, plus equal-high/lows pools.
    A pool is 'consumed' once a candle closes through it.
    """
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["time"].values
    n = len(df)

    sweeps: List[dict] = []
    last_high = None
    last_low = None
    ptr = 0
    consumed = set()

    for i in range(n):
        while ptr < len(swings) and swings[ptr]["confirmed_at"] <= i:
            sw = swings[ptr]
            if sw["type"] == "high":
                last_high = sw
            else:
                last_low = sw
            ptr += 1

        # --- structural sweep of the most recent swing level
        if last_high is not None and highs[i] > last_high["price"] and closes[i] < last_high["price"]:
            sweeps.append({"side": "buyside", "kind": "swing_high", "level": last_high["price"],
                           "index": i, "time": int(times[i])})
        if last_low is not None and lows[i] < last_low["price"] and closes[i] > last_low["price"]:
            sweeps.append({"side": "sellside", "kind": "swing_low", "level": last_low["price"],
                           "index": i, "time": int(times[i])})

        # --- sweeps of equal highs/lows pools
        for pi, p in enumerate(pools):
            if pi in consumed or i <= p["last_time"]:
                continue
            if p["side"] == "buyside":
                if closes[i] > p["price"]:
                    consumed.add(pi)
                elif highs[i] > p["price"] and closes[i] < p["price"]:
                    sweeps.append({"side": "buyside", "kind": "equal_highs", "level": p["price"],
                                   "index": i, "time": int(times[i])})
            else:
                if closes[i] < p["price"]:
                    consumed.add(pi)
                elif lows[i] < p["price"] and closes[i] > p["price"]:
                    sweeps.append({"side": "sellside", "kind": "equal_lows", "level": p["price"],
                                   "index": i, "time": int(times[i])})

    # dedup identical (side, level, bar) events, keep chronological order
    seen = set()
    out = []
    for s in sweeps:
        key = (s["side"], round(s["level"], 8), s["index"])
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out
