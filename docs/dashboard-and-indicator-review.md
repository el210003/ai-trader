# Dashboard & Indicator Logic Review

Review of the dashboard (`app/dashboard/server.py`, `app/dashboard/static/index.html`), the SMC/indicator & setup engines (`app/smc/*`, `app/engine/*`, `app/csm.py`, `app/ai/*`), and the pipeline/execution integration (`app/pipeline.py`, `app/execution/executor.py`).

Issues are grouped by **Indicator / SMC / Setup Logic** and **Dashboard Backend & Frontend**, ordered by severity within each section, with root causes and concrete fixes.

---

## Part 1 — Indicator, SMC & Setup Logic Issues

### 1. [High] ~~`FastSMC` vs `analyze()` Parity & Look-Ahead Drift on `rejections` Equal-Highs/Lows Pools~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_causal_pool_rejection_parity`)
- **Where:** `app/smc/rejection.py` (`detect_rejections`, `RejectionStream`, `rejection_tail`), `app/smc/fast_context.py` (`FastSMC.__init__`, `FastSMC.at`), `app/smc/__init__.py` (`analyze`)
- **What happens:**
  1. In `smc.analyze(df, cfg)`, `pools` is clustered **once** at the tip of `df` (`tol = close[-1] * tol_pct`) over all `confirmed_swings`, and then passed into `detect_rejections(df, pools, ...)`. Replaying historical bars `i = 1..n-1` using `close[-1]` for `tol` introduces subtle end-of-window look-ahead in historical rejection pool matching compared to `FastSMC.at(i)` (which clusters pools causally at step `i` using `close_i * tol_pct`).
  2. In `rejection_tail`, pool consumption is checked with:
     ```python
     if np.any(c[ready:prior_end] > lvl) or np.any((h[ready:prior_end] > lvl) & (c[ready:prior_end] < lvl)):
         continue
     ```
     Whereas `detect_rejections` only scans `pools` on bars where `rng_pts > 0`. If a zero-range bar (`h == l`) occurs at `m >= ready` with `c[m] > lvl`, `detect_rejections` leaves the pool unconsumed while `rejection_tail` marks it consumed.
  3. Even when `FastSMC.at(i)` passes `pools_i` (clustered at `close[i] * tol_pct`) into `rejection_tail`, earlier bars `k < i` in the tail window are tested against pools formed with `close[i]` rather than each bar's own prior close `close[k - 1]`.
- **Proposed Fix (Implementation Details):**
  1. **Add causal pool mode to `detect_rejections` (`app/smc/rejection.py`):**
     - Update the signature to `detect_rejections(df, pools: Optional[List[dict]], atr_val: float, cfg: dict, keep_last: int = 100, *, tol_pct: Optional[float] = None) -> List[dict]`.
     - Maintain `confirmed_swings: List[dict] = []` and `consumed_causal: set[tuple] = set()` across the chronological bar loop `i = 1..n-1` (mirroring `detect_sweeps` in `app/smc/liquidity.py:130-162`).
     - At step 4 (after bar `i` evaluation), when pivot `p = i - n_swing` confirms (`p - n_swing >= 0`), append the confirmed pivot dict (`{"index": p, "time": int(times[p]), "type": "high"|"low", "price": float(phv|plv), "confirmed_at": i}`) to `confirmed_swings`.
     - At step 1a (inside `if rng_pts > 0:`), when `tol_pct is not None`, compute `tol_i = float(c[i - 1]) * float(tol_pct)`, build `causal_pools = active_pools_at(confirmed_swings, tol_i)`, and key consumed pools by `pkey = (p["side"], tuple(p["members"]))` instead of enumeration index `pi` (which shifts as new swings confirm). Keep the existing static `pools` loop only as a fallback when `tol_pct is None` and `pools` is provided.
  2. **Stream causal pool sweeps directly inside `RejectionStream` (`app/smc/rejection.py`):**
     - In `RejectionStream.__init__`, maintain the exact same `confirmed_swings` and `consumed_causal` state across `i = 1..n-1` using `tol_pct = float(cfg.get("eq_tolerance_pct", 0.0006))` and `tol_i = float(c[i - 1]) * tol_pct` inside `if self.rng_pts[i] > 0:`.
     - Append matched `(1_000_000_000 + pi, side, "equal_highs"|"equal_lows", lvl)` entries directly into `rec` (`self.book_swept[i]`) and sort `rec` by sequence.
     - Remove the redundant `pools` scan and `np.any(...)` slice checks from `rejection_tail()`, since `st.book_swept[k]` now holds the exact causal swing + pool sweeps for bar `k` (automatically respecting the `rng_pts[k] > 0` guard).
  3. **Update callers (`app/smc/__init__.py` & `app/smc/fast_context.py`):**
     - In `app/smc/__init__.py:analyze()`, call `detect_rejections(df, None, atr_val, cfg, tol_pct=tol_pct)` (note `liquidity_pools` returned in the context dict still uses the tip-of-window `pools` for downstream setup/feature consumers).
     - In `app/smc/fast_context.py:FastSMC.at()`, call `rejection_tail(st, i, atr_i, self._rej_window)` without passing `pools=pools_i`.
  4. **Verification:**
     - Add/run a parity test comparing `smc.analyze(df.iloc[:i+1], cfg)["rejections"]` against `FastSMC(df, cfg).at(i)["rejections"]` across multi-regime OHLCV slices (including zero-range bars and equal-high/low clusters) to confirm bit-identical output.

---

