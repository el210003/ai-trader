# Setup Builder

This document explains how raw SMC context is turned into a concrete trade
setup with **entry / stop-loss / take-profit / risk:reward / confluences**.
Entry point: `app/engine/setup_builder.py::build_setups`.

---

## What the builder has to decide

Given a symbol, timeframe, candle history, and the SMC context dict, the
builder must answer:

1. Is there a **tradeable setup** in either direction?
2. If yes, where exactly should the entry, stop, and target sit?
3. What **confluences** does it have (for both human readability and ML
   features)?

The output is a list of setup dicts:

```python
{
  "symbol", "tf", "direction": "long" | "short", "status": "active",
  "entry", "stop_loss", "take_profit",
  "rr",                                 # risk:reward ratio
  "entry_zone": {"type", "top", "bottom", "origin_time", "origin_index"},
  "range_position",                     # 0..1 — entry position in dealing range
  "trend_aligned", "has_recent_sweep",
  "confluences": [...],                 # human-readable confluence list
  "formed_index", "formed_at", "last_close",
}
```

The pipeline keeps the **best setup per direction** (highest ML prob, then
most confluences), so the dashboard shows at most one long and one short per
symbol/timeframe.

---

## The five-step pipeline

For each `direction ∈ ("long", "short")`:

1. **Direction gate** — require trend alignment *or* a recent sweep.
2. **Zone selection** — find the best fresh OB/FVG on the right side of price
   and in the correct premium/discount half.
3. **Entry price** — a limit order at the zone's top (long) or bottom (short),
   capped by current price.
4. **Stop-loss** — beyond the zone edge or the sweep wick (whichever is
   further), with an ATR buffer. Enforced minimum-risk floor.
5. **Take-profit** — nearest opposing liquidity pool / swing level that
   yields `RR ≥ min_rr`. Capped at `max_rr`.

If any step fails, that direction has **no setup**.

---

## Step 1 — Direction gate

```python
aligned = (direction == "long"  and trend == "bullish") or \
          (direction == "short" and trend == "bearish")

recent  = [s for s in smc["sweeps"]
           if s["side"] == want_side
           and (n - 1) - s["index"] <= sweep_lookback_bars]

if not aligned and not recent:
    continue   # no setup for this direction
```

| Trigger | Why |
|---|---|
| **Trend aligned** | The simplest case: trade with the structure. |
| **Recent opposing sweep** | The "spring" / "upthrust" pattern — sweep signals potential reversal. A sweep against the trade direction can justify a counter-trend entry. |

Both can be true simultaneously (trend aligned *and* a sweep happened); both
are valid triggers.

---

## Step 2 — Zone selection

The builder collects all fresh order blocks and FVGs in the same direction
as the trade:

```python
want_zone_dir = "bullish" if direction == "long" else "bearish"
zones = [("order_block", ob) for ob in smc["order_blocks"]["fresh"]
                          if ob["direction"] == want_zone_dir]
     + [("fvg", fg)       for fg in smc["fvgs"]["fresh"]
                          if fg["direction"] == want_zone_dir]
```

Each candidate is then filtered and scored:

```python
for ztype, z in zones:
    mid = (z["top"] + z["bottom"]) / 2.0
    if direction == "long":
        good_side   = z["top"] <= close           # retest entry below price
        correct_half = mid <= range["equilibrium"] # in discount
    else:
        good_side   = z["bottom"] >= close
        correct_half = mid >= range["equilibrium"] # in premium
    if not good_side: continue

    dist_atr = abs(close - mid) / atr
    if dist_atr > 12.0: continue                  # absurdly far zone

    score = (2 if correct_half else 0) \
          + (1 if ztype == "order_block" else 0) \
          + (1 if correct_half and ztype == "order_block" else 0)

    cands.append((score, -dist_atr, ztype, z, correct_half))
```

