# SMC Engine

This document explains how every component of the **Smart Money Concepts**
engine (`app/smc/`) works, how they fit together, and the design decisions
behind each one. Read it if you want to understand, validate, or tweak what
the engine considers a "trade setup".

---

## What the engine does

The engine takes a candle `DataFrame` (time, open, high, low, close, volume)
and produces one **SMC context dict** with everything downstream needs:

- The current market trend (bullish / bearish / neutral)
- All recent swing points
- All structural breaks (BOS / CHoCH) in chronological order
- Liquidity pools (equal highs / equal lows)
- Liquidity sweeps (stop hunts)
- The current dealing range + equilibrium + premium/discount classification
- Fresh and mitigated order blocks
- Fresh and filled fair value gaps
- Current ATR

Entry point: `app/smc/__init__.py::analyze(df, cfg)`.

---

## 1. Market structure (`structure.py`)

### 1a. Swing points — `find_swings`

Fractal pivot detection. A **swing high** at bar `i` is the unique max of
`high` over `[i-lookback, i+lookback]`. A **swing low** is the unique min of
`low` over the same window.

```python
for i in range(lookback, n - lookback):
    if highs[i] >= seg_h.max() and (seg_h == highs[i]).sum() == 1:
        swings.append({type: "high", index: i, price: ..., time: ...,
                       confirmed_at: i + lookback})
```

| Property | Reason |
|---|---|
| **Fractal, not ZigZag** | Every swing is a real local pivot; no merging of close-by pivots. Lets the equal-highs clustering see all candidates. |
| **Confirmed at `i + lookback`** | A swing is only *known* after `lookback` future bars confirm it. The structure walker uses `confirmed_at`, never the swing's own `index`, to avoid look-ahead. |
| **`== 1` uniqueness check** | Ignores plateaus so that a flat multi-bar top doesn't count as both a swing and not a swing. |

Config: `smc.swing_lookback` (default `3`). Higher = fewer, more meaningful
swings; lower = more reactive but noisier.

### 1b. BOS & CHoCH — `detect_structure`

For every confirmed swing, scan forward from its confirmation bar to the next
swing's confirmation. The first candle whose **close** breaks the level is the
structural break:

- `closes[i] > last swing high` → bullish break
- `closes[i] < last swing low` → bearish break

The break's *type* depends on the current trend:

| Trend before break | Break direction | Result |
|---|---|---|
| neutral | bullish | **BOS** (first bullish break starts a bullish trend) |
| bullish | bullish | **BOS** (continuation) |
| bearish | bullish | **CHoCH** (change of character — potential reversal) |
| neutral | bearish | **BOS** |
| bearish | bearish | **BOS** |
| bullish | bearish | **CHoCH** |

```python
for k, sw in enumerate(swings):
    start = sw["confirmed_at"]
    end = swings[k+1]["confirmed_at"] if k+1 < len(swings) else n
    for i in range(start, end):
        if sw["type"] == "high" and closes[i] > sw["price"]:
            etype = "choch" if trend == "bearish" else "bos"
            trend = "bullish"; break
        if sw["type"] == "low" and closes[i] < sw["price"]:
            etype = "choch" if trend == "bullish" else "bos"
            trend = "bearish"; break
```

The returned `trend` is the *current* trend after applying all breaks in
chronological order.

| Design choice | Reason |
|---|---|
| **Closes, not wicks** | Wick-breaks produce lots of false signals in noisy markets. Close-based breaks align with how institutional traders define breakouts. |
| **Break to next swing confirmation** | Scans only the interval where this swing is "the relevant one". As soon as a new swing confirms, it takes over. |
| **One break per swing** | The loop breaks after the first qualifying candle. Prevents one candle from triggering both a BOS and a CHoCH. |

---

## 2. Liquidity (`liquidity.py`)

Liquidity = clusters of stop-loss orders sitting at obvious levels. Smart
money hunts those stops before reversing. The engine tracks both the **pools**
(where stops sit) and the **sweeps** (when they get taken).

### 2a. Equal-level pools — `cluster_equal_levels`

Groups swing highs whose prices sit within `tol` of each other into "pools":

```python
levels = sorted(swings with type=="high" by price)
for i in unvisited:
    group = [levels[i]] + every subsequent level within tol of group[0]
    if len(group) >= min_touches:
        pools.append({"side": "buyside",
                      "price": mean of group prices,
                      "touches": len(group),
                      "last_time": max(group times),
                      "members": [group indices]})
```

| Knob | Default | Effect |
|---|---|---|
| `smc.eq_tolerance_pct` | `0.0006` | Absolute tolerance = `close * tolerance_pct`. Higher tolerance = bigger pools, fewer distinct levels. |
| `min_touches` | `2` | Minimum swings to qualify as a pool. 2 is the classic "equal highs/lows" threshold. |