### 2. [High] ~~`detect_structure` Skips Unbroken Swings When Another Swing Confirms in Between~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_structure_independent_active_swings`)
- **Where:** `app/smc/structure.py:31-79` (`detect_structure`)
- **What happens:**
  ```python
  for k, sw in enumerate(swings):
      start = sw["confirmed_at"]
      end = swings[k + 1]["confirmed_at"] if k + 1 < len(swings) else n
      for i in range(start, end):
          if sw["type"] == "high" and closes[i] > sw["price"]: ...
  ```
  - Only the **single most recently confirmed swing** (`swings[k]`) is monitored during `[swings[k].confirmed_at, swings[k+1].confirmed_at)`.
  - If a swing high confirms at `1.1000`, then a minor higher low confirms at `1.0950` before `1.1000` is broken, the loop switches exclusively to watching `closes[i] < 1.0950` and **completely misses a subsequent close above `1.1000`**.
  - Additionally, if two swings (a high and a low) share the same `confirmed_at` bar `t`, `swings[k]` has `start == end == t` (`range(t, t)` is empty), so the first swing is never evaluated at all.
- **Proposed Fix (Implementation Details):**
  1. **Replace the swing-outer-loop with a single chronological bar walk (`app/smc/structure.py:31-58`):**
     - Maintain `active_high: Optional[dict] = None`, `active_low: Optional[dict] = None`, `ptr = 0` (index into `swings`, which is sorted ascending by `confirmed_at`), `events: List[dict] = []`, and `trend = "neutral"`.
     - For each bar `i` in `range(n)`:
       - Advance `ptr` while `ptr < len(swings) and swings[ptr]["confirmed_at"] <= i`:
         - If `swings[ptr]["type"] == "high"`, set `active_high = swings[ptr]`.
         - Else (`"low"`), set `active_low = swings[ptr]`.
       - Check bullish break: if `active_high is not None and closes[i] > active_high["price"]`:
         - Emit `{"type": "choch" if trend == "bearish" else "bos", "direction": "bullish", "level": float(active_high["price"]), "level_time": int(active_high["time"]), "level_index": int(active_high["index"]), "index": int(i), "time": int(times[i])}`.
         - Set `trend = "bullish"` and `active_high = None` (consumed so the same swing level fires at most once).
       - Check bearish break: if `active_low is not None and closes[i] < active_low["price"]`:
         - Emit `{"type": "choch" if trend == "bullish" else "bos", "direction": "bearish", "level": float(active_low["price"]), "level_time": int(active_low["time"]), "level_index": int(active_low["index"]), "index": int(i), "time": int(times[i])}`.
         - Set `trend = "bearish"` and `active_low = None`.
  2. **Causal parity with `FastSMC`:**
     - Because `active_high` and `active_low` depend solely on `swings` with `confirmed_at <= i` and `closes[:i+1]`, `FastSMC.at(i)` (`[e for e in self.events if e["index"] <= i]`) remains bit-identical to `smc.analyze(df[:i+1])["events"]` without changing `app/smc/fast_context.py`.
  3. **Verification:**
     - Add a unit test with an alternating swing sequence (e.g., swing high at `1.1000` confirmed at bar 8, swing low at `1.0950` confirmed at bar 11, close at `1.1010` at bar 13) asserting that the bullish `BOS`/`CHoCH` at `1.1000` is emitted, and that simultaneous `confirmed_at` swings do not drop the first swing.

---

### 3. [High] ~~`skip_already_tested` Falsely Rejects Order Blocks During the Impulse Leg~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_skip_already_tested_uses_left_at_and_falls_back`)
- **Where:** `app/engine/setup_builder.py:338-360`, `app/smc/order_blocks.py:36-55`
- **What happens:**
  - `find_order_blocks` correctly tracks `left_at` (the first candle `k >= ev["index"]` whose `low > top` for bullish OBs or `high < bottom` for bearish OBs) so the impulse candles leaving the OB do not count as mitigation (`app/smc/order_blocks.py:42-55`).
  - However, in `build_setups`, `skip_already_tested` scans `range(z["origin_index"] + 1, n)` for `low <= entry` (`entry = min(z["top"], close)`). Because candle `origin_index + 1` opens near `close[origin_index] <= top` (and any intermediate candles before `ev["index"]` or `left_at` still overlap `[bottom, top]`), its low is almost always `<= top` before price ever leaves the OB (`left_at`), **disqualifying nearly all Order Block setups immediately**.
  - In addition, when multiple fresh zones exist in `cands`, filtering `skip_already_tested` *after* selecting `cands[0]` (`app/engine/setup_builder.py:322-324`) aborts the entire direction rather than falling back to the next untested candidate zone (e.g., an FVG or secondary OB).
- **Proposed Fix (Implementation Details):**
  1. **Use `left_at + 1` as the retest scan start for Order Blocks (`app/engine/setup_builder.py:387-397`):**
     - Compute the scan start index as:
       ```python
       test_start = (z["left_at"] + 1) if z.get("left_at") is not None else (z["origin_index"] + 1)
       ```
     - Iterate `for k in range(test_start, n):` using numpy array access (`lows = df["low"].values`, `highs = df["high"].values` hoisted outside the loop instead of per-bar `.iat[k]`).
     - Note that `ob_view()` (`app/smc/order_blocks.py:72`) already guarantees `z["left_at"] is not None and z["left_at"] <= n - 1` for every fresh OB, while FVGs (`app/smc/fvg.py`) have no `left_at` key and naturally use `z["origin_index"] + 1` (the candle after the 3-candle gap completes).
  2. **Evaluate `skip_already_tested` per candidate (or before locking `cands[0]`):**
     - Move the retest check into the `cands` loop (using `z_entry = min(z["top"], close)` for long and `z_entry = max(z["bottom"], close)` for short) so that if the highest-scoring OB was already retested, `build_setups` falls back to the next valid candidate zone instead of dropping the setup entirely.
  3. **Verification:**
     - Add a unit test in `tests/` constructing a bullish BOS with an OB at `origin_index = 10`, impulse departure at `left_at = 13`, and clean pullback at bar `16`, verifying `build_setups` emits an active `order_block` setup when `skip_already_tested=True`.

