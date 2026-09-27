"""Rejection-bar detector: engulfing bar + level sweep (RejectionProto port).

Line-faithful port of the detection stream in docs/RejectionProto.mq5
(EvaluateBar / EngulfEval / IfPivotAdd / MarkBrokenByClose / UpdateDayExtremes;
research spec in docs/rejectionproto-research.md §1.1–1.2):

  REJECT := an engulfing bar (D1/D2/D3, direction-corrected) that *swept* a
            live level (wick pierced, close retreated) — the spring/fakey event.

Detection runs on closed bars only. The no-hindsight rule holds (ProcessBar
order): the evaluated bar references level books AS OF the previous close —
a pivot confirmed by the current bar joins only for later bars, a body-close
through a level breaks it only for later bars, and day extremes fold in only
after evaluation.

All bar anatomy uses INTEGER-POINT math with half-up rounding (the prototype's
ULP-safe rule — "double subtractions carry ULP noise that flips x.5 rounding
knife-edges"): D3's bodyPts*3 >= rangePts*2, clv/depth/br as integer percents,
exp10 from integer bodies, and haveExp = exp10 > 0 (an expansion that rounds
to 0.x0 is "no baseline" -> bucket 2, exactly as the EA decides it).

Bucket classification (§1.2), mutually exclusive and exhaustive:
  1  momentum  — haveExp && big_exp_tenths > 0 && exp10 >= big_exp_tenths
  0  A-grade   — haveExp && exp10 < mom_exp_tenths && clvPct < weak_close_pct
  2  other     — everything else (incl. the 2x–3x gap and no-baseline fallback)

Bucket details that matter:
  - momentum is evaluated FIRST (strict if/else-if, EA order);
  - big_exp_tenths == 0 disables the momentum bucket (EA: InpBigExpTenths > 0);
  - the 2x–3x gap and no-baseline bars land in bucket 2.

Level books (all swept references):
  - swing highs/lows: fractal N-bar pivots with the EA's tie-break — equality
    allowed against the OLDER flank, strict against the NEWER flank, so on a
    flat top/bottom (EQH/EQL) the NEWEST touch keeps the reference (built
    internally with the same N as ai-trader's swing_lookback);
  - equal-highs/lows pools: ai-trader's clustered liquidity (additive; the EA
    covers EQH via the newest-of-equals rule instead);
  - server-day extremes (InpUseDayExtreme): running day H/L, reset at server
    midnight, never body-broken — folded in AFTER evaluation.

Complexity: O(n log n + band hits). Live swing levels live in price-sorted
lists; a body-close through a level removes it (a break by close C removes a
strict prefix of the highs list / suffix of the lows list) and the per-bar
sweep test is a range query over the wick band — exact and fast enough for
training replay (analyze per window step).

Known deviations from the EA (documented in docs/rejection-entry.md):
  - noise gate is ATR-relative (min_range_atr) instead of fixed points, so it
    transfers across symbols (EA: InpMinRangePoints);
  - equal-highs/lows pools are additive references beyond the EA's books.
"""
import bisect
import math
from decimal import Decimal
from typing import List, Optional, Tuple

_SIDE_NAMES = {
    ("buyside", "swing_high"): "swing high",
    ("sellside", "swing_low"): "swing low",
    ("buyside", "day_high"): "day high",
    ("sellside", "day_low"): "day low",
    ("buyside", "equal_highs"): "equal highs",
    ("sellside", "equal_lows"): "equal lows",
}

_BIG = float("inf")


def swept_level_name(side: str, kind: str) -> str:
    return _SIDE_NAMES.get((side, kind), f"{side} {kind}")