Buyside pools sit **above** current price (stop losses of shorts).
Sellside pools sit **below** (stop losses of longs).

### 2b. Liquidity sweeps — `detect_sweeps`

A single pass over candles. Maintains pointers to the most recently confirmed
swing high/low and a `consumed` set for pools:

```python
last_high, last_low = None, None
ptr = 0; consumed = set()

for i in range(n):
    while ptr < len(swings) and swings[ptr].confirmed_at <= i:
        if type=="high": last_high = swings[ptr]
        else: last_low = swings[ptr]
        ptr += 1

    # structural sweep of the last swing level
    if last_high and highs[i] > last_high.price and closes[i] < last_high.price:
        sweeps.append({"side": "buyside", "kind": "swing_high", ...})
    if last_low  and lows[i]  < last_low.price  and closes[i] > last_low.price:
        sweeps.append({"side": "sellside", "kind": "swing_low", ...})

    # equal-level pool sweeps
    for pi, p in enumerate(pools):
        if pi in consumed or i <= p.last_time: continue
        if p.side == "buyside":
            if closes[i] > p.price:                 consumed.add(pi)
            elif highs[i] > p.price and closes[i] < p.price:
                sweeps.append({"side": "buyside", "kind": "equal_highs", ...})
        else:
            if closes[i] < p.price:                 consumed.add(pi)
            elif lows[i] < p.price and closes[i] > p.price:
                sweeps.append({"side": "sellside", "kind": "equal_lows", ...})
```

**A sweep = wick beyond the level + close back inside.** That's the stop-hunt
signature: stops trigger, then price reverses.

| Design choice | Reason |
|---|---|
| **Two checks per candle: last swing + all pools** | The last-swing check catches fresh structure sweeps; the pool check catches equal-level sweeps even after a BOS has consumed the most recent swing. |
| **Consume pool on first close through** | Once price closes beyond a pool, the pool's liquidity has been *used*; future wicks past it aren't sweeps anymore. Prevents a single pool from firing every bar in a trend. |
| **Single pass, O(n)** | The pointer into sorted swings never moves backward. Whole sweep pass is linear. |

---

## 3. Premium / Discount (`pd_zones.py`)

### `compute_dealing_range`

The current **dealing range** brackets price between the most recent
confirmed swing high and the most recent confirmed swing low:

```python
top    = max(last_high.price, last_low.price)  # defensive swap
bottom = min(last_high.price, last_low.price)
eq     = (top + bottom) / 2
position = (close - bottom) / (top - bottom)   # in [0, 1]
zone = "premium" if position > 0.5
       else ("discount" if position < 0.5 else "equilibrium")
```

| Property | Reason |
|---|---|
| **Last confirmed swing on each side** | The market is "ranging" between these two anchors — price is currently oscillating inside them. |
| **Fallback to 200-bar rolling extremes** | If no swings confirmed (very short history), fall back to a 200-bar window's high/low. |
| **`position` in [0,1]** | Discount = buy territory (lower half), Premium = sell territory (upper half). Setup builder requires long entries in discount and short entries in premium. |

---

## 4. Order blocks (`order_blocks.py`)

An **order block** is the last opposite-color candle before the impulsive leg
that breaks market structure.

### Construction

For each BOS/CHoCH event with break bar index `i`:

```python
for j in range(i-1, max(i-impulse_walkback-1, -1), -1):
    if direction == "bullish" and closes[j] < opens[j]: ob_idx = j; break
    if direction == "bearish" and closes[j] > opens[j]: ob_idx = j; break
```

The OB zone is `[lows[ob_idx], highs[ob_idx]]` — full candle range.

### Mitigation

A bullish OB is **mitigated** when price returns *into the zone after having
left it*. The naive implementation ("any later candle's low ≤ zone top")
instantly flags every OB, because the impulse candle itself still overlaps the
zone. The fix:

```python
left_at = None
for k in range(i, n):                              # find first candle fully above
    if direction == "bullish" and lows[k] > top: left_at = k; break
    ...
if left_at is None:
    mitigated_index = i                              # never left → consumed immediately
else:
    for k in range(left_at + 1, n):                  # then find first return
        if direction == "bullish" and lows[k] <= top: mitigated_index = k; break
        ...
```

| Property | Reason |
|---|---|
| **"Leave first, return second"** | A real OB retest happens after the impulse leg completes. Without this guard, every OB gets flagged on its first impulse candle. |
| **`never left → mitigated_index = i`** | If the OB candle sits so close to the level that the impulse never clearly exits it, the OB is considered consumed (invalid). |
| **Full candle range as zone** | Conservative: both wicks are potential reaction points, not just the body. |

