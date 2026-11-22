# Dashboard & Indicator Logic Review

Review of the dashboard (`app/dashboard/server.py`, `app/dashboard/static/index.html`), the SMC/indicator & setup engines (`app/smc/*`, `app/engine/*`, `app/csm.py`, `app/ai/*`), and the pipeline/execution integration (`app/pipeline.py`, `app/execution/executor.py`).

Issues are grouped by **Indicator / SMC / Setup Logic** and **Dashboard Backend & Frontend**, ordered by severity within each section, with root causes and concrete fixes.

---

## Part 1 — Indicator, SMC & Setup Logic Issues

### 1. [High] `FastSMC` vs `analyze()` Parity & Look-Ahead Drift on `rejections` Equal-Highs/Lows Pools
- **Where:** `app/smc/rejection.py` (`detect_rejections` vs `rejection_tail`), `app/smc/fast_context.py` (`FastSMC.at`), `app/smc/__init__.py`
- **What happens:**
  1. In `smc.analyze(df, cfg)`, `pools` is clustered **once** at the tip of `df` (`tol = close[-1] * tol_pct`) over all `confirmed_swings`, and then passed into `detect_rejections(df, pools, ...)`. Replaying historical bars `i = 1..n-1` using `close[-1]` for `tol` introduces subtle end-of-window look-ahead in historical rejection pool matching compared to `FastSMC.at(i)` (which clusters pools causally at each step `i` using `close_i * tol_pct`).
  2. In `rejection_tail`, pool consumption is checked with:
     ```python
     if np.any(c[ready:prior_end] > lvl) or np.any((h[ready:prior_end] > lvl) & (c[ready:prior_end] < lvl)):
         continue
     ```
     Whereas `detect_rejections` only scans `pools` on bars where `rng_pts > 0`. If a zero-range bar (`h == l`) occurs at `m >= ready` with `c[m] > lvl`, `detect_rejections` leaves the pool unconsumed while `rejection_tail` marks it consumed.
- **Proposed Fix:**
  - Compute equal-level pools causally per bar inside `detect_rejections` (matching `detect_sweeps(..., tol_pct=tol_pct)`) instead of passing a static end-of-window `pools` list from `smc/__init__.py`.
  - Guard the `rejection_tail` prior-bar consumption check with `rng_pts[ready:prior_end] > 0` so `FastSMC` and `analyze()` stay bit-identical.

---

### 2. [High] `detect_structure` Skips Unbroken Swings When Another Swing Confirms in Between
- **Where:** `app/smc/structure.py:31-58`
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
- **Proposed Fix:**
  - Maintain `active_high` (latest unbroken confirmed swing high) and `active_low` (latest unbroken confirmed swing low) independently across the chronological bar walk `i = 0..n-1`. Activate swings when `confirmed_at == i`, emit `BOS`/`CHoCH` on close beyond `active_high` or `active_low`, and clear the broken reference so it only fires once.

---

### 3. [High] `skip_already_tested` Falsely Rejects Order Blocks During the Impulse Leg
- **Where:** `app/engine/setup_builder.py:310-317`, `app/smc/order_blocks.py:33-51`
- **What happens:**
  - `find_order_blocks` correctly tracks `left_at` (the first candle whose low is strictly above `top` for bullish OBs) so the impulse candles leaving the OB do not count as mitigation.
  - However, in `build_setups`, `skip_already_tested` scans `range(z["origin_index"] + 1, n)` for `low <= entry` (`entry = min(z["top"], close)`). Because candle `origin_index + 1` opens near `close[origin_index] <= top`, its low is almost always `<= top` before price ever leaves the OB (`left_at`), **disqualifying nearly all Order Block setups immediately**.
- **Proposed Fix:**
  - Start the `skip_already_tested` scan at `(z["left_at"] + 1)` when `z.get("left_at") is not None` (for Order Blocks), falling back to `z["origin_index"] + 1` for FVGs.

---

### 4. [High] `htf_tp_distance_atr` Double-Divides by ATR When No HTF Pool Exists (Breaks Gold / Indices / Crypto)
- **Where:** `app/ai/features.py:103-118`, `148`
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
  - `tp_dist` is initialized to `20.0` (already in ATR units), but line 148 unconditionally divides `tp_dist` by `atr_v`.
  - On FX pairs (`atr_v ~ 0.001`), `20.0 / 0.001 = 20000` clamps back to `20.0`. On instruments where `atr_v > 1.0` (e.g., `XAUUSD` with `ATR = 15.0` or `BTCUSD` with `ATR = 500.0`), `20.0 / 500.0 = 0.04 ATR` — encoding "no HTF pool ahead" as if an HTF target were `0.04 ATR` away.