def infer_point(df) -> float:
    """Infer the symbol's point size (10^-digits) from the price series.
    MT5 rates are exact decimal quotes, so the shortest repr recovers the
    quote precision; callers who know the digits can pass their own point."""
    best = 0
    for col in ("open", "high", "low", "close"):
        vals = df[col].values
        for v in (vals[0], vals[len(vals) // 2], vals[-1]):
            d = -Decimal(str(float(v))).as_tuple().exponent
            if d > best:
                best = d
    return float(10 ** -min(max(best, 1), 8))


def _half_up(x: float) -> int:
    """Half-up rounding for non-negative values (MQL5 MathRound semantics:
    x.5 edges settle away from zero, never banker's-round)."""
    return int(math.floor(x + 0.5))


def _engulf(i: int, o, h, l, c, point: float) -> Tuple[int, int]:
    """EngulfEval port: engulfing test of bar i vs bar i-1.

    Returns (eng, defs): eng = +1 bull / -1 bear / 0 none; defs bitmask
    (bit1 = D1 body engulf, bit2 = D2 outside bar, bit4 = D3 decisive).
    D3 uses integer points (bodyPts*3 >= rangePts*2, exact at the boundary —
    the EA's knife-edge rule; float compare mislands x.5 bars).
    """
    po, ph, pl, pc = o[i - 1], h[i - 1], l[i - 1], c[i - 1]
    bo, bh, bl, bc = o[i], h[i], l[i], c[i]

    bull_pair = pc < po and bc > bo      # P closed bear, B closed bull
    bear_pair = pc > po and bc < bo      # P closed bull, B closed bear
    if not (bull_pair or bear_pair):
        return 0, 0

    body_b, body_p = abs(bc - bo), abs(pc - po)
    range_b, range_p = bh - bl, ph - pl

    d1 = body_b > body_p and (
        (bo <= pc and bc >= po) if bull_pair else (bo >= pc and bc <= po))
    d2 = range_b > range_p and bh >= ph and bl <= pl
    body_pts = _half_up(body_b / point)
    range_pts = _half_up(range_b / point)
    d3 = d1 and range_pts > 0 and body_pts * 3 >= range_pts * 2

    defs = (1 if d1 else 0) | (2 if d2 else 0) | (4 if d3 else 0)
    if not defs:
        return 0, 0
    return (1 if bull_pair else -1), defs


def detect_pivots(df, n_swing: int, keep_last: int = 400) -> List[dict]:
    """IfPivotAdd port over the whole series (EA DrawSwingMarker bookkeeping):
    every confirmed N-bar fractal pivot, in confirmation order. A bar p is a
    swing high iff no OLDER flank bar has high > h (equality allowed) and no
    NEWER flank bar has high >= h (equality disqualifies) — on EQH/EQL the
    newest touch keeps the swing reference. The returned marker persists even
    after the level later breaks (the EA's chart object is never removed).

    Returns [{index, time, type: "high"|"low", price, confirmed_at}] —
    confirmed_at = pivot index + N (the EA's confirmation lag)."""
    n_swing = max(1, int(n_swing))
    h = df["high"].values
    l = df["low"].values
    times = df["time"].values
    n = len(df)
    out: List[dict] = []
    for p in range(n_swing, n - n_swing):
        phv = h[p]
        plv = l[p]
        is_high = True
        is_low = True
        for k in range(1, n_swing + 1):
            if h[p - k] > phv or h[p + k] >= phv:   # older eq ok | newer strict
                is_high = False
            if l[p - k] < plv or l[p + k] <= plv:   # older eq ok | newer strict
                is_low = False
            if not (is_high or is_low):
                break
        if is_high:
            out.append({"index": int(p), "time": int(times[p]), "type": "high",
                        "price": float(phv), "confirmed_at": int(p + n_swing)})
        if is_low:
            out.append({"index": int(p), "time": int(times[p]), "type": "low",
                        "price": float(plv), "confirmed_at": int(p + n_swing)})
    return out[-keep_last:]


def detect_rejections(df, pools: List[dict], atr_val: float, cfg: dict,
                      keep_last: int = 100) -> List[dict]:
    """Single chronological pass over closed bars (ProcessBar order:
    evaluate -> fold day extremes -> break body-closed levels -> add pivots
    confirmed by this bar). Returns rejection events (ascending by bar index,
    last `keep_last` kept):

      {index, time, direction: bull|bear, defs, bucket: 0|1|2,
       exp: x | None, clv: %, br: %, depth: % of bar range,
       swept: [{side, kind, level}], open, high, low, close}

    cfg keys (smc block):
      swing_lookback (3) — pivot width N (EA: InpSwingBars);
      rejection sub-block:
        avg_body_bars (20), mom_exp_tenths (20 = A-grade upper bound),
        big_exp_tenths (30 = momentum lower bound; 0 disables the bucket),
        weak_close_pct (50), min_range_atr (0.05), use_day_extremes (true).
    """
    rj = (cfg.get("rejection") or {})
    n_swing = max(1, int(cfg.get("swing_lookback", 3)))          # InpSwingBars
    avg_body_bars = max(0, int(rj.get("avg_body_bars", 20)))     # InpAvgBodyBars
    agrade_max_exp = int(rj.get("mom_exp_tenths", 20))           # exp10 <  20
    momentum_min_exp = int(rj.get("big_exp_tenths", 30))         # exp10 >= 30
    weak_close = int(rj.get("weak_close_pct", 50))               # clvPct <  50
    use_day = bool(rj.get("use_day_extremes", True))             # InpUseDayExtreme
    min_range = float(rj.get("min_range_atr", 0.05)) * (atr_val or 0.0)
    point = infer_point(df)

    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    times = df["time"].values
    n = len(df)

    def pts(v: float) -> int:
        return _half_up(v / point)

    events: List[dict] = []
    live_highs: List[list] = []      # price-sorted [price, seq] swing highs
    live_lows: List[list] = []       # price-sorted [price, seq] swing lows
    next_seq = 0
    consumed = set()                 # pool indices closed through
    # day-extreme book (UpdateDayExtremes): running server-day H/L as of the
    # previous close; never body-broken, reset at server midnight. Seeded with
    # bar 0 (the EA folds every bar, including the first backfill bar).
    day_stamp = int(times[0]) // 86400 if n else None
    day_high = float(h[0]) if n else 0.0
    day_low = float(l[0]) if n else 0.0
    day_has = n > 0

    for i in range(1, n):
        rng_i = h[i] - l[i]
        h_pts, l_pts = pts(h[i]), pts(l[i])
        c_pts, o_pts = pts(c[i]), pts(o[i])
        rng_pts = h_pts - l_pts          # EA anatomy: rngPts = hPts - lPts
        body_pts = abs(c_pts - o_pts)

        # ---- step 1a: sweep test against the books as of the previous close.
        # A level is swept when the wick pierces it and the close retreats:
        #   high X: h[i] > X > c[i]   low X: l[i] < X < c[i]
        # depth per level in integer % of the bar range (EA formula).
        # Day extremes sweep only when the running extremes belong to the
        # bar's own server day (EA: g_dayHas && g_dayStamp == DateOf(b.time)).
        swept: List[Tuple[int, str, str, float]] = []   # (seq, side, kind, level)
        if rng_pts > 0:
            k0 = bisect.bisect_right(live_highs, [c[i], _BIG])
            k1 = bisect.bisect_left(live_highs, [h[i], -1.0])
            for e in live_highs[k0:k1]:
                swept.append((e[1], "buyside", "swing_high", e[0]))
            k0 = bisect.bisect_right(live_lows, [l[i], _BIG])
            k1 = bisect.bisect_left(live_lows, [c[i], -1.0])
            for e in live_lows[k0:k1]:
                swept.append((e[1], "sellside", "swing_low", e[0]))
            if use_day and day_has and day_stamp == int(times[i]) // 86400:
                if h[i] > day_high and c[i] < day_high:
                    swept.append((500_000_000, "buyside", "day_high", day_high))
                if l[i] < day_low and c[i] > day_low:
                    swept.append((500_000_001, "sellside", "day_low", day_low))
            # equal-highs/lows pools (ai-trader's clustered liquidity;
            # consumed on close-through, additive to the EA's books)
            for pi, p in enumerate(pools):
                if pi in consumed or i <= p["last_time"]:
                    continue
                if p["side"] == "buyside":
                    if c[i] > p["price"]:
                        consumed.add(pi)         # closed through -> dead
                    elif h[i] > p["price"] and c[i] < p["price"]:
                        swept.append((1_000_000_000 + pi, "buyside", "equal_highs", p["price"]))
                else:
                    if c[i] < p["price"]:
                        consumed.add(pi)
                    elif l[i] < p["price"] and c[i] > p["price"]:
                        swept.append((1_000_000_000 + pi, "sellside", "equal_lows", p["price"]))
            swept.sort()                             # confirmation order (then pools)

        # ---- step 1b: engulfing test (noise gate on bar range; sweeps are
        # still evaluated for too-small bars, exactly as in the EA)
        eng, defs = 0, 0
        if rng_i >= min_range and rng_pts > 0:
            eng, defs = _engulf(i, o, h, l, c, point)

        # ---- step 1c: REJECT conjunction + instrumentation (§1.2 metrics)
        if eng != 0 and swept:
            clv_pct = _half_up(100.0 * ((c_pts - l_pts) if eng > 0 else (h_pts - c_pts)) / rng_pts)
            br_pct = _half_up(100.0 * body_pts / rng_pts)
            depth = max(
                _half_up(100.0 * ((h_pts - pts(lvl)) if side == "buyside"
                                  else (pts(lvl) - l_pts)) / rng_pts)
                for _sq, side, _kind, lvl in swept)

            # exp: signal-bar body vs the mean body of the prior avg_body_bars
            # bars (the signal bar itself excluded) — integer points, half-up
            exp10 = 0
            if avg_body_bars > 0:
                lo = max(0, i - avg_body_bars)
                cnt = i - lo
                sum_body = sum(abs(pts(c[k]) - pts(o[k])) for k in range(lo, i))
                if cnt > 0 and sum_body > 0:
                    exp10 = _half_up(10.0 * body_pts * cnt / sum_body)
            have_exp = avg_body_bars > 0 and exp10 > 0      # EA: exp10 > 0

            if have_exp and momentum_min_exp > 0 and exp10 >= momentum_min_exp:
                bucket = 1                       # momentum reject (EA order: first)
            elif have_exp and exp10 < agrade_max_exp and clv_pct < weak_close:
                bucket = 0                       # A-grade (weak close + small bar)
            else:
                bucket = 2                       # other (incl. the 2x-3x gap)

            events.append({
                "index": int(i), "time": int(times[i]),
                "direction": "bull" if eng > 0 else "bear",
                "defs": int(defs), "bucket": bucket,
                "exp": exp10 / 10.0 if have_exp else None,
                "clv": clv_pct, "br": br_pct, "depth": depth,
                "swept": [{"side": side, "kind": kind, "level": float(lvl)}
                          for _sq, side, kind, lvl in swept],
                "open": float(o[i]), "high": float(h[i]),
                "low": float(l[i]), "close": float(c[i]),
            })

        # ---- step 2: fold the bar into the running day extremes (AFTER
        # evaluation — the bar that sets a day extreme cannot sweep it);
        # reset at server midnight (EA: g_dayStamp roll)
        if use_day:
            stamp = int(times[i]) // 86400
            if stamp != day_stamp:
                day_stamp, day_has = stamp, False
            if not day_has:
                day_high, day_low, day_has = h[i], l[i], True
            else:
                if h[i] > day_high:
                    day_high = h[i]
                if l[i] < day_low:
                    day_low = l[i]

        # ---- step 3: body-close through a level breaks it (for later bars).
        # Highs break when close > level (a prefix of the sorted list); lows
        # when close < level (a suffix). Day extremes are never broken.
        k = bisect.bisect_left(live_highs, [c[i], -1.0])
        if k:
            del live_highs[:k]
        k = bisect.bisect_right(live_lows, [c[i], _BIG])
        if k < len(live_lows):
            del live_lows[k:]

        # ---- step 4: pivots confirmed by bar i's close join AFTER evaluation
        # (IfPivotAdd port, candidate p = i - N): equality allowed against the
        # OLDER flank, strict against the NEWER flank — on EQH/EQL the newest
        # touch keeps the swing reference.
        p = i - n_swing
        if p - n_swing >= 0:
            phv = h[p]
            plv = l[p]
            is_high = True
            is_low = True
            for k2 in range(1, n_swing + 1):
                if h[p - k2] > phv or h[p + k2] >= phv:   # older eq ok | newer strict
                    is_high = False
                if l[p - k2] < plv or l[p + k2] <= plv:   # older eq ok | newer strict
                    is_low = False
            if is_high:
                bisect.insort(live_highs, [phv, next_seq])
                next_seq += 1
            if is_low:
                bisect.insort(live_lows, [plv, next_seq])
                next_seq += 1

    return events[-keep_last:]