Output:

```python
{"fresh": [unmitigated OB age ≤ ob_max_age_bars][-8:],
 "all":   [last 14 OBs, mitigated or not, for chart display]}
```

---

## 5. Fair value gaps (`fvg.py`)

A **fair value gap** is a 3-candle imbalance: the wicks of candle 1 and
candle 3 don't overlap, leaving a gap around candle 2's body.

```python
for i in range(2, n):
    if lows[i] > highs[i-2]:                       # bullish FVG (gap up)
        direction, top, bottom = "bullish", lows[i], highs[i-2]
    elif highs[i] < lows[i-2]:                     # bearish FVG (gap down)
        direction, top, bottom = "bearish", lows[i-2], highs[i]
    else:
        continue
```

### Fill detection

A bullish FVG is **filled** when price trades *completely through* it:

```python
for k in range(i+1, n):
    if direction == "bullish" and lows[k] <= bottom:  # closed the whole gap
        filled_index = k; break
    if direction == "bearish" and highs[k] >= top:
        filled_index = k; break
```

| Design choice | Reason |
|---|---|
| **Full fill, not partial** | Partial fills turn the gap into an inverse FVG (institutional signal) — too noisy for a v1. Full-fill is unambiguous. |
| **Gap defined by candle 1's high and candle 3's low** | Standard definition. The middle candle's body is ignored. |

---

## 6. ATR (`atr` in `smc/__init__.py`)

Wilder's smoothing via exponential moving average:

```python
tr = max(high - low, |high - prev_close|, |low - prev_close|)
atr = tr.ewm(alpha = 1/period, adjust=False).mean()
```

Used for:
- Volatility-aware SL buffer (`sl_buffer_atr * ATR`)
- Minimum-risk floor (`min_risk_atr * ATR`)
- One of the ML features (`atr_pct = ATR / close * 100`)

Config: `smc.atr_period` (default `14`).

---

## 7. Putting it together — `analyze(df, cfg)`

```python
swings     = find_swings(df, lookback)
events, trend = detect_structure(df, swings)

tol = close * eq_tolerance_pct
pools = cluster_equal_levels(swings, "buyside", tol) \
      + cluster_equal_levels(swings, "sellside", tol)

sweeps         = detect_sweeps(df, swings, pools)
dealing_range  = compute_dealing_range(df, swings)
order_blocks   = find_order_blocks(df, events, ob_max_age_bars)
fvgs           = find_fvgs(df, fvg_max_age_bars)
atr            = atr(df, atr_period)

return {trend, swings, events, sweeps, liquidity_pools,
        dealing_range, order_blocks, fvgs, atr,
        last_close, n_bars}
```

The orchestrator runs each detector once and returns a single dict. Every
downstream consumer (setup builder, dashboard, ML feature engineering, LLM
context) reads from this dict.

---

## Complexity

All detectors run in linear or near-linear time on the candle count:

| Detector | Complexity |
|---|---|
| `find_swings` | O(n × lookback) (small constant `lookback`) |
| `detect_structure` | O(n) — single pass per swing |
| `cluster_equal_levels` | O(s²) over `s` swings (s ≈ n/6, so fast) |
| `detect_sweeps` | O(n + n × pools) (pools few) |
| `compute_dealing_range` | O(s) |
| `find_order_blocks` | O(events × walkback + n × obs) |
| `find_fvgs` | O(n × fills) |

The full SMC pass over 1500 candles runs in **well under a second** in pure
Python — fast enough that the ML trainer can call it ~370 times per
symbol/timeframe without trouble.

---

## Configuration reference (`config.yaml` → `smc.*`)

| Key | Default | Meaning |
|---|---|---|
| `swing_lookback` | `3` | Bars on each side for fractal pivot detection. |
| `eq_tolerance_pct` | `0.0006` | Fraction of close used as absolute tolerance for equal-high/lows clustering. |
| `ob_max_age_bars` | `300` | Max age for a fresh (tradeable) order block. |
| `fvg_max_age_bars` | `200` | Max age for a fresh (tradeable) fair value gap. |
| `sweep_lookback_bars` | `30` | How far back a sweep counts as "recent" for setup triggers. |
| `min_rr` | `1.5` | Minimum risk:reward for setups (filter). |
| `default_rr` | `2.0` | Fallback RR when no liquidity target meets `min_rr`. |
| `max_rr` | `5.0` | Cap on RR — fantasy targets are clipped. |
| `sl_buffer_atr` | `0.25` | SL buffer beyond zone / sweep wick (ATR units). |
| `min_risk_atr` | `0.75` | Minimum stop distance from entry (ATR units). |
| `atr_period` | `14` | Wilder's ATR period. |