| Filter | Reason |
|---|---|
| **`good_side`** | Entry must be a *retest* below current price (longs) / above (shorts). Zones above current price aren't actionable as limit entries for longs. |
| **`dist_atr ≤ 12`** | Zones too far from price produce huge, unreliable setups. |
| **Score** | Order-block + correct-half combos rank highest; then either factor alone; then by proximity to current price. |
| **Tie-breaker** | Closest mid-price wins (`-dist_atr` for ascending sort). |

The winning zone becomes the entry zone; its `origin_time` and `origin_index`
flow into the setup dict and into the ML `zone_freshness` feature.

---

## Step 3 — Entry price

```python
if direction == "long":
    entry = min(z["top"], close)        # limit at zone top OR market if inside zone
else:
    entry = max(z["bottom"], close)
```

If the price is currently *inside* the zone, `entry = close` (market entry).
If the price is above the zone, `entry = zone_top` (a limit order waiting for
the retest).

---

## Step 4 — Stop-loss

The stop must clear the zone's lower edge *and* any recent sweep wick:

```python
if direction == "long":
    sweep_lows = [df["low"].iat[s["index"]] for s in recent]
    sl_base    = min([z["bottom"]] + sweep_lows)
    sl         = sl_base - sl_buffer_atr * atr
    risk       = entry - sl
else:
    sweep_highs = [df["high"].iat[s["index"]] for s in recent]
    sl_base     = max([z["top"]] + sweep_highs)
    sl          = sl_base + sl_buffer_atr * atr
    risk        = sl - entry

# volatility floor — never a razor-thin stop
if risk < min_risk_atr * atr:
    if direction == "long":  sl = entry - min_risk_atr * atr
    else:                    sl = entry + min_risk_atr * atr
    risk = min_risk_atr * atr
```

| Rule | Reason |
|---|---|
| **`sl_buffer_atr * ATR` past the worst level** | Avoid being stopped out by noise around the zone / sweep wick. |
| **Use `min(z_bottom, sweep_low)` (long)** | The stop clears both the OB edge and the deepest recent sweep wick. Whichever is further. |
| **Volatility floor** | A 2-pip stop on a 100-pip-ATR market is suicide. `min_risk_atr = 0.75` ensures every stop is at least 0.75 ATR away. |

If even with the floor `risk ≤ 0` (entry inside the SL — pathological data),
the direction is skipped.

---

## Step 5 — Take-profit

`_target_liquidity` picks the nearest opposing liquidity level that yields
`RR ≥ min_rr`:

```python
if direction == "long":
    pool_levels   = [p["price"] for p in pools if p["side"] == "buyside"]
    swing_levels  = [s["price"] for s in swings if s["type"] == "high"]
    floor         = max(entry, close)             # target must be beyond current price
    cands         = sorted({p for p in pool_levels + swing_levels if p > floor})
    for t in cands:
        rr = (t - entry) / risk
        if rr >= min_rr:
            return t, rr
    return entry + default_rr * risk, default_rr   # fallback
```

| Rule | Reason |
|---|---|
| **Targets must be beyond current price** | A TP below current price (for a long) is already being hit. Not a real target. |
| **`pools + swing_levels`** | Equal-highs pools are *the* classic smart-money targets; swing highs from any timeframe structure are second. |
| **First qualifying target wins** | Nearest target above `min_rr` — favors realistic, reachable levels. |
| **Fallback at `default_rr × risk`** | If no pool qualifies, take an RR-2 multiple of the stop. Less ideal but still tradeable. |

### RR cap

After picking the target, the builder caps fantasy RR:

```python
if rr > max_rr:                                    # default 5.0
    if direction == "long":  tp = entry + max_rr * risk
    else:                    tp = entry - max_rr * risk
    rr = max_rr
```

Without this cap, a 0.4-ATR risk with a faraway target produced RR > 15,
which the ML model treats as uninformative. The cap normalizes RR into a
useful range.

---

## Confluences

The builder appends human-readable chips that are also ML features:

