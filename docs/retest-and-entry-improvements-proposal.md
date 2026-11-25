# Proposal: Solving the "Perpetual Retest Wait" & Expanding Actionable Entry Families

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

| Phase | Scope | Files Touched | Success Metric (300+ Resolved Setups) |
|---|---|---|---|
| **Phase 1 (Config & Guardrails)** | Tighten `max_entry_distance_atr: 0.55`, `entry_valid_bars: 6`, `retrace_pct: 35`, `momentum_close_entry: true`, and add `eff_rr` market guard in `executor.py` | `config.yaml`, `app/engine/setup_builder.py`, `app/execution/executor.py` | Cut `EXPIRED_UNFILLED` from `18.8%` to **`< 8%`**; increase `rejection` share from `4.6%` to `12–15%` |
| **Phase 2 (Near-Zone Market + OB Penetration)** | Add `near_zone_market_atr: 0.20` and `ob_max_penetration: 0.25` | `app/engine/setup_builder.py`, `app/smc/order_blocks.py` | Raise `order_block` fill rate from `61.3%` to **`> 80%`** without degrading filled WR |
| **Phase 3 (Continuation Family + Retrain)** | Implement `_continuation_setups()`, add `is_continuation` feature, bump `BUILDER_VERSION`, retrain ML model, validate in `dry_run: true` | `app/engine/setup_builder.py`, `app/ai/features.py`, `app/ai/ml_model.py` | Positive expectancy (`> +0.15R`) on `continuation` setups and higher daily actionable `BUY/SELL` signal count during London/NY sessions |