- **Proposed Fix:**
  - Keep `tp_dist_atr = cap` in ATR units, assign `tp_dist_atr = (min(beyond) - entry) / atr_v` (or `(entry - max(beyond)) / atr_v`) when `beyond` is non-empty, and return `float(min(cap, tp_dist_atr))`.
  - Also remove or register the 3 dead feature keys computed in `make_features` (`is_order_block`, `symbol_setup_density`, `entry_in_htf_zone`) that are missing from `FEATURES`.

---

### 5. [Medium] `compute_dealing_range` Uses Inverted / Stale Swing Brackets Instead of the Active Impulse Leg
- **Where:** `app/smc/pd_zones.py:13-36`, `app/smc/fast_context.py:124-138`
- **What happens:**
  - `compute_dealing_range` pairs the latest swing high and latest swing low independently and swaps `top`/`bottom` if `bottom > top`. In sustained trends, one side can be stale or inverted, and clamping `position` to `[0.0, 1.0]` labels a bullish breakout above the range high as `100% premium` (blocking long entries).
- **Proposed Fix:**
  - Anchor the dealing range to the impulse leg of the most recent structural break (the swing preceding the latest BOS/CHoCH to the highest high / lowest low since that break), falling back to `(last_high, last_low)` only when no structure events exist.

---

### 6. [Medium] Pivot Tie-Break Mismatch Between `find_swings` and `detect_pivots` Drops Equal Highs/Lows
- **Where:** `app/smc/structure.py:5-28` vs `app/smc/rejection.py:113-145`
- **What happens:**
  - `find_swings` requires strict uniqueness `(seg_h == highs[i]).sum() == 1`, discarding double tops/bottoms whose highs/lows are equal within `[i - lookback, i + lookback]`, so `cluster_equal_levels` never sees them. Meanwhile, `detect_pivots` (the chart markers and rejection book) allows equality on the older flank so the newest touch keeps the swing.
- **Proposed Fix:**
  - Use the older-flank-equality tie-break consistently in `find_swings` (`highs[i - k] > phv` and `highs[i + k] >= phv`).

---

### 7. [Medium] `infer_point` Samples Only 3 Bars (Can Mis-Detect Tick Precision by 100x)
- **Where:** `app/smc/rejection.py:66-76`
- **What happens:**
  - Inspecting only `vals[0]`, `vals[len(vals)//2]`, `vals[-1]` fails when those 3 quotes happen to end in trailing zeros (e.g., `1.10500` -> `1.105` -> `0.001` instead of `0.00001`), corrupting integer-point anatomy (`d3`, `depth`, retrace entry rounding).
- **Proposed Fix:**
  - Inspect the last 100 bars across `open/high/low/close` (or pass broker `point` directly from `symbol_trade_info` when available).

---

### 8. [Low] `csm.py` Trailing-Bar Misalignment When One Pair Has a Newer/Older Timestamp
- **Where:** `app/csm.py:45-48`
- **What happens:**
  - `pd.DataFrame(closes).sort_index()` outer-joins 28 pairs on bar open time. If 1 pair has a slightly newer bar than the other 27, `.iloc[-1]` has `NaN` for 27 pairs and computes all 8 currency strengths from that single pair.
- **Proposed Fix:**
  - Apply `close_df = close_df.ffill(limit=2)` and drop trailing rows with fewer than `min_pairs` valid entries before computing z-scores and `.iloc[-1]`.

---

## Part 2 — Dashboard Backend & Frontend Issues

