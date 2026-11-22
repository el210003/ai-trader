"""Liquidity: equal highs/lows pools (resting liquidity) and liquidity sweeps (stop hunts)."""
import bisect
from typing import List, Optional

import numpy as np

_BIG = float("inf")


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
            elif levels[j]["price"] - group[0]["price"] > tol:
                break
        if len(group) >= min_touches:
            confirmed_vals = [int(g.get("confirmed_at", g["index"])) for g in group]
            pools.append({
                "side": side,
                "price": float(np.mean([g["price"] for g in group])),
                "touches": len(group),
                "last_time": int(max(g["time"] for g in group)),
                "last_index": int(max(g["index"] for g in group)),
                "last_confirmed_at": int(max(confirmed_vals)),
                "members": [int(g["index"]) for g in group],
            })
    pools.sort(key=lambda p: p["price"])
    return pools


def pool_ready_bar(pool: dict) -> int:
    """Earliest bar index at which `pool` is confirmed and eligible to be swept."""
    if "last_confirmed_at" in pool:
        return int(pool["last_confirmed_at"])
    if "last_index" in pool:
        return int(pool["last_index"])
    lt = int(pool.get("last_time", 0))
    return lt if lt < 10_000_000 else 0


def active_pools_at(confirmed_swings: List[dict], tol: float) -> List[dict]:
    return (
        cluster_equal_levels(confirmed_swings, "buyside", tol)
        + cluster_equal_levels(confirmed_swings, "sellside", tol)
    )