---

### 4. [High] ~~`htf_tp_distance_atr` Double-Divides by ATR When No HTF Pool Exists (Breaks Gold / Indices / Crypto)~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_htf_tp_distance_atr_units`; retrain ML models via `python -m app.pipeline --retrain` if deployed artifacts exist)
- **Where:** `app/ai/features.py:103-141`
- **What happens:**
  ```python
  cap = 20.0
  tp_dist = cap
  if htf_ctx:
      ...
      if beyond:
          tp_dist = min(beyond) - entry   # raw price delta
  ...
  "htf_tp_distance_atr": float(min(cap, tp_dist / atr_v)),
  ```
  - `tp_dist` is initialized to `20.0` (already in ATR units), but line 144 unconditionally divides `tp_dist` by `atr_v`.
  - On FX pairs (`atr_v ~ 0.001`), `20.0 / 0.001 = 20000` clamps back to `20.0`. On instruments where `atr_v > 1.0` (e.g., `XAUUSD` with `ATR = 15.0` or `BTCUSD` with `ATR = 500.0`), `20.0 / 500.0 = 0.04 ATR` — encoding "no HTF pool ahead" as if an HTF target were `0.04 ATR` away.
  - Meanwhile, `feature_vector()` (`app/ai/features.py:159`) imputes `None` with `20.0`, confirming the intended sentinel value for "no HTF pool ahead" is `20.0` ATR.
  - Additionally, `make_features()` computes 3 extra keys (`is_order_block`, `symbol_setup_density`, `entry_in_htf_zone`) that are absent from `FEATURES` (`app/ai/features.py:10-42`) and therefore silently ignored by `feature_vector()` and `ml_model.py` (which keys saved models on `FEATURES` via `data/models/.feature_set` in `app/pipeline.py:412-431`).
- **Proposed Fix (Implementation Details):**
  1. **Keep `tp_dist_atr` consistently in ATR units (`app/ai/features.py:103-118`, `144`):**
     - Rename `tp_dist` to `tp_dist_atr = cap` (in ATR units) and divide by `atr_v` *only* inside the `if beyond:` branches:
       ```python
       cap = 20.0
       tp_dist_atr = cap
       if htf_ctx:
           want_pools = "buyside" if direction == "long" else "sellside"
           pools = []
           for c in htf_ctx.values():
               pools.extend(c.get(want_pools, []))
           if direction == "long":
               beyond = [p for p in pools if p > entry]
               if beyond:
                   tp_dist_atr = (min(beyond) - entry) / atr_v
           else:
               beyond = [p for p in pools if p < entry]
               if beyond:
                   tp_dist_atr = (entry - max(beyond)) / atr_v
       ```
     - Update the return dict entry to `"htf_tp_distance_atr": float(min(cap, max(0.0, tp_dist_atr)))`.
  2. **Clean up or register the 3 unreferenced keys in `app/ai/features.py`:**
     - Either remove the 3 dead keys (`is_order_block`, `symbol_setup_density`, `entry_in_htf_zone`) from `make_features`'s return dict so `set(make_features(...).keys()) == set(FEATURES)` OR explicitly add them to `FEATURES` (note: changing `FEATURES` automatically triggers a full ML training-cache invalidation and model retrain via `data/models/.feature_set` in `app/pipeline.py:431`). Add an assertion `assert set(make_features(...)) == set(FEATURES)` in `scripts/smoke_rejection.py` to prevent future drift.
  3. **Retrain ML model after fixing `htf_tp_distance_atr`:**
     - Existing `data/models/*.joblib` models trained on `XAUUSD` / `BTCUSD` / indices learned inverted feature splits on `htf_tp_distance_atr` (~`0.04` instead of `20.0` when `htf_ctx` is empty); purge cached training rows and retrain (`python -m app.pipeline --retrain`) once applied.
  4. **Verification:**
     - Add a unit test verifying `make_features(..., htf_ctx=None)["htf_tp_distance_atr"] == 20.0` for both `atr_v = 0.001` (FX) and `atr_v = 500.0` (BTCUSD), and `== 2.0` when `min(beyond) - entry == 2.0 * atr_v`.

---

### 5. [Medium] ~~`compute_dealing_range` Uses Inverted / Stale Swing Brackets Instead of the Active Impulse Leg~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_dealing_range_impulse_leg_and_fast_parity`)
- **Where:** `app/smc/pd_zones.py:5-115` (`compute_dealing_range`), `app/smc/__init__.py:33`, `app/smc/fast_context.py:125`
- **What happens:**
  - `compute_dealing_range(df, swings)` pairs the latest confirmed swing high and latest confirmed swing low independently and swaps `top`/`bottom` if `bottom > top` (`app/smc/pd_zones.py:30-33`).
  - In sustained trends, one side can be stale or inverted (e.g., after a bullish BOS breaks above a prior swing high, no new swing high has confirmed yet, so `last_high` is still the *broken* old high while `last_low` is a higher low — or worse, `last_high` is older and below `last_low`). Clamping `position` to `[0.0, 1.0]` labels a bullish breakout trading above the old swing high as `100% premium`, which fails `correct_half = mid <= rng["equilibrium"]` in `build_setups` (`app/engine/setup_builder.py:310`) and blocks trend-continuation long entries.
  - Moreover, `FastSMC.at(i)` (`app/smc/fast_context.py:124-150`) duplicates `compute_dealing_range` inline instead of calling a shared helper, so any fix in `pd_zones.py` must stay in sync with `FastSMC.at(i)`.