### 9. [High] `NameError: name 'mt5' is not defined` Breaks `/api/csm` Self-Heal Refresh
- **Where:** `app/dashboard/server.py:397-409`
- **What happens:**
  - Inside `csm_data()` -> `_csm_work()`, `pipeline.update_csm(cfg, store, mt5=mt5, demo=demo_mode)` references an undefined variable `mt5` (which only exists in `_bar_watcher`'s local scope). The `NameError` is silently caught by `except Exception: pass`, so `/api/csm` never refreshes stale CSM snapshots.
- **Proposed Fix:**
  - Connect and shut down a local `_mt5` handle inside `_csm_work()` (guarded by a process-wide MT5 IPC lock).

---

### 10. [High] Unsynchronized Concurrent `MetaTrader5` IPC Calls Across `/api/live`, `_bar_watcher`, and `ExecutionEngine`
- **Where:** `app/dashboard/server.py:356-374`, `app/data/mt5_client.py:258-263`
- **What happens:**
  - The `MetaTrader5` Python package uses a single process-global IPC state. `/api/live` runs in FastAPI's threadpool every 5–15s, calling `mt5_client.connect_mt5(...)` (`mt5.initialize()`) without locking or shutting down while `_bar_watcher` and `ExecutionEngine` concurrently call `mt5.*` on daemon threads. This causes IPC races, dropped sessions, and indefinite hangs.
- **Proposed Fix:**
  - Guard all `mt5` module interactions with a shared `threading.RLock()` (or a single thread-safe MT5 session manager) so FastAPI handlers and background loops serialize IPC calls cleanly.

---

### 11. [Medium] `ExecutionEngine.allowed_symbols` Is Frozen at Startup
- **Where:** `app/dashboard/server.py:52-53`, `276-280`
- **What happens:**
  - Updating symbol selection in the dashboard (`POST /api/symbols/selection`, `/select-all`, `/clear`) persists `data/symbol_selection.json`, but `execution_engine.allowed_symbols` still holds the startup list until server restart.
- **Proposed Fix:**
  - Update `execution_engine.allowed_symbols = set(visible_symbols())` in the symbol-selection endpoints (or pass `visible_symbols` as a dynamic getter to `ExecutionEngine`).

---

### 12. [Medium] `liveTick()` Re-renders the Entire Chart Every Few Seconds & Only One Setup's Lines Are Drawn
- **Where:** `app/dashboard/static/index.html:824-836`, `1115-1135`
- **What happens:**
  1. `renderChart()` picks only `(a.setups||[]).find(s => s.verdict==='BUY' || s.verdict==='SELL') || (a.setups||[])[0]`, hiding the second setup's Entry/SL/TP lines whenever both a LONG and SHORT setup are present.
  2. `liveTick()` calls full `renderChart()` on every forming-bar tick, tearing down and recreating all markers/price lines and causing flicker instead of calling `state.series.update(c)`.
  3. `utc_offset` auto-detection in `renderBoxes()` recomputes `Math.round(est/3600)` on every render, which can jitter by ±1h near bar boundaries.
- **Proposed Fix:**
  - Use `state.series.update(c)` in `liveTick()` when updating/appending the live candle.
  - Render price lines for all setups in `a.setups` (labeled `LONG ENTRY` / `SHORT ENTRY` when multiple exist).
  - Cache the auto-detected `utc_offset` once per loaded dataset.

---

### 13. [Medium] HTML Attribute Injection / Broken `onclick` on Symbols With Quotes or Backslashes
- **Where:** `app/dashboard/static/index.html:855`, `1027`, `1192-1193`
- **What happens:**
  - `esc()` escapes `& < > "` for HTML text, not `'` or `\` inside `onclick="gotoSetup('${esc(row.symbol)}', ...)"`. Broker symbols containing `'` or `\` break the handler or inject JS.
- **Proposed Fix:**
  - Use `data-symbol` / `data-tf` attributes and delegated click listeners instead of inline JS string interpolation.

---

### 14. [Low] Journal ML Calibration Chart Drops `> 60%` Setups & Ranked Tab Shows Disabled Symbols
- **Where:** `app/dashboard/static/index.html:950-965`, `app/dashboard/server.py:412-430`, `app/symbol_selection.py:29-55`
- **What happens:**
  1. `loadJournal()` defines `buckets = [[50,60],[40,50],[30,40],[20,30],[10,20],[0,10]]`, silently dropping all setups with `ml_prob > 0.60` from the signal-density breakdown.
  2. `/api/setups/ranked` returns setups for disabled symbols because it doesn't filter by `visible_symbols()`.
  3. `SymbolSelection` never reloads from disk if modified by CLI while the server is running, and reads `self._enabled` without `self._lock`.
- **Proposed Fix:**
  - Expand `buckets` in `loadJournal()` to cover `60–70%`, `70–80%`, `80–100%`.
  - Filter `/api/setups/ranked` with `allowed = set(visible_symbols())`.
  - Acquire `self._lock` and check file `mtime` in `SymbolSelection`.