def detect_sweeps(
    df,
    swings: List[dict],
    pools: Optional[List[dict]] = None,
    *,
    tol_pct: Optional[float] = None,
    use_day_extremes: bool = False,
) -> List[dict]:
    """Single chronological pass over closed candles (ProcessBar no-hindsight order).

    A sweep happens when price *wicks* beyond a live liquidity level but the
    candle CLOSES back on the original side (stops triggered, price rejected):
      - high[i] > buyside level  and close[i] < level -> buyside sweep  (bearish signal)
      - low[i]  < sellside level and close[i] > level -> sellside sweep (bullish signal)

    Live level books (as of bar i-1's close):
      1. Every confirmed, un-broken, un-swept swing high/low (`live_highs`, `live_lows`).
         A candle body-close through a swing level breaks it; a wick-and-retreat
         sweeps and consumes it so it does not re-fire on subsequent bars.
      2. Equal-highs / equal-lows pools (`equal_highs`, `equal_lows`) once all
         member swings are confirmed (`i >= pool['last_confirmed_at']`).
         When `tol_pct` is given, pools are clustered causally from swings
         confirmed before bar `i` using `tol = close[i-1] * tol_pct` so
         `detect_sweeps(df[:k+1], ...)` matches the prefix of `detect_sweeps(df, ...)`.
      3. Running server-day high/low (`day_high`, `day_low`) when `use_day_extremes=True`,
         folded in AFTER bar `i` is evaluated and reset at server midnight.
    """
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    times = df["time"].values
    n = len(df)
    if n == 0:
        return []

    sweeps: List[dict] = []
    live_highs: List[list] = []   # price-sorted [price, seq]
    live_lows: List[list] = []    # price-sorted [price, seq]
    next_seq = 0

    confirmed_swings: List[dict] = []
    ptr = 0

    # Static pool mode (when tol_pct is None and caller supplies `pools`)
    static_pools = list(pools or [])
    consumed_static = set()
    # Causal pool mode (when tol_pct is provided): track consumed member sets
    consumed_causal = set()

    day_stamp = int(times[0]) // 86400
    day_high = float(highs[0])
    day_low = float(lows[0])
    day_has = True

    # Bar 0 seeds books/swings (no prior bar to sweep against)
    while ptr < len(swings) and int(swings[ptr]["confirmed_at"]) <= 0:
        sw = swings[ptr]
        confirmed_swings.append(sw)
        if sw["type"] == "high":
            bisect.insort(live_highs, [float(sw["price"]), next_seq])
        else:
            bisect.insort(live_lows, [float(sw["price"]), next_seq])
        next_seq += 1
        ptr += 1

    for i in range(1, n):
        hi = float(highs[i])
        lo = float(lows[i])
        cl = float(closes[i])
        t_i = int(times[i])
        bar_sweeps: List[dict] = []

        # ---- Step 1: evaluate bar i against books as of bar i-1's close
        # 1a. Live swing highs / lows (wick pierced, close retreated)
        k0 = bisect.bisect_right(live_highs, [cl, _BIG])
        k1 = bisect.bisect_left(live_highs, [hi, -1.0])
        if k1 > k0:
            for lvl, _seq in live_highs[k0:k1]:
                bar_sweeps.append({
                    "side": "buyside", "kind": "swing_high", "level": float(lvl),
                    "index": i, "time": t_i,
                })
            del live_highs[k0:k1]  # swept -> consumed

        k0 = bisect.bisect_right(live_lows, [lo, _BIG])
        k1 = bisect.bisect_left(live_lows, [cl, -1.0])
        if k1 > k0:
            for lvl, _seq in live_lows[k0:k1]:
                bar_sweeps.append({
                    "side": "sellside", "kind": "swing_low", "level": float(lvl),
                    "index": i, "time": t_i,
                })
            del live_lows[k0:k1]   # swept -> consumed

        # 1b. Equal-highs / equal-lows liquidity pools
        if tol_pct is not None:
            tol_i = float(closes[i - 1]) * float(tol_pct)
            causal_pools = active_pools_at(confirmed_swings, tol_i)
            for p in causal_pools:
                pkey = (p["side"], tuple(p["members"]))
                if pkey in consumed_causal or i < pool_ready_bar(p):
                    continue
                lvl = float(p["price"])
                if p["side"] == "buyside":
                    if cl > lvl:
                        consumed_causal.add(pkey)
                    elif hi > lvl and cl < lvl:
                        consumed_causal.add(pkey)
                        bar_sweeps.append({
                            "side": "buyside", "kind": "equal_highs", "level": lvl,
                            "index": i, "time": t_i,
                        })
                else:
                    if cl < lvl:
                        consumed_causal.add(pkey)
                    elif lo < lvl and cl > lvl:
                        consumed_causal.add(pkey)
                        bar_sweeps.append({
                            "side": "sellside", "kind": "equal_lows", "level": lvl,
                            "index": i, "time": t_i,
                        })
        else:
            for pi, p in enumerate(static_pools):
                if pi in consumed_static or i < pool_ready_bar(p):
                    continue
                lvl = float(p["price"])
                if p["side"] == "buyside":
                    if cl > lvl:
                        consumed_static.add(pi)
                    elif hi > lvl and cl < lvl:
                        consumed_static.add(pi)
                        bar_sweeps.append({
                            "side": "buyside", "kind": "equal_highs", "level": lvl,
                            "index": i, "time": t_i,
                        })
                else:
                    if cl < lvl:
                        consumed_static.add(pi)
                    elif lo < lvl and cl > lvl:
                        consumed_static.add(pi)
                        bar_sweeps.append({
                            "side": "sellside", "kind": "equal_lows", "level": lvl,
                            "index": i, "time": t_i,
                        })

        # 1c. Running server-day extremes (never body-broken, reset at midnight)
        if use_day_extremes and day_has and day_stamp == t_i // 86400:
            if hi > day_high and cl < day_high:
                bar_sweeps.append({
                    "side": "buyside", "kind": "day_high", "level": float(day_high),
                    "index": i, "time": t_i,
                })
            if lo < day_low and cl > day_low:
                bar_sweeps.append({
                    "side": "sellside", "kind": "day_low", "level": float(day_low),
                    "index": i, "time": t_i,
                })

        # Deduplicate same-bar sweeps at identical/overlapping levels (prefer
        # equal_highs/equal_lows over individual swing_high/swing_low when within tol)
        if bar_sweeps:
            dedup_tol = (float(closes[i - 1]) * float(tol_pct)) if tol_pct is not None else 1e-8
            kind_prio = {
                "equal_highs": 0, "equal_lows": 0,
                "day_high": 1, "day_low": 1,
                "swing_high": 2, "swing_low": 2,
            }
            bar_sweeps.sort(key=lambda s: (kind_prio.get(s["kind"], 9), s["level"]))
            kept: List[dict] = []
            for s in bar_sweeps:
                if any(
                    k["side"] == s["side"] and abs(k["level"] - s["level"]) <= dedup_tol
                    for k in kept
                ):
                    continue
                kept.append(s)
            kept.sort(key=lambda s: (s["side"], s["level"]))
            sweeps.extend(kept)

        # ---- Step 2: fold bar i into running server-day extremes AFTER evaluation
        if use_day_extremes:
            stamp = t_i // 86400
            if stamp != day_stamp:
                day_stamp, day_has = stamp, False
            if not day_has:
                day_high, day_low, day_has = hi, lo, True
            else:
                if hi > day_high:
                    day_high = hi
                if lo < day_low:
                    day_low = lo

        # ---- Step 3: body-close through a level breaks it for later bars
        k = bisect.bisect_left(live_highs, [cl, -1.0])
        if k:
            del live_highs[:k]
        k = bisect.bisect_right(live_lows, [cl, _BIG])
        if k < len(live_lows):
            del live_lows[k:]

        # ---- Step 4: pivots confirmed by bar i's close join AFTER evaluation
        while ptr < len(swings) and int(swings[ptr]["confirmed_at"]) <= i:
            sw = swings[ptr]
            confirmed_swings.append(sw)
            if sw["type"] == "high":
                bisect.insort(live_highs, [float(sw["price"]), next_seq])
            else:
                bisect.insort(live_lows, [float(sw["price"]), next_seq])
            next_seq += 1
            ptr += 1

    return sweeps