```python
conf = []
if aligned:
    conf.append("market structure aligned")
if recent:
    last = recent[-1]
    conf.append(f"{last['side']} liquidity sweep ({last['kind'].replace('_', ' ')}) "
                f"{n - 1 - last['index']} bars ago")
if correct_half:
    conf.append(f"entry in {'discount' if direction=='long' else 'premium'} zone")
conf.append(f"fresh {ztype.replace('_', ' ')}")
if len(zones) > 1:
    conf.append(f"{len(zones)} fresh zones stacked")
```

`len(confluences)` becomes the ML `confluence_count` feature.

---

## Best-per-direction selection

In the pipeline, multiple setups can exist (one per direction plus possible
counter-trend variations). The pipeline keeps the best per direction:

```python
best = {}
for s in setups:
    key = s["direction"]
    rank = ((s["ml_prob"] if s["ml_prob"] is not None else 0.5),
            len(s["confluences"]))
    if key not in best or rank > best[key][0]:
        best[key] = (rank, s)
chosen = [s for _, s in best.values()]
```

| Ranking key | Order | Rationale |
|---|---|---|
| `ml_prob` (default 0.5 if no model) | Primary | Model's calibrated win probability. |
| `confluence_count` | Tie-breaker | When ML is unsure, more confluences wins. |

Final ordering on the dashboard:

```python
chosen.sort(key=lambda s: (-s["final_score"], s["direction"]))
```

The card with the highest `final_score` (from hybrid fusion) appears first.

---

## Why this design

### Why limit-order entries instead of market entries

Live OB/FVG entries are typically **wait-for-retest** orders. The builder
encodes that as `entry = min(zone_top, close)` so the trader can place a
limit at the zone top and only get filled if price actually returns. The
ML labeler simulates the same semantics — setups that never retested are
dropped from training.

### Why min-RR filter instead of always trading

Without a min-RR filter, the builder would happily produce 0.3-RR scalps that
are statistically doomed. `min_rr = 1.5` means every setup is profitable at
40% win rate — a realistic SMC floor.

### Why `max_rr = 5.0` cap

Targets above 5× risk produce unreliable labels in training (too few samples
ever reach them) and unhelpful scores in live inference. The cap concentrates
the model on realistic setups.

### Why minimum risk floor

A 0.2-ATR stop means a single tick can flip your trade. `min_risk_atr = 0.75`
forces every stop into a regime-appropriate distance, dramatically improving
the stability of both labels and live performance.

---

## Configuration reference

| Config key | Default | Effect |
|---|---|---|
| `smc.sweep_lookback_bars` | `30` | Window for "recent sweep" trigger. |
| `smc.min_rr` | `1.5` | Minimum RR; below this, no setup. |
| `smc.default_rr` | `2.0` | Fallback RR when no target qualifies. |
| `smc.max_rr` | `5.0` | Cap on RR. |
| `smc.sl_buffer_atr` | `0.25` | Buffer past zone / sweep wick (ATR units). |
| `smc.min_risk_atr` | `0.75` | Volatility floor for stop distance (ATR units). |
| `smc.ob_max_age_bars` | `300` | Max age for tradeable OBs. |
| `smc.fvg_max_age_bars` | `200` | Max age for tradeable FVGs. |

---

## Possible improvements

- **Multiple OBs/FVGs as one zone** — stack multiple adjacent zones into a
  "confluence zone" with a wider entry region.
- **Higher-timeframe bias** — fetch HTF context (e.g., H4 for an M15 setup)
  and require HTF trend alignment in addition to LTF.
- **Partial take-profits** — TP1 at nearest pool (RR ≥ min_rr / 2), TP2 at
  the swing high, with size splits.
- **Volatility-adjusted R** — make `min_rr` scale with `atr_pct` so
  high-volatility regimes require higher RR.
- **Risk-per-trade sizing** — output a recommended lot size given an
  account risk percentage, instrument pip value, and the computed stop.
