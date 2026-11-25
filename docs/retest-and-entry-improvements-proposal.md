# Proposal: Solving the "Perpetual Retest Wait" & Expanding Actionable Entry Families

> **REVIEW STATUS (2026-11-25):** This document was written from a first-pass reading of
> the builder and the journal. A subsequent audit against the live DB, the ML training
> path, and the executor found **several claims in §2 that do not survive verification**,
> and **one defect that invalidates the premise of Proposal 5**. See
> **[§6 — Review Findings](#6-review-findings-verified)** for the corrections, and
> **[§7 — Revised Recommendation](#7-revised-recommendation-what-to-actually-do)** for the
> plan that replaces §5. Sections §1–§5 are retained unchanged as the original draft so the
> reasoning trail stays visible.

---

## 1. Executive Summary

`ai-trader` currently models trade entries almost exclusively as **passive deep-retest limit orders** parked at the distal edge of unmitigated Order Blocks (OBs) and Fair Value Gaps (FVGs). While theoretically clean in textbook Smart Money Concepts (SMC), live forward-journal telemetry (`data/trader.db`, `n = 668` setups, `613` resolved) shows that in real FX/metals price action, strong directional moves rarely pull back `0.50–1.00 ATR` into deep OBs before reaching liquidity targets.

This proposal introduces five targeted changes across `app/engine/setup_builder.py`, `app/smc/order_blocks.py`, `app/execution/executor.py`, and `config.yaml`:

1. **Tighten Zone Validity Windows (`24 -> 6` bars for FVGs, `8` bars for OBs)** and add **distance-decay / early-invalidation rules** so stale limits stop cluttering the book.
2. **Add Shallow-Pullback & Immediate-Touch Entry Modes (`near_zone_market_atr` & `ob_max_penetration`)** so setups within `0.20 ATR` of a fresh zone execute immediately rather than missing fills by 1–3 pips.
3. **Introduce a `momentum_continuation` (BOS/Displacement Break-and-Hold) Setup Family** to capture high-momentum trends that never retest deep OBs.
4. **Upgrade `rejection_bar` Triggering** (allow close-chase when `exp >= 3.0x` momentum criteria hold, and tighten retrace depth to `35%`) to scale up the system's highest-win-rate family (`51.7%` WR).
5. **Align Execution Geometry (`executor.py`)** so `market` execution recalculates SL/TP/RR from the live ask/bid (and rejects if `effective_rr < min_rr`) instead of executing deep-limit geometry at market price.

---

## 2. Diagnostic Evidence (Code & Journal Audit)

> ⚠️ **SUPERSEDED IN PART — read [§6](#6-review-findings-verified) first.**
> §2.2's family table and §2.3's distance table **pool two builder regimes** and are
> dominated by the *old* pre-fix regime (`id < 2132`). The verified findings are:
> the current builder's unfilled rate is **5.1%** (not `18.8%`); the `> 0.55 ATR`
> distance bucket is **+0.322R**, not a loss bucket; and the dominant setup-killer is
> the **zone-age gate at `setup_builder.py:328`**, which annihilates 75% of order blocks
> because an OB's `origin_index` sits ~137 bars back. See §6.2, §6.3, §6.5.

### 2.1 Why the Engine Always Waits for a Retest

1. **Hard `good_side` Limit Filter (`app/engine/setup_builder.py:331-336`):**
   Every zone candidate must satisfy `z["top"] <= close` (long) or `z["bottom"] >= close` (short), and entry is placed at `min(z["top"], close)` / `max(z["bottom"], close)`. Price is never allowed to enter on displacement breakout or shallow consolidation above/below the zone.
2. **Double-Impulse Requirement on Order Blocks (`app/smc/order_blocks.py:37-47`):**
   An OB is only marked fresh after `lows[k] > top` (`left_at`), meaning price must already impulse completely clear of the OB candle, and then `setup_builder.py` waits for a *second* full pullback to `z["top"]`. Any single-wick touch (`lows[test_start:n] <= z_entry`) permanently disqualifies the zone (`skip_already_tested: true`).
3. **50% Retrace Requirement on Rejection Bars (`app/engine/setup_builder.py:113-138`):**
   Even when an engulfing liquidity-sweep bar closes decisively (`bucket == 1` momentum reject), `mode: retrace` with `retrace_pct: 50` forces a limit order halfway back into the signal bar's body—missing runaway reversals.
4. **Execution Entry-Type Mismatch (`config.yaml:185`):**
   `setup_builder.py` computes `entry`, `stop_loss`, `take_profit`, `rr`, and `ml_prob` assuming a passive limit fill at the zone edge, while `execution.entry_type` defaults to `market`. Executing a `0.75 ATR`-distant limit setup at market immediately sacrifices `0.75 ATR` of reward and widens stop risk by `0.75 ATR`.

### 2.2 Historical Performance Breakdown (`data/trader.db`)

| Setup Family (`kind:zone`) | Total | Resolved | Filled | Expired Unfilled | Win | Loss | Fill Rate | Win Rate (Filled) | Win Rate (All Resolved) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `zone:fvg` | 521 | 476 | 404 | 74 (`15.5%`) | 144 | 242 | 84.5% | 37.3% | 30.3% |
| `zone:order_block` | 114 | 106 | 65 | 41 (`38.7%`) | 19 | 44 | 61.3% | 30.2% | 17.9% |
| `zone:rejection_bar` | 31 | 29 | 29 | 0 (`0.0%`) | 15 | 14 | 100.0% | **51.7%** | **51.7%** |
| **All Setups** | **668** | **613** | **498** | **115 (`18.8%`)** | **178** | **300** | **81.2%** | **37.2%** | **29.0%** |

### 2.3 Entry Distance & Fill-Latency Distribution

* **Distance to Entry (`entry_distance_atr`, `n = 272`):**
  * `< 0.10 ATR`: `27` setups (`59.3%` filled, `62.5%` WR on closed trades)
  * `0.10 – 0.25 ATR`: `48` setups (`85.4%` filled, `38.5%` WR)
  * `0.25 – 0.50 ATR`: `71` setups (`84.5%` filled, `45.8%` WR)
  * `0.50 – 1.00 ATR`: `126` setups (**`46.3%` of all setups**, `70.6%` filled, `43.5%` WR)
  * Among the **20 most recent journaled setups**, `18/20` required `0.31–0.98 ATR` pullbacks.
* **Time to Fill (`n = 206` filled setups with recorded `fill_time`):**
  * **70.4% (`145/206`)** fill on **Bar 1** (`<= 15 min` on M15).
  * **Median = 1.0 bar**, **75th percentile = 2.0 bars**.
  * Setups unfilled after 4 bars rarely fill while the original directional thesis is still intact.

---

## 3. Proposed Improvements

### Proposal 1: Shorten Retest Windows & Cap Maximum Retest Distance

**Problem:** `max_entry_distance_atr: 1.0` allows zones up to a full ATR away, and `entry_valid_bars: 24` (6 hours on M15) leaves stale limit orders open long after momentum fades.

**Changes (`config.yaml` & `app/engine/setup_builder.py`):**
* Reduce `smc.max_entry_distance_atr` from `1.0` to **`0.55`** (eliminates the furthest half of the `0.50–1.00 ATR` bucket where non-fills concentrate).
* Reduce `smc.entry_valid_bars` from `24` to **`6`** (90 minutes on M15) for FVGs and **`8`** (2 hours) for Order Blocks.
* Reduce `execution.pending_expiry_minutes` from `240` to **`90`**.
* **Early Invalidation Rule:** Cancel/skip any pending zone setup if price reaches **>= 60% of the distance to `take_profit`** before filling (`(high - entry) / (tp - entry) >= 0.60` for longs). Once opposing liquidity is already swept/reached without us, the retest thesis is invalidated.

---

### Proposal 2: Near-Zone Market Trigger (`near_zone_market_atr`) & Shallow OB Entry

**Problem:** Many setups sit `0.10–0.25 ATR` from current price (`dist_atr <= 0.25`) and miss fills by a few pips before launching toward TP.

**Changes (`app/engine/setup_builder.py` & `app/smc/order_blocks.py`):**
* Add `smc.near_zone_market_atr: 0.20`:
  * When a valid fresh OB/FVG is within `0.20 ATR` of `last_close` **and** structure + H1 HTF bias are aligned (`aligned and trend_align >= 1.0`), emit `entry_style: "market"` with `entry = last_close` (provided `rr >= min_rr` holds from `last_close`).
* **Order Block Proximal Buffer:** Allow OB selection when price has wicked up to `25%` into the OB body (`ob_max_penetration: 0.25`) rather than disqualifying the entire OB on a 1-pip touch (`skip_already_tested` currently drops the OB on any `low <= z["top"]`).

---

### Proposal 3: New `momentum_continuation` Setup Family (No Deep Retest Required)

**Problem:** During strong London/NY trends or CSM-aligned breakouts, price breaks structure (BOS) with displacement and never pulls back into a prior OB/FVG. Currently, `build_setups()` emits **zero** setups during these moves.

**Design (`app/engine/setup_builder.py` -> `_continuation_setups()`):**
Trigger when **all** of the following hold on the entry timeframe (`M15`):
1. **Confirmed BOS in trend direction** within the last `1–2` bars (`ev["type"] == "bos"` and `ev["direction"] == want_trend`).
2. **Displacement candle:** Break bar body `>= 1.2 * ATR` and close in the top/bottom `25%` of its range (`clv >= 0.75` for long, `<= 0.25` for short).
3. **HTF + CSM Alignment:** Nearest HTF (`H1`) trend matches direction (`trend_align >= 1.0`) and `csm_dir == direction`.
4. **Entry / SL / TP Geometry:**
   * `entry_style`: `"market"` (or shallow `20%` micro-pullback limit valid for `2` bars).
   * `stop_loss`: Below the displacement bar low (or last micro-swing low) minus `0.25 * ATR`, subject to `min_risk_atr: 0.50`.
   * `take_profit`: Next opposing HTF/LTF liquidity pool with `rr >= 1.5` (capped at `max_rr: 3.0`).
   * `setup_kind`: `"continuation"`.

---

### Proposal 4: Adaptive Rejection-Bar Entry (`close` vs `retrace` by Momentum Bucket)

**Problem:** `zone:rejection_bar` has a `51.7%` win rate but generates only `4.6%` (`31/668`) of setups because `mode: retrace` requires a 50% body pullback even on explosive momentum rejections (`bucket == 1`, `exp >= 3.0x`).

**Changes (`app/engine/setup_builder.py::_rejection_setups` & `config.yaml`):**
* Add `smc.rejection.momentum_close_entry: true`:
  * When `ev["bucket"] == 1` (momentum reject, `exp >= 3.0x`) and `aligned == True`, use **`entry_style: "close"`** (`entry = c_i`) so the bot enters immediately on bar close without waiting for a 50% retrace that rarely comes on high-expansion bars.
  * Keep `mode: "retrace"` for `bucket == 0` (A-grade) and `bucket == 2`, but tighten `retrace_pct` from `50` to **`35`** and `entry_valid_bars` from `8` to **`4`**.

---

### Proposal 5: Execution Geometry Guard in `ExecutionEngine`

**Problem:** `config.yaml` sets `execution.entry_type: market`, whereas `zone` setups compute `sl`, `tp`, `rr`, and `ml_prob` around a passive limit `entry` up to `1.0 ATR` away.

**Changes (`app/execution/executor.py::evaluate_and_execute`):**
1. Respect `setup["entry_style"]` (`"limit"` vs `"market"`) explicitly:
   * `"limit"` setups (`zone` setups with `dist_atr > near_zone_market_atr`, `retrace` rejections) place MT5 `BUY_LIMIT` / `SELL_LIMIT` orders at `setup["entry"]` with `pending_expiry_minutes: 90`.
   * `"market"` setups (`near_zone_market`, `continuation`, `momentum_close` rejections) execute at market.
2. **Live Slippage / Drift Recalculation:** Whenever a setup executes at `market` (including limit-to-market fallback when price is already in the zone), recompute effective RR against live `tick.ask` / `tick.bid`:
   ```python
   eff_risk = abs(live_price - sl)
   eff_reward = abs(tp - live_price)
   eff_rr = eff_reward / eff_risk if eff_risk > 0 else 0.0
   if eff_rr < min_rr:
       return {**base, "status": "skipped", "reason": f"live drift cut RR to {eff_rr:.2f} < {min_rr}"}
   ```

---

## 4. ML & Labeler Compatibility

Because `app/engine/labeler.py` and `app/ai/features.py` rely on `setup_builder.py` output during historical replay training:

1. **Bump `BUILDER_VERSION`** in `app/ai/ml_model.py` when activating Proposals 1–4 so the stale-model guard triggers a clean retrain.
2. **Add `setup_kind` & entry-style features** in `app/ai/features.py` (`is_continuation`, `is_market_entry`) so the classifier learns distinct base rates for `zone`, `rejection`, and `continuation` setups rather than conflating limit fills with market entries.
3. **Preserve `label_setup_with_index()` causality:** For `entry_style == "market"`, bar `i + 1` fills immediately (`filled = True` on bar `i + 1`), while `entry_style == "limit"` keeps the exact `lo <= entry + tol` fill check within `entry_valid_bars`.

---

## 5. Rollout & Validation Plan

> ⚠️ **REPLACED by [§7.2](#72-the-plan-that-replaces-§5).** Proposals 1 and 4 in this
> table are now scored **"do not do"** (see §7.1) and Proposal 5's diagnosis was
> **inverted** (§6.6). The table below is retained as the original draft.

| Phase | Scope | Files Touched | Success Metric (300+ Resolved Setups) |
|---|---|---|---|
| **Phase 1 (Config & Guardrails)** | Tighten `max_entry_distance_atr: 0.55`, `entry_valid_bars: 6`, `retrace_pct: 35`, `momentum_close_entry: true`, and add `eff_rr` market guard in `executor.py` | `config.yaml`, `app/engine/setup_builder.py`, `app/execution/executor.py` | Cut `EXPIRED_UNFILLED` from `18.8%` to **`< 8%`**; increase `rejection` share from `4.6%` to `12–15%` |
| **Phase 2 (Near-Zone Market + OB Penetration)** | Add `near_zone_market_atr: 0.20` and `ob_max_penetration: 0.25` | `app/engine/setup_builder.py`, `app/smc/order_blocks.py` | Raise `order_block` fill rate from `61.3%` to **`> 80%`** without degrading filled WR |
| **Phase 3 (Continuation Family + Retrain)** | Implement `_continuation_setups()`, add `is_continuation` feature, bump `BUILDER_VERSION`, retrain ML model, validate in `dry_run: true` | `app/engine/setup_builder.py`, `app/ai/features.py`, `app/ai/ml_model.py` | Positive expectancy (`> +0.15R`) on `continuation` setups and higher daily actionable `BUY/SELL` signal count during London/NY sessions |

---

## 6. Review Findings (Verified)

This section is the result of auditing the draft above against `data/trader.db`
(`n = 668` journal rows, `613` resolved, `n = 67` analyses), the ML training path,
and the execution path. **Six findings; three of them contradict the draft.**

### 6.1 ✅ CONFIRMED — the retest-wait perception is real, but it is a *rendering* symptom, not the root cause

The dashboard badge (`app/dashboard/static/index.html:1080`) shows
`limit pending · waiting retest` whenever `distAtr > 0.3`. Of the 272 journal rows
that carry `entry_distance_atr`:

| Bucket | Rows | Share |
|---|---:|---:|
| `distAtr <= 0.30` → *actionable now* | 85 | **31.2%** |
| `distAtr > 0.30` → *waiting retest* | 187 | **68.8%** |

And of the **20 most recent** journaled setups, **19/20 have `entry_distance_atr >= 0.31`**
(median ≈ `0.66 ATR`). So the user's perception — *"it will always wait for the retest"* —
is literally what the UI shows, and the UI is right about what it is rendering.

### 6.2 ❌ CORRECTION — §2.3's headline "`0.50–1.00 ATR` = 46.3% of setups" is a **measurement artifact**

The draft's §2.3 table and the "18 of 20 most recent setups sit `0.31–0.98 ATR` away"
claim were computed over a **biased subpopulation**. `entry_distance_atr` is only
persisted on **272 of 668 rows (41%)** — and those rows are **exactly the newer
journal rows** (`id >= 2132`, ids `2132..2403`), which also carry `features`,
`fill_tolerance`, and `atr` in their payload. Rows `id < 2132` (396 rows) were
written by an older `append_setups` with a 7-key payload and have **no**
`entry_distance_atr` at all.

This creates a **regime split** that invalidates any pooled statistic:

| Regime | Rows | Unfilled rate | WR (filled) | Expectancy |
|---|---:|---:|---:|---:|
| **Old** (`id < 2132`, pre-`left_at`-fix builder) | 396 | **25.5%** | 31.9% | **−0.051R** |
| **New** (`id >= 2132`, current builder) | 272 | **5.1%** | 44.7% | **+0.283R** |

**The old regime is the "it never fills / it fills too late" regime the user remembers.**
It was largely fixed by commit `d466052` (retest buffer + validity window) and `e8dfe8e`
(`left_at` for the already-tested check). The current builder's unfilled rate is
**5.1%, not 18.8%**.

> **Consequence for the draft:** §2.2's family table (`zone:order_block` "38.7% unfilled",
> "17.9% win rate") is **old-regime data**. In the new regime, `order_block` has only
> **12 rows** (`n=12`, 5 losses, 0 wins) — statistically meaningless. The `fvg` family
> in the new regime is `n=229`, fill 72.1%, **WR 44.8%**, and `rejection_bar` is
> `n=31`, fill 93.5%, **WR 51.7%**.

### 6.3 ❌ CORRECTION — the "distance" thesis does not survive stratification

Re-running §2.3's distance analysis **within the new regime only** (the 272 rows that
actually have the field) shows expectancy is **not monotone in distance**:

| `entry_distance_atr` | n | Fill % | WR (filled) | Expectancy |
|---|---:|---:|---:|---:|
| `<= 0.10` | 27 | 59.3% | 62.5% | **+0.684R** (n=16) |
| `0.10–0.25` | 51 | 86.3% | 38.1% | +0.139R (n=42) |
| `0.25–0.40` | 44 | 79.5% | 50.0% | **+0.399R** (n=34) |
| `0.40–0.55` | 41 | 90.2% | 40.0% | +0.082R (n=35) |
| `> 0.55` | 109 | 67.9% | 44.4% | **+0.322R** (n=72) |

The `> 0.55 ATR` bucket — the one the draft wanted to eliminate — has **+0.322R**
expectancy on 72 filled trades, *better* than the `0.10–0.25` bucket (+0.139R). The
`0.40–0.55` band (+0.082R) and `0.10–0.25` band (+0.139R) are the genuinely weak ones.
**Distance alone is not a valid proxy for "waiting is bad."** Cutting
`max_entry_distance_atr` to `0.55` (draft §3, Proposal 1) would have removed a
**positive-expectancy** population. Note also that only `n = 272` rows support any
distance conclusion, and the per-bucket `n` of 16–72 means wide confidence intervals —
the `> 0.55` bucket's WR CI is roughly `33%–56%`.

### 6.4 ❌ CORRECTION — the `rejection_bar` family is *not* "under-triggered by retrace depth"

Draft §3/Proposal 4 claimed the rejection family is rare because `mode: retrace` +
`retrace_pct: 50` demands too deep a pullback. The funnel says otherwise:

- Rejection **detection** is genuinely rare: **100 rejections in 7,304 EURUSD M15 bars**
  (≈ 1 per 73 bars), bucket split `bucket 2: 72, bucket 0: 14, bucket 1: 14`.
- The measured funnel (full history, EURUSD / GBPUSD / USDJPY M15, evaluated at the
  **trigger bar's** close):

| Stage | EURUSD | GBPUSD | USDJPY |
|---|---:|---:|---:|
| Bars with an eligible trigger (`trigger_lookback_bars: 2`) | 192 (2.6% of bars) | 194 (2.7%) | 193 (2.6%) |
| …surviving `max_retrace_atr: 1.0` | 177 (92.2%) | 176 (90.7%) | 167 (86.5%) |
| …surviving `require_directional_sweep` | **82 (42.7%)** | **104 (53.6%)** | **96 (49.7%)** |

So the binding constraints are **detection rarity** (≈1 per 73 bars) and the
**directional-sweep requirement** (which discards ~half of what survives). The
`max_retrace_atr` cap discards only ~7–13%, and `retrace_pct` is a *minor* lever:

| `retrace_pct` | EURUSD surviving | GBPUSD | USDJPY |
|---|---:|---:|---:|
| `50` (current) | 177 | 176 | 167 |
| `35` | 179 | 180 | 175 |
| `25` | 183 | 184 | 181 |

Shallowing the retrace from 50% → 25% would add roughly **3–8 setups per symbol over
7,300 bars** — i.e. *noise*, not the missing volume.

**Correct lever:** widen `trigger_lookback_bars` (2 → 4–6) — at `lb=6` the eligible-trigger
rate rises from **2.6% → 6.8% of bars** (2.6×) — and relax the directional-sweep
requirement, **not** `retrace_pct`.

And note `docs/rejectionproto-research.md` §"⚠ CORRECTION (2026-09-26)" voided the
momentum-reject **close-chase** edge (all positive P&L was a Python bug), so the draft's
"close-chase on momentum rejects" (Proposal 4) runs **directly against** the project's
own tick-exact research verdict.

### 6.5 ⚠️ NEW — the deeper problem is **setup starvation**, not entry timing

The live `analysis` table has **67 rows**, of which **44 are `context_only`** and only
**22 are M15**. Of those 22 M15 symbols, **21 produce ZERO setups** and one produces one
(`AUDCHF M15`, `dist_atr = 0.76`, verdict `WAIT`). Total live setups: **2**.

Replaying the *real* `build_setups()` over all 22 M15 symbols with real HTF context
(1,500-bar window), the drop reasons are:

| Gate (in `setup_builder.py`) | Drops |
|---|---:|
| `zone_age > entry_valid_bars` (L328) | 116 |
| `no_candidate` after all zone filters | 23 |
| `skip_already_tested` (L342–349) | 20 |
| `chasing / max_entry_distance_atr` (L398–401) | 16 |
| `require_htf_bias` | 2 |
| `wrong_side` (L337) | 1 |
| **Kept** | **2** |

**The dominant killer is the zone-age gate at L328 — and it is a bug, not a design
choice.** `entry_valid_bars` is doing **two incompatible jobs** at once:

1. **Fill-window semantics** — "expire a setup that hasn't filled within N bars"
   (used by the labeler at `labeler.py:35` and the resolver at `outcomes.py:64`).
2. **Zone-age semantics** — "reject a zone that formed more than N bars ago"
   (used at `setup_builder.py:328`).

These collide because an **order block's `origin_index` is the candle *before* the
impulse leg**, so OBs are inherently old: across 10 symbols, fresh OBs have a **median
origin age of 137 bars** (75% are older than 24 bars), while fresh FVGs have a median
age of **13 bars**. A single `entry_valid_bars: 24` therefore annihilates **75% of all
order blocks** while barely touching FVGs — which is precisely why the journal is
`521 fvg / 114 order_block / 31 rejection_bar` and why 21/22 symbols emit nothing.

**Verified:** disabling the zone-age gate entirely produces **the same 14 setups** in
the isolated harness (the age gate is not what survives; the `wrong_side` /
`already_tested` / `chasing` gates are). So this is a **reallocation** problem, not
simply "remove a filter".

> **The prior ablation in `aa3834c` is the cautionary tale:** `entry_valid_bars: 12`
> *hurt* expectancy (`+0.041R → −0.014R`) — but that test changed the **fill window**
> (a real, measurable tightening) while the **zone-age** side effect was an unintended
> confound. The two semantics must be split into two knobs before either is re-tuned.

### 6.6 ❌ CORRECTION — Proposal 5's premise ("`entry_type: market` vs deep-limit geometry") is **inverted**

The draft assumed `config.yaml`'s `execution.entry_type: market` (L203) overrides a
deep-limit setup and creates a geometry mismatch. The audit shows the opposite problem:

**The `limit` branch in the executor is unreachable for every setup the builder produces.**

- `executor.py:468`: `entry_type = ((s.get("entry_style") or self.x.get("entry_type")) or "market")`
- The builder sets `"entry_style"` to **`"retrace"`** or **`"close"`** (`setup_builder.py:246`)
  for rejection setups — **never `"limit"`**.
- Zone setups set **no `entry_style` at all** → they fall through to `cfg["execution"]["entry_type"]`.
- `executor.py:480` then tests `if entry_type == "limit":` — which is **never true**.

So even with `execution.entry_type: limit` in config, the executor would still send
**market** orders for zone setups, because `entry_style` is absent and the string
comparison is against `"limit"` while the builder only ever emits `"retrace"`/`"close"`.

> **The bug that matters:** `executor.py:483` sends a limit at `entry` — the deep retest
> level — **only** if `entry_type == "limit"`. Since that branch is dead, zone setups
> that *do* clear the verdict gates would be fired **at market** into a move that has
> already run `0.3–1.0 ATR` past the zone. Combined with `execution.min_score: 44`
> (config L210) but `GATE_DEFAULTS = {"min_score": 70.0}` (`executor.py:32`), and
> `GATE_DEFAULTS["min_ml_prob"] = 0.50` vs config `0.41`, the gate thresholds in the
> **dashboard/`status()`** view and the ones **actually enforced** differ.

### 6.7 ⚠️ NEW — the journal and the ML trainer disagree about what a "fill" is

`retest_buffer_atr: 0.20` gives a limit a **0.20 ATR tolerance** — a fill is registered
when price comes *near* the entry, not at it (`labeler.py:33`, `outcomes.py:71`).
Auditing the 206 resolved fills against raw candles:

- **62 / 206 (30.1%) of journal "fills" never actually traded at the entry level** — the
  bar only came within 0.2 ATR of it.
- Re-resolving the **same 668 rows** with a **strict** fill (price must touch `entry`):

| Fill model | Fill rate | WR | Expectancy |
|---|---:|---:|---:|
| With the 0.20 ATR retest buffer (current) | 75.0% | 37.2% | **+0.080R** |
| Strict (price must trade at `entry`) | 73.6% | 35.9% | **+0.034R** |

So the tolerance is worth **~+0.046R** of apparent expectancy, and the current live
execution path (`executor.py:483`, a real `BUY_LIMIT` at `entry`) has **no tolerance at
all** — i.e. the live bot faces the strict column, while the journal reports the
tolerant one. **This is the single most concrete "in reality it seldom happens" gap:
the backtest fills that the broker would not.**

Also note: `entry_valid_bars` is **never written to the journal payload**
(`store.py:240–245` stores 14 keys — not `setup_kind`, `entry_style`, `entry_valid_bars`,
or `rejection`). So `outcomes.py:63` (`payload.get("entry_valid_bars")`) **always falls
back to the global 24 bars** — the rejection family's own 8-bar RET window is silently
ignored at resolution time. Verified: re-resolving the 29 `rejection_bar` rows at 8 vs
24 bars yields identical results *for this sample* (all 29 fill either way), so the bug
is currently latent — but it will bite as soon as a rejection goes unfilled.

---

## 7. Revised Recommendation (what to actually do)

The draft's 5 proposals are re-scored below, then replaced by a plan ordered by
**measured impact per unit of risk**.

### 7.1 Scoring of the draft's proposals

| Draft proposal | Verdict | Reason |
|---|---|---|
| 1. Tighten retest windows / cap distance at `0.55 ATR` | ❌ **Do not do** | `> 0.55 ATR` bucket has **+0.322R** expectancy (n=72) in the new regime; capping removes profitable setups (§6.3). `entry_valid_bars: 12` was already measured to **hurt** (`aa3834c`). |
| 2. Near-zone market trigger + OB penetration buffer | ⚠️ **Defer** | The mechanism is sound but the `0.10–0.25 ATR` band is the **weakest** bucket (+0.139R); sizing this on `dist_atr` is not supported. |
| 3. `momentum_continuation` family | ⚠️ **Defer** | No evidence yet that continuation entries have positive expectancy; and the ML model (AUC `0.561`) is too weak to score a new family. Needs to come *after* the model is fixed. |
| 4. Rejection close-chase on momentum | ❌ **Do not do** | Directly contradicts the project's own tick-exact verdict (`docs/rejectionproto-research.md`, DEC-034: the momentum close-chase edge was **voided** as a Python-bug artifact). The real levers are `trigger_lookback_bars` (2 → 4–6, ~2.6× trigger rate) and the directional-sweep requirement, **not** `retrace_pct` (§6.4). |
| 5. Execution geometry guard | ⚠️ **Wrong diagnosis, right instinct** | The mismatch is real but **inverted**: the executor's `limit` branch is **dead code** (§6.6). Fixing it means making the limit path reachable — after which the drift re-calculation *does* become necessary. |

### 7.2 The plan that replaces §5

**Phase A — Make the engine's own numbers match the broker (no new features, no retrain).**
This is where the user's complaint is literally answered: the backtest fills that the
broker would not, and the dashboard badge that renders a wait-state as the only state.

| # | Change | File | Why it is first |
|---|---|---|---|
| A1 | **Close the fill-fidelity gap**: decide one fill semantics and use it in the labeler, the resolver, *and* the executor. Either (a) place the MT5 limit at `entry ± retest_buffer` so the broker fills what the model assumes, or (b) set `retest_buffer_atr: 0` and let the model learn the strict reality. Today the model is trained on tolerance fills and executed on strict ones (§6.7). | `config.yaml`, `app/execution/executor.py` | Worth ~`0.046R` and removes a systematic train/live skew. |
| A2 | **Make the `limit` branch reachable**: set `entry_style: "limit"` on zone setups (or compare against the `entry_style` values the builder actually emits). Today **every** zone setup would be fired at market into a move that already ran past the zone (§6.6). | `app/engine/setup_builder.py`, `app/execution/executor.py` | Correctness bug; no signal can be trusted until fixed. |
| A3 | **Split the two `entry_valid_bars` semantics** into `zone_max_age_bars` (zone freshness, per-family) and `entry_valid_bars` (fill window). Give OBs a separate age budget — they are structurally old (median origin age **137 bars** vs FVG's **13**), so a shared 24-bar budget kills 75% of them (§6.5). | `app/engine/setup_builder.py`, `config.yaml` | This is the actual cause of 21/22 symbols emitting nothing. |
| A4 | **Align the enforced gates with the displayed ones**: `GATE_DEFAULTS` says `min_score: 70.0` / `min_ml_prob: 0.50`; config says `44` / `0.41`; `status()` reports the defaults. Pick one and make the dashboard, the CLI, and `_gates()` agree. | `app/execution/executor.py`, `app/dashboard/server.py` | The operator cannot see the real thresholds. |
| A5 | **Persist the missing journal keys** (`setup_kind`, `entry_style`, `entry_valid_bars`, `rejection`) so the Perf tab can actually segment families and `outcomes.py:63` can honor per-setup windows. | `app/data/store.py` | Currently `setup_kind` is `MISSING` on **all 668** rows — every family split in §2.2 had to be reconstructed from `entry_zone.type`. |

**Phase B — Restore model signal, then re-tune entry styles.**

| # | Change | Why |
|---|---|---|
| B1 | **Fix the ML model's probability plateau**: `56 / 664` rows share the *identical* `ml_prob = 0.411` (a single degenerate leaf), and `82 / 664` clear `0.41`. `cv_auc = 0.561` (baseline `data/ml_baseline.json`) is barely above chance, and the top feature importance is `rr` — a *design parameter*, not a market signal. | `app/ai/ml_model.py`, `app/ai/features.py` |
| B2 | **Recalibrate the verdict threshold against the *new-regime* distribution.** In `id >= 2132`, only `6.6%` of setups reach `score >= 44` and only `18` rows exist. But the *per-verdict* expectancy is inverted: **`AVOID` n=67 → +0.409R (WR 50.7%)** vs `BUY` n=5 → **−0.294R** vs `WAIT` n=403 → **+0.041R**. Trading the setups the LLM *rejects* has been better than trading the ones it approves. | `app/ai/hybrid.py`, `config.yaml` |
| B3 | **Only then** re-test draft Proposals 2 and 3 (near-zone market entry, continuation family) against the Phase-A engine, with the new regime's data as the baseline. | — |

**Phase C — Report honestly.** The Perf tab's `by_verdict` panel should show the
new/old regime split, the strict-vs-tolerant fill rate side by side, and the
`ml_prob` plateau count — so the next "why does it never trade" question is answered
from the dashboard instead of from a forensic DB dive.

### 7.3 Guardrails for any follow-up work

- **Do not re-run the `entry_valid_bars` ablation without splitting the semantics**
  (§6.5) — the previous attempt (`aa3834c`) conflated fill-window and zone-age and
  concluded, wrongly, that a tighter window is bad.
- **Do not trust any statistic that pools `id < 2132` with `id >= 2132`** (§6.2).
  Any new figure should be computed on the new regime only, or on a replay harness,
  and reported with its `n`.
- **Treat `n < 30` per bucket as noise.** The `order_block` new-regime sample is
  `n = 12`; the draft's "38.7% unfilled / 17.9% WR" order-block claim has no current
  support.
- **Before any live change, run the engine in `dry_run: true`** (`execution.enabled` is
  `false` and the `trades` table is **empty** — nothing has ever been sent live), and
  confirm the *order type* actually placed. A2 makes that check meaningful for the
  first time.

---

## 8. Third-Party Review (external audit, 2026-11-26)

> Independent review pass. Method: every load-bearing claim in §6–§7 was re-verified
> directly against the source (`setup_builder.py`, `executor.py`, `labeler.py`,
> `outcomes.py`, `store.py`, `order_blocks.py`, `config.yaml`) and against
> `data/trader.db` via SQL. **Verdict: approve with changes** — the §6 self-audit is
> high quality and almost entirely survives verification; two claims need correction
> (§8.2) before §7 is executed.

### 8.1 Independently verified ✅

| Claim (source) | Re-verification | Result |
|---|---|---|
| Regime split at `id 2132` (§6.2) | `setups_history`: 668 rows; exactly **272** carry `entry_distance_atr`, ids `2132..2403` | ✅ exact |
| Old/new regime stats (§6.2) | Old: 393 resolved, expectancy **−0.051R** (exact), unfilled 101/396 = **25.5%**. New: 220 resolved, **+0.283R** (exact), unfilled 14/272 = **5.1%** | ✅ exact |
| §2.2 outcome distribution | `outcomes`: 178 WIN / 300 LOSS / 115 EXPIRED_UNFILLED / 498 filled | ✅ |
| Dual `entry_valid_bars` semantics (§6.5) | zone-age gate `setup_builder.py:327–329` vs fill-window use `labeler.py:35` / `outcomes.py:63–64` | ✅ |
| Journal payload missing keys (§6.7 / A5) | `store.py:240–245` persists exactly 14 keys; no `setup_kind` / `entry_style` / `entry_valid_bars` | ✅ |
| Gate mismatch (§6.6) | config `min_score: 44` (L210) / `min_ml_prob: 0.41` (L211) vs `GATE_DEFAULTS` 70.0/0.50 (`executor.py:32`) | ✅ |
| Nothing has ever traded (§7.3) | `trades` = 0 rows; `execution.enabled: false`, `dry_run: true` | ✅ |
| ml_prob plateau (B1) | **55** rows at `ml_prob = 0.4108…` (doc says 56 at "0.411" — off-by-one/rounding, immaterial); **82/664 > 0.41 exact**; top feature importance is `rr` (0.0595) in `data/ml_baseline.json` | ✅ |
| Close-chase voided (§6.4) | `docs/rejectionproto-research.md` carries the `⚠ CORRECTION (2026-09-26)` banner and the DEC-034 disposition ("should not be built") | ✅ |
| Analysis starvation context (§6.5) | `analysis` table = 67 rows | ✅ |

### 8.2 Discrepancies found

#### D1 ❌ §6.6's closing claim is internally inconsistent — the dead branch is *conditional*, not structural

§6.6 states: *"even with `execution.entry_type: limit` in config, the executor would still
send market orders for zone setups."* This contradicts §6.6's own third bullet. For **zone
setups** (`entry_style` absent — `entry_style` appears only at `setup_builder.py:241`, on
rejection setups), `entry_type` falls through to `cfg["execution"]["entry_type"]` verbatim
(`executor.py:468`; `self.x = cfg["execution"]` at `executor.py:52`). Config `limit`
**would** activate the real limit branch — including the `side_ok → market` fallback
already implemented (`executor.py:480–491`). The branch is dead today **only because
config says `market`**.

The family that can *never* limit is **`rejection`**: the builder emits
`entry_style ∈ {"retrace", "close"}` and neither value is ever mapped to `"limit"`, so
rejection setups take the market branch regardless of config — and the executor's comment
(`executor.py:466–467`: *"retrace → limit into the rejection bar body"*) describes a
mapping that **does not exist anywhere in the codebase**.

**Consequence for A2:** the fix is two-part, and flipping config alone is a trap — it
fixes zone setups while silently leaving rejection setups at market. A2 should: (i) emit
or normalize `entry_style: "limit"` for zone setups (or compare against the values the
builder actually emits), *and* (ii) map `retrace` → limit (or correct the comment). The
dry-run record already logs `order_type` (`executor.py` dry-run branch), so an automated
"placed order type matches setup intent" assertion is nearly free.

#### D2 ❌ §7.2 B2 violates the document's own §7.3 guardrail, and its counts don't reconcile

B2's figures (`AVOID n=67 → +0.409R`, `BUY n=5 → −0.294R`, `WAIT n=403 → +0.041R`) are
computed over the **pooled** population — the expectancies match pooled resolved data
exactly (actual pooled counts: AVOID 78, BUY 5, WAIT 527) — while §7.3 itself forbids
pooling `id < 2132` with `id >= 2132`. The printed counts don't reconcile with the DB
under any join (AVOID 67 vs 78; WAIT 403 vs 527; AVOID WR 50.7% vs 34/71 = 47.9%).

Recomputed **new-regime-only** (the document's own standard):

| Verdict | n (resolved) | Filled | WR (filled) | Expectancy |
|---|---:|---:|---:|---:|
| `AVOID` | 39 | 35 | 60.0% | **+0.770R** |
| `BUY` | 3 | 3 | 33.3% | +0.177R |
| `SELL` | 2 | 2 | 50.0% | +0.305R |
| `WAIT` | 176 | 166 | 39.8% | +0.185R |

In the new regime **the inversion dissolves**: `BUY` expectancy is *positive* (+0.177R)
and every verdict is positive — with n = 2–3 per action verdict, nothing can be concluded
in either direction. B2's *action* (recalibrate verdict thresholds) may still be
defensible, but as written it rests on exactly the kind of statistic §7.3 warns against.
Re-derive it on new-regime data or a replay harness, or park it behind an
"insufficient evidence" label.

#### D3 ⚠️ Minor nits

- B1: the plateau is **55** rows at `ml_prob = 0.4108…`, not "56 at 0.411" — the value
  only *rounds* to 0.411. Immaterial to the conclusion (degenerate leaf either way).
- WR convention is inconsistent across sections: §6.2's new-regime "WR 44.7%" is
  wins/(W+L) = 89/199, while wins/filled-trades = 89/206 = **43.2%**. State the
  denominator once, per §7.3's own "report with its `n`" rule.
- The executor comment vs reality mismatch (`executor.py:466–467`) should be reconciled
  under A2 (see D1).

### 8.3 Assessment of the §7 plan

Phase A's ordering is correct and is the strongest part of the document: every A1–A5
issue was independently confirmed as real, and prioritizing measurement/correctness over
new setup families avoids repeating §5's mistakes. Suggested amendments:

1. **A2 — expand per D1** (two-part fix + order-type assertion in dry-run).
2. **A1 — decide fill semantics with broker constraints in mind.** Option (a) (limit at
   `entry ± retest_buffer`) worsens effective RR by up to the buffer and may collide with
   the broker's minimum stop/fill distance on tight zones. Once real limits exist, draft
   Proposal 5's live drift-recalc becomes relevant again — fold it into A1/A2 rather than
   dropping it entirely.
3. **A3 — set `zone_max_age_bars` defaults from the measured origin-age distributions**
   (FVG median ≈ 13 bars, OB median ≈ 137 bars), and validate against §6.5's own replay
   finding that the age gate is *not* what survives — expect reallocation, not a windfall.
4. **Sequence A5 before any window re-tuning.** Until `entry_valid_bars` / `entry_style`
   are persisted in the journal, the resolver silently uses the global 24-bar window
   (§6.7) and any window experiment is confounded — the exact failure mode of the
   `aa3834c` ablation (§6.5).
5. **B2 — re-derive per D2 before touching thresholds.**
6. **Phase A has no success metrics.** Striking §5's table was right, but nothing
   replaced its discipline. Suggested: A2 = dry-run shows 100% limit orders for zone
   setups and the intended type for rejection setups; A3 = ≥15/22 M15 symbols emit ≥1
   setup in dry-run; A1 = journal records both strict and tolerant fill outcomes during
   the transition.

### 8.4 Verdict

**Approve with changes.** The §6 self-audit is unusually honest and survives independent
code- and DB-level verification almost line-for-line — §6.2's regime split and both
expectancy figures are exact to the third decimal. Two corrections are required before
execution: the §6.6 mechanism claim (D1) and the §7.2 B2 evidence (D2). Neither changes
the Phase-A ordering; both change what its first work items must contain.