- **Proposed Fix (Implementation Details):**
  1. **Anchor the dealing range to the active impulse leg (`app/smc/pd_zones.py`):**
     - Update the signature to `compute_dealing_range(df, swings: List[dict], events: Optional[List[dict]] = None, end_idx: Optional[int] = None) -> dict` (defaulting `end_idx = len(df) - 1`).
     - When `events` is non-empty, inspect the most recent structure event `ev = events[-1]` (note: with Item 2 fixed, `ev` includes `level_index` and `index`):
       - **Bullish impulse (`ev["direction"] == "bullish"`):**
         - Find the lowest confirmed swing low prior to or at `ev["index"]` (specifically the lowest swing low between the previous bullish/bearish structure event and `ev["index"]`, falling back to `df["low"].iloc[ev.get("level_index", 0):ev["index"] + 1].min()` if no swing low exists in that window) — this is the impulse origin (`bottom`, `lo_i`, `lo_t`).
         - Find the running impulse peak from `lo_i` through `end_idx` (`df["high"].iloc[lo_i:end_idx + 1]` argmax) — this is `top`, `hi_i`, `hi_t`.
       - **Bearish impulse (`ev["direction"] == "bearish"`):**
         - Find the highest confirmed swing high between the prior structure event and `ev["index"]` (falling back to `df["high"].iloc[ev.get("level_index", 0):ev["index"] + 1].max()`) — this is the impulse origin (`top`, `hi_i`, `hi_t`).
         - Find the running impulse trough from `hi_i` through `end_idx` (`df["low"].iloc[hi_i:end_idx + 1]` argmin) — this is `bottom`, `lo_i`, `lo_t`.
     - Fall back to the existing `(last_high, last_low)` logic (and 200-bar rolling extremes) only when `events` is empty (`trend == "neutral"`).
  2. **Deduplicate `FastSMC.at(i)` (`app/smc/fast_context.py:124-150` & `app/smc/__init__.py:33`):**
     - Pass `events` from `smc.analyze`: `dealing_range = compute_dealing_range(df, swings, events=events)`.
     - Replace the duplicated dealing-range block in `FastSMC.at(i)` with a direct call `dr = compute_dealing_range(df, swings_i, events=events_i, end_idx=i)` so both paths share the exact same causal implementation.
  3. **Verification:**
     - Add a unit test verifying that right after a bullish BOS to new highs, `equilibrium` sits at the midpoint of `[impulse_low, running_high]`, placing a shallow pullback FVG/OB in `discount` (`position < 0.5`) rather than `100% premium`, and confirming `FastSMC.at(i)["dealing_range"] == smc.analyze(df[:i+1])["dealing_range"]`.

---

### 6. [Medium] ~~Pivot Tie-Break Mismatch Between `find_swings` and `detect_pivots` Drops Equal Highs/Lows~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_find_swings_tie_break_matches_detect_pivots`)
- **Where:** `app/smc/structure.py:5-36` (`find_swings`) vs `app/smc/rejection.py:130-163` (`detect_pivots`)
- **What happens:**
  - `find_swings` (`app/smc/structure.py:15, 20`) requires strict uniqueness across the entire window:
    ```python
    if highs[i] >= seg_h.max() and (seg_h == highs[i]).sum() == 1:
    ```
    When a clean double top or double bottom occurs within `[i - lookback, i + lookback]` (e.g., two bars with identical highs `1.10500` separated by `<= lookback` bars), `(seg_h == highs[i]).sum() == 2`, so **neither bar** is emitted as a swing. Consequently, `cluster_equal_levels` (`app/smc/liquidity.py:11`) never sees tight equal highs/lows, and `detect_structure` cannot break them.
  - Meanwhile, `detect_pivots` (`app/smc/rejection.py:146-150`, used for chart swing markers and the rejection engine's level book) uses the MQL5 EA tie-break: equality allowed on the **older** flank (`h[p - k] <= phv`) and strict inequality on the **newer** flank (`h[p + k] < phv`), so the newest touch keeps the swing reference.
- **Proposed Fix (Implementation Details):**
  1. **Unify `find_swings` with `detect_pivots`'s flank-asymmetric tie-break (`app/smc/structure.py:5-28`):**
     - Replace the `seg_h` / `seg_l` slice-max + count check in `find_swings` with the explicit flank loop from `detect_pivots`:
       ```python
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
       ```
     - Note: because `highs[i + k] >= phv` only checks up to `i + lookback`, the confirmation bar remains `i + lookback` (still 100% causal for `FastSMC`).
  2. **Verification:**
     - Add a unit test with an exact double top (`high[5] == high[7] == 1.10500` with `lookback=3`) verifying that bar `7` (the newer touch) is emitted by `find_swings`, clustered into an `equal_highs` pool by `cluster_equal_levels`, and matches `detect_pivots`.

---

### 7. [Medium] ~~`infer_point` Samples Only 3 Bars (Can Mis-Detect Tick Precision by 100x)~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_infer_point_trailing_window_and_override`)
- **Where:** `app/smc/rejection.py:78-99` (`infer_point`), `app/smc/rejection.py:424-444` (`infer_point_by_bar`), `app/smc/rejection.py:181` (`detect_rejections`), `app/engine/setup_builder.py:123` (`build_rejection_setups`), `app/smc/__init__.py:19` (`analyze`)
- **What happens:**
  - `infer_point(df)` inspects only 3 bars per column (`vals[0]`, `vals[len(vals)//2]`, `vals[-1]`). When those 3 quotes happen to end in trailing zeros (e.g., `1.10500` -> Python `float` `1.105` -> exponent `-3` -> `0.001` instead of `0.00001`), `infer_point` underestimates precision by `10x`–`100x`.
  - That inflates `point` by `100x`, collapsing `body_pts` / `range_pts` to `0` or `1`, breaking D3 decisive-body checks (`body_pts * 3 >= range_pts * 2`), `depth` / `clv` integer percentages, and retrace limit rounding (`c_i - depth_pts * point` in `app/engine/setup_builder.py:145`).
  - Also note `infer_point_by_bar(df)` (`app/smc/rejection.py:383-404`) mirrors `infer_point(df[:i+1])` for `FastSMC`, so any change to `infer_point`'s window must be mirrored in `infer_point_by_bar` to preserve `FastSMC` parity.
- **Proposed Fix (Implementation Details):**
  1. **Expand `infer_point` to inspect a trailing window (`app/smc/rejection.py:78-90`):**
     - Add an optional `point: Optional[float] = None` parameter to `infer_point(df, window: int = 100) -> float` (and accept optional `point: Optional[float] = None` in `detect_rejections`, `build_rejection_setups`, and `smc.analyze` via `cfg.get("point")`).
     - When `point` is not explicitly supplied, scan the last `min(len(df), window)` bars across `open`, `high`, `low`, `close` (e.g., `vals[-window:]` in reverse order, breaking early as soon as `best >= 5` on FX or iterating all `<= 100` bars since `400` `Decimal` exponent checks take `< 0.2ms`).
  2. **Keep `infer_point_by_bar` synchronized for `FastSMC` (`app/smc/rejection.py:383-404`):**
     - Update `infer_point_by_bar(df, window: int = 100)` so `best[i]` is the running max decimal exponent over `D[max(0, i + 1 - window) : i + 1]` (via `pd.Series(D).rolling(window, min_periods=1).max().to_numpy()`), ensuring `point_by_bar[i] == infer_point(df.iloc[:i+1], window=window)` for every bar `i`.
  3. **Pass exact broker `point` where available (`app/pipeline.py` / `app/engine/setup_builder.py`):**
     - When `symbol_trade_info` (`point` from MT5 `symbol_info`) is available in the pipeline/config, pass `point` through `cfg` or directly into `analyze()` / `build_rejection_setups()` so live execution never relies on decimal-string heuristics.
  4. **Verification:**
     - Add a unit test with a 5-digit FX DataFrame whose first, middle, and last bars all end in `.xxx00` (`1.10500`) while bar `-2` is `1.10512`, asserting `infer_point(df) == 1e-5` and `infer_point_by_bar(df)[-1] == 1e-5`.

---

### 8. [Low] ~~`csm.py` Trailing-Bar Misalignment When One Pair Has a Newer/Older Timestamp~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_csm_trailing_bar_alignment`)
- **Where:** `app/csm.py:42-81` (`compute_tf_strength`), `app/csm.py:132-135` (`update_csm`)
- **What happens:**
  - `compute_tf_strength` builds `close_df = pd.DataFrame(closes).sort_index()`, which outer-joins up to 28 pairs on bar open timestamp (`time`).
  - During live broker ticks or right at a timeframe rollover (e.g., H1/H4 open), 1 or 2 liquid pairs (such as `EURUSD` or `USDJPY`) often receive the new bar a few hundred milliseconds before minor crosses (`NZDCHF`, `CADCHF`). On that newest timestamp row, `close_df.iloc[-1]` has valid values for 1–2 pairs and `NaN` for the remaining 26–27 pairs.
  - Because `pd.concat(cols, axis=1).mean(axis=1)` (`app/csm.py:64`) skips `NaN`s, `ser.iloc[-1]` computes all 8 currency strengths from whichever 1–2 pairs happened to tick first, causing violent false spikes and spurious cross-TF alignment `transitions` in `update_csm`.
- **Proposed Fix (Implementation Details):**
  1. **Require a minimum cross-sectional pair coverage on trailing bars (`app/csm.py:45-55`):**
     - Update `compute_tf_strength` to accept `min_pairs: int = 10` (passed from `update_csm` at `app/csm.py:126` where `min_pairs` is already loaded from config).
     - After `close_df = pd.DataFrame(closes).sort_index()`, forward-fill small single-bar gaps (`close_df = close_df.ffill(limit=1)`) and drop any rows where fewer than `min_pairs` pairs have non-null closes:
       ```python
       close_df = close_df.ffill(limit=1)
       valid_counts = close_df.notna().sum(axis=1)
       close_df = close_df[valid_counts >= max(1, min_pairs)]
       ```
     - This cleanly drops any incomplete tip bar where only a couple of pairs have rolled over while preserving normal historical bars.
  2. **Guard against trailing `NaN` in smoothed currency series (`app/csm.py:64-69`):**
     - Use `ser_clean = ser.dropna()` before indexing `ser_clean.iloc[-1]` (returning `{}` if `ser_clean.empty`) so a currency whose pairs all lacked data on the final bar never writes `float("nan")` into `strength[tf]`.
  3. **Verification:**
     - Add a unit test in `tests/` passing 28 pairs aligned through `t = 1000` plus 1 pair (`EURUSD`) with an extra bar at `t = 1001`, verifying `compute_tf_strength(..., min_pairs=10)` evaluates at `t = 1000` across all 28 pairs rather than computing 8 strengths from `EURUSD` alone.

---

## Part 2 — Dashboard Backend & Frontend Issues

### 9. [High] ~~`NameError: name 'mt5' is not defined` Breaks `/api/csm` Self-Heal Refresh~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_csm_endpoint_self_heal_no_nameerror`, using `mt5_client.mt5_session`)
- **Where:** `app/dashboard/server.py:420-436` (`csm_data` -> `_csm_work`)
- **What happens:**
  - Inside `csm_data()` -> `_csm_work()`, `pipeline.update_csm(cfg, store, mt5=mt5, demo=demo_mode)` references an undefined variable `mt5` (which only exists in `_bar_watcher`'s local scope at `app/dashboard/server.py:85`).
  - Every time the CSM snapshot exceeds `max_age` when `_bar_watcher` or `auto_run` is inactive, `_csm_work()` immediately raises `NameError: name 'mt5' is not defined`, which is silently swallowed by `except Exception: pass`. As a result, `/api/csm` never self-heals stale CSM data.
- **Proposed Fix (Implementation Details):**
  1. **Connect/close a local MT5 session inside `_csm_work()` (`app/dashboard/server.py:422-434`):**
     - Acquire the shared process-wide MT5 IPC lock (`mt5_client.mt5_session(cfg["mt5"], demo=demo_mode)` from Item 10) inside `_csm_work()`:
       ```python
       def _csm_work():
           try:
               with mt5_client.mt5_session(cfg["mt5"], demo=demo_mode) as _mt5:
                   pipeline.update_csm(cfg, store, mt5=_mt5, demo=demo_mode)
           except Exception as e:
               debug(f"[csm] background refresh failed: {e}")
           finally:
               _csm_lock.release()
       ```
  2. **Log failures instead of silent `pass`:**
     - Log caught exceptions via `debug(...)` so terminal/IPC failures during self-heal refreshes are visible in diagnostics.
  3. **Verification:**
     - Add a unit test mocking `store.load_csm` (stale timestamp), `mt5_client.connect_mt5`, and `pipeline.update_csm`, calling `GET /api/csm` and verifying `pipeline.update_csm` is invoked with a valid `mt5` handle (no `NameError`).

---

### 10. [High] ~~Unsynchronized Concurrent `MetaTrader5` IPC Calls Across `/api/live`, `_bar_watcher`, and `ExecutionEngine`~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_mt5_lock_serializes_concurrent_calls`)
- **Where:** `app/dashboard/server.py:92-235` (`_bar_watcher`), `app/dashboard/server.py:390-412` (`live`), `app/dashboard/server.py:432-442` (`_csm_work`), `app/data/mt5_client.py:16-123` (`MT5_LOCK`, `mt5_session`, `connect_mt5`, `fetch_ohlcv`, `shutdown_mt5`), `app/execution/executor.py:249-290` (`ExecutionEngine.scan_once`, `status`, `flatten`)
- **What happens:**
  - The `MetaTrader5` Python C-extension wraps a single process-global IPC pipe to the MT5 terminal; `import MetaTrader5 as mt5` returns the exact same module singleton across all threads.
  - Currently:
    - `_bar_watcher` holds `mt5 = connect_mt5(...)` on a daemon thread and calls `peek_last_bar_time`, `fetch_ohlcv`, `update_csm`, and `shutdown_mt5` without any lock.
    - `/api/live` (`app/dashboard/server.py:388`) runs in FastAPI's AnyIO worker threadpool every 5–15s, calling `mt5_client.fetch_ohlcv(..., mt5=None)` which invokes `connect_mt5(...)` (`mt5.initialize()`) concurrently with `_bar_watcher` and never releases/locks IPC.
    - `ExecutionEngine` (`app/execution/executor.py`) runs `scan_once()` on its own `mt5-execution` daemon thread, calling `mt5.initialize()`, `mt5.symbol_info_tick()`, `mt5.positions_get()`, and `mt5.order_send()` concurrently.
  - Interleaved `initialize()` / `shutdown()` / `copy_rates_*` / `order_send()` calls across threads corrupt IPC state, cause intermittent `(-10004, 'No IPC connection')` errors, or deadlock inside the MT5 DLL.
- **Proposed Fix (Implementation Details):**
  1. **Introduce a re-entrant IPC lock and context manager in `app/data/mt5_client.py`:**
     - Add a module-level `MT5_LOCK = threading.RLock()` and a shared connection state `_SHARED_MT5 = None`.
     - Implement `@contextmanager def mt5_session(cfg_mt5: dict, demo: bool = False):`:
       - When `demo` is `True`, `yield None` without acquiring `MT5_LOCK`.
       - Otherwise, acquire `MT5_LOCK`, lazily call `connect_mt5(cfg_mt5)` if `_SHARED_MT5 is None`, yield `_SHARED_MT5`, and on fatal IPC error reset `_SHARED_MT5 = None` via `shutdown_mt5` inside the lock (avoiding `mt5.shutdown()` on normal short requests like `/api/live`).
     - Wrap `connect_mt5`, `fetch_ohlcv`, `fetch_ohlcv_mt5`, and `shutdown_mt5` in `with MT5_LOCK:` so even direct callers are serialized.
  2. **Serialize all MT5 callers through `MT5_LOCK` / `mt5_session`:**
     - **`app/dashboard/server.py` (`/api/live`):** Replace `mt5_client.fetch_ohlcv(..., mt5=None)` with `with mt5_client.mt5_session(cfg["mt5"], demo=demo_mode) as _mt5: df = mt5_client.fetch_ohlcv(..., mt5=_mt5)`.
     - **`app/dashboard/server.py` (`_bar_watcher`):** Guard the MT5 IPC phases (peek loop, Phase 1 `fetch_ohlcv` batch, and `update_csm`) with `with mt5_client.MT5_LOCK:`, releasing the lock during Phase 2 CPU analysis (`pool.map` / `refresh_pair` with `mt5=None` since candles are already upserted).
     - **`app/execution/executor.py` (`ExecutionEngine.scan_once`):** Wrap the MT5 broker calls inside `scan_once()` (`_ensure_mt5`, `symbol_info`, `positions_get`, `order_send`) with `with mt5_client.MT5_LOCK:`.
  3. **Verification:**
     - Add a concurrency test spawning 8 threads calling `mt5_session` + `fetch_ohlcv` and simulated `scan_once` against a mock `MetaTrader5` module that asserts single-thread occupancy (raises `AssertionError` if two threads enter `copy_rates_from_pos` / `order_send` simultaneously).

---

### 11. [Medium] ~~`ExecutionEngine.allowed_symbols` Is Frozen at Startup~~ *(Fixed)*
- **Status:** Implemented & verified (`scripts/smoke_rejection.py` — `test_execution_engine_dynamic_allowed_symbols`)
- **Where:** `app/dashboard/server.py:53`, `app/execution/executor.py:52-54`, `232-246` (`_current_allowed_symbols`, `_candidates`)
- **What happens:**
  - `ExecutionEngine.__init__` converts `allowed_symbols` into a static `set(allowed_symbols)` once when `create_app` boots (`app/dashboard/server.py:53`).
  - When the user enables or disables symbols in the dashboard settings modal (`POST /api/symbols/selection`, `POST /api/symbols/select-all`, `POST /api/symbols/clear`), `selection` persists to `data/symbol_selection.json`, and `_bar_watcher` immediately switches to the updated `visible_symbols()`, **but `execution_engine.allowed_symbols` stays frozen at its startup set** until the server is restarted (and if `clear_selection` empties the selection, `visible_symbols()` reverts to all `resolved` symbols per `SymbolSelection.enabled_list` policy while `execution_engine.allowed_symbols` still holds the old subset).
- **Proposed Fix (Implementation Details):**
  1. **Support a callable `allowed_symbols` getter in `ExecutionEngine` (`app/execution/executor.py:48-55`, `228-243`):**
     - In `ExecutionEngine.__init__`, store `self._allowed_symbols_fn = allowed_symbols if callable(allowed_symbols) else None` and `self.allowed_symbols = set(allowed_symbols) if (allowed_symbols and not callable(allowed_symbols)) else None`.
     - Add a helper method `_current_allowed_symbols(self) -> Optional[set]`:
       ```python
       def _current_allowed_symbols(self):
           if self._allowed_symbols_fn is not None:
               syms = self._allowed_symbols_fn()
               return set(syms) if syms else None
           return self.allowed_symbols
       ```
     - Call `allowed = self._current_allowed_symbols()` once at the top of `_candidates()` (`app/execution/executor.py:234`) instead of reading `self.allowed_symbols` directly.
  2. **Pass `visible_symbols` (callable) from `create_app` (`app/dashboard/server.py:53`):**
     - Change `ExecutionEngine(cfg, store, allowed_symbols=visible_symbols(), ...)` to `ExecutionEngine(cfg, store, allowed_symbols=visible_symbols, ...)` so every scan dynamically reflects both dashboard updates and external CLI edits to `data/symbol_selection.json` (once Item 14's `mtime` reload is in place).
  3. **Verification:**
     - Add a unit test initializing `ExecutionEngine` with a mutable list getter `lambda: current_syms`, mutating `current_syms` at runtime, and verifying `_candidates()` immediately includes/excludes setups for the updated symbol set.

---

### 12. [Medium] `liveTick()` Re-renders the Entire Chart Every Few Seconds & Only One Setup's Lines Are Drawn
- **Where:** `app/dashboard/static/index.html:609-620` (`renderBoxes` `utc_offset`), `836-845` (`renderChart` price lines), `1620-1636` (`liveTick`)
- **What happens:**
  1. `renderChart()` (`app/dashboard/static/index.html:836-841`) picks only `(a.setups||[]).find(s => s.verdict==='BUY' || s.verdict==='SELL') || (a.setups||[])[0]`, hiding the second setup's Entry/SL/TP lines whenever both a `long` and `short` setup are present on the same pair/timeframe.
  2. `liveTick()` (`app/dashboard/static/index.html:1620-1636`) calls full `renderChart()` on every forming-bar tick (`setData`, `clearPriceLines`, `setMarkers`, `renderBoxes`), tearing down and recreating all chart overlays every 5–15 seconds and resetting pan/zoom interactions instead of calling `state.series.update(c)`.
  3. `utc_offset` auto-detection in `renderBoxes()` (`app/dashboard/static/index.html:613-619`) recomputes `Math.round(est/3600)` on every pan, zoom, resize, and live tick. When `est` straddles a 0.5h boundary or when a new bar opens right before `Date.now()` advances, `offH` can jitter by `±1h` mid-session, shifting all session bands and `0:00` midnight lines.
- **Proposed Fix (Implementation Details):**
  1. **Incremental candle updates in `liveTick()` (`app/dashboard/static/index.html:1620-1636`):**
     - Instead of calling `renderChart()` when `changed` is true, update LightweightCharts incrementally via `state.series.update({time: c.time, open: c.open, high: c.high, low: c.low, close: c.close})` for each updated/new bar `c` (note: if `cs.length > 500`, shift `cs` in memory, and only fall back to `renderChart()` if `c.time < cs[cs.length - 1].time` out-of-order).
     - Call `renderBoxes()` only if a new bar timestamp was appended (`newBarAppended === true`) so session bands extend to the new bar without rebuilding markers or price lines on intrabar price ticks.
  2. **Render price lines for every setup in `a.setups` (`app/dashboard/static/index.html:836-841`):**
     - Loop over `const setups = a.setups || []`; when `setups.length > 1`, prefix labels with `${s.direction === 'long' ? 'LONG ' : 'SHORT '}` (`LONG ENTRY` / `SHORT ENTRY`, `LONG SL` / `SHORT SL`, `LONG TP` / `SHORT TP`) so opposing or dual setups are both visible and unambiguous.
  3. **Cache auto-detected `utc_offset` per loaded dataset (`app/dashboard/static/index.html:543`, `613-619`, `1132`):**
     - Add `utcOffset: null` to `state` (`app/dashboard/static/index.html:543`), reset `state.utcOffset = null` in `loadAnalysis()` when a new dataset arrives, and in `renderBoxes()` compute and store `state.utcOffset` once per dataset when `scfg.utc_offset` is unset.
  4. **Verification:**
     - Load a pair with both a LONG and SHORT setup in the browser and verify both sets of Entry/SL/TP lines render with directional prefixes, intrabar ticks update the right-edge candle smoothly without flickering markers, and panning/zooming never shifts session `0:00` lines.

---

### 13. [Medium] HTML Attribute Injection / Broken `onclick` on Symbols With Quotes or Backslashes
- **Where:** `app/dashboard/static/index.html:783` (`esc`), `876` (`renderStructure` MTF chip `switchTf`), `1457` (`loadRanked` row `gotoSetup`)
- **What happens:**
  - `esc()` (`app/dashboard/static/index.html:783`) only escapes `& < > "` for HTML text nodes, leaving single quotes `'` and backslashes `\` unescaped.
  - Both `renderStructure()` (`onclick="switchTf('${tf}')"` at line 876) and `loadRanked()` (`onclick="gotoSetup('${esc(row.symbol)}','${esc(row.tf)}')"` at line 1457) interpolate values directly into single-quoted JS string literals inside HTML attributes. Broker symbols containing `'` or `\` (common on custom CFD/futures suffixes) cause a JS `SyntaxError` that breaks click navigation or allows attribute/JS injection.
- **Proposed Fix (Implementation Details):**
  1. **Replace inline `onclick` interpolation with `data-*` attributes and delegated event listeners (`app/dashboard/static/index.html`):**
     - In `renderStructure()` (`line 876`), emit `<span class="mtf-chip ${cls}${cur}" data-tf="${esc(tf)}" title="...">${esc(tf)} ${arr}</span>` and attach a single delegated click listener on `$('structureCard')` (`e.target.closest('[data-tf]')` -> `switchTf(el.dataset.tf)`).
     - In `loadRanked()` (`line 1457`), emit `<div class="rank-row ${heat}" data-symbol="${esc(row.symbol)}" data-tf="${esc(row.tf)}" title="...">` and attach a single delegated click listener on `$('rankedRows')` (`e.target.closest('.rank-row[data-symbol]')` -> `gotoSetup(el.dataset.symbol, el.dataset.tf)`).
     - Also escape `'` (`&#39;`) in `esc()` as defense-in-depth for any single-quoted HTML attributes.
  2. **Verification:**
     - Render a mock ranked row with `symbol: "US500'cash\\1"` in DevTools and verify clicking the row invokes `gotoSetup("US500'cash\\1", ...)` cleanly with zero console errors.

---

### 14. [Low] Journal ML Calibration Chart Drops `> 60%` Setups & Ranked Tab Shows Disabled Symbols
- **Where:** `app/dashboard/static/index.html:1264-1275` (`loadJournal` calibration buckets), `app/dashboard/server.py:436-455` (`ranked_setups`), `app/symbol_selection.py:21-69` (`SymbolSelection`)
- **What happens:**
  1. `loadJournal()` (`app/dashboard/static/index.html:1264`) hardcodes `buckets = [[50,60],[40,50],[30,40],[20,30],[10,20],[0,10]]` with `hi===60?1:0`, silently dropping every setup with `ml_prob > 0.61` from the signal-density histogram.
  2. `/api/setups/ranked` (`app/dashboard/server.py:440`) iterates over all `store.list_analyses()` without filtering by `visible_symbols()`, leaking previously analyzed but currently disabled symbols into the Ranked tab.
  3. `SymbolSelection` (`app/symbol_selection.py:21-69`) caches `self._loaded = True` at startup and never checks `self.path.stat().st_mtime` afterward, so CLI edits to `data/symbol_selection.json` are invisible to a running server. Additionally, `_load()`, `is_enabled()`, `enabled_list()`, and `get()` read/mutate `self._enabled` without acquiring `self._lock`.
- **Proposed Fix (Implementation Details):**
  1. **Cover the full `0–100%` probability range in `loadJournal()` (`app/dashboard/static/index.html:1264-1275`):**
     - Expand `buckets` to `[[80,100],[70,80],[60,70],[50,60],[40,50],[30,40],[20,30],[10,20],[0,10]]` and check `pct >= lo && (hi === 100 ? pct <= 100 : pct < hi)` so high-confidence (`60–100%`) setups are displayed accurately.
  2. **Filter `/api/setups/ranked` to enabled symbols (`app/dashboard/server.py:436-455`):**
     - Compute `allowed = set(visible_symbols())` at the start of `ranked_setups()` and skip entries where `entry["symbol"] not in allowed` before loading payloads or incrementing `pairs`.
  3. **Add `mtime`-based reload and thread-safe locking in `SymbolSelection` (`app/symbol_selection.py:21-69`):**
     - Track `self._mtime: Optional[float] = None`. Add a `_ensure_fresh()` helper (called inside `with self._lock:` by `is_enabled`, `enabled_list`, and `get`) that compares `self.path.stat().st_mtime` (or `None` if missing) against `self._mtime` and reloads `self._enabled` whenever the file was created, modified, or deleted externally.
     - Update `_save()` (already called under `self._lock`) to refresh `self._mtime = self.path.stat().st_mtime` immediately after writing so self-writes do not trigger redundant reloads.
  4. **Verification:**
     - Add unit tests covering: (a) `SymbolSelection` detecting an external JSON write via `mtime` change and concurrent reads/writes under threads; (b) `GET /api/setups/ranked` excluding disabled symbols; and (c) `loadJournal()` bucketing a setup with `ml_prob = 0.85` into `80–100%`.
