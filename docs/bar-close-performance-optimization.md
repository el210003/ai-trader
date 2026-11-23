# Proposal: Bar-Close Watcher & Multi-Symbol Latency Optimization

## 1. Problem Statement

When `serve --on-bar-close` processes a simultaneous bar-close burst across 23–30 symbols on an M15 boundary, the watcher poll cycle can exceed **150–270+ seconds**, triggering the watchdog warning:

```
[debug] processing 30 bar-close pair(s): AUDUSD M15, EURUSD M15, ...
[debug] WARNING: bar watcher has not completed a poll for 149s -- an MT5 call is likely blocking; restart serve.bat to recover
```

### Root Causes Identified in the Current Codebase

1. **Full 3,000-bar MT5 pull on every single bar close (`app/dashboard/server.py:163-169`)**:
   - Even when only 1 candle closed and SQLite already holds 3,000 historical bars, `_bar_watcher` calls `fetch_ohlcv(symbol, tf, cfg["data"]["bars"])` (`3000` bars) sequentially for every triggered pair (`30 × 3,000 = 90,000` bars) while holding `MT5_LOCK`.
   - Each call also runs `store.upsert_candles` with `INSERT OR REPLACE` across all 3,000 rows (`90,000` SQLite row upserts per burst).
2. **Redundant 84-pair MT5 pull inside `update_csm` (`app/csm.py:114-125`)**:
   - Right after the batch finishes, `update_csm` unconditionally fetches `28 CSM_PAIRS × 3 timeframes = 84` MT5 series of `400` bars each (`33,600` bars), **re-fetching** symbols/timeframes that were already pulled milliseconds earlier in Phase 1 and performing another `33,600` SQLite row upserts.
3. **ProcessPool SQLite write contention & un-cached LLM calls (`app/pipeline.py:155-169`, `276-306`)**:
   - Up to 8 worker processes simultaneously open `Store(storage_path)` and write `save_analysis` + `append_setups` to the same SQLite database.
   - Each worker instantiates a fresh `LLMAnalyzer` with an empty in-memory `_cache = {}`—so `ai.llm.cache_ttl: 900` is **never shared across workers or across bar closes**! Every worker makes live HTTP requests to `https://api.minimax.io/v1` (up to `timeout: 45s` per setup), and `_analysis_pool` is created lazily on the *first* bar-close burst, adding Windows process-spawn + `numpy`/`pandas`/`sklearn` import overhead right in the middle of the burst.
4. **Watchdog false-alarm during active batch work (`app/dashboard/server.py:249-256`)**:
   - `watch["last_poll_done"]` is only updated at the *end* of the entire batch (`server.py:245`), so any legitimate multi-symbol batch taking >120s prints `an MT5 call is likely blocking; restart serve.bat to recover` even when MT5 is healthy and phase 2/CSM is running.

---

## 2. Target Latency Budget

| Stage | Current (30 pairs) | Optimized (30 pairs) | Savings |
|---|---|---|---|
| Phase 1: MT5 candle delta pull + SQLite upsert | ~25–60s (`90,000` bars) | **~1.5–3s** (`1,500` bars) | **~95%** |
| Phase 2: SMC + ML (warm ProcessPool + single-writer DB) | ~15–30s (cold pool + DB lock) | **~2–4s** | **~85%** |
| Phase 2b: LLM enrichment (shared cache + higher gate + parallel) | ~60–150s (uncached, `min_ml_prob: 0.30`) | **~2–8s** (cached + `min_ml_prob: 0.42`) | **~90%** |
| Phase 3: `update_csm` (delta pull + reuse fresh DB bars) | ~25–50s (`33,600` bars) | **~1–2s** | **~95%** |
| **Total bar-close cycle** | **150–270s+** | **~6–15s** | **~15–20× faster** |

---

## 3. Detailed Optimization Plan

### 3.1 Incremental (Delta) Candle Pulls in `_bar_watcher` (`app/dashboard/server.py` & `app/data/store.py`)

Instead of pulling `cfg["data"]["bars"]` (`3000`) bars every time a single bar closes:

1. Add a lightweight `Store.latest_candle_time(symbol, tf) -> Optional[int]` helper (`SELECT MAX(time) FROM candles WHERE symbol=? AND tf=?`) and `Store.candle_count(symbol, tf)`.
2. In `_bar_watcher` Phase 1:
   - If `store.candle_count(symbol, tf) >= int(cfg["data"]["bars"]) * 0.9`, fetch only a small overlap window (e.g. `delta_bars = 50` via `dashboard.bar_delta_bars: 50`) instead of `3000` bars—enough to cover any missed ticks/reconnections while reducing MT5 IPC payload and SQLite `upsert_candles` rows by **60×** (`3000 -> 50`).
   - Fall back to the full `cfg["data"]["bars"]` pull only when a pair has fewer than 90% of expected history in SQLite (cold start / newly enabled symbol).

```python
# app/dashboard/server.py (Phase 1)
delta_bars = int(cfg.get("dashboard", {}).get("bar_delta_bars", 50) or 50)
full_bars = int(cfg["data"]["bars"])
have_bars = store.candle_count(symbol, tf)
fetch_n = delta_bars if have_bars >= int(full_bars * 0.9) else full_bars
df = _m5.fetch_ohlcv(symbol, tf, fetch_n, demo=demo_mode, cfg_mt5=cfg["mt5"], mt5=mt5)
store.upsert_candles(df, symbol, tf)
```

---

### 3.2 Incremental & Cache-Aware `update_csm` (`app/csm.py`)

`update_csm` only needs `lookback + ma_length + roc_len` bars (`20 + 10 + 1 = 31` bars, safely covered by `bars: 120` instead of `400`), and SQLite already holds recent history for most of the 28 FX pairs:

1. **Check SQLite freshness before calling MT5**:
   - Query `store.load_candles(pair, tf)` first; if the latest stored bar is already within 1 bar interval of `now` (e.g. just updated by `_bar_watcher` Phase 1), skip the MT5 `fetch_ohlcv` call completely.
   - Otherwise, if SQLite already has `>= bars` rows, pull only `delta_bars = 30` from MT5 and upsert, then read `df.tail(bars)` from SQLite.
2. **Lower default `csm.bars` from `400` to `120`** in `app/config.py` and `config.yaml` (still ~4× the `lookback + ma_length` requirement).

---

### 3.3 Pre-Warm `_analysis_pool` at Startup & Eliminate Cross-Process SQLite Write Contention (`app/pipeline.py` & `app/dashboard/server.py`)

1. **Pre-warm the worker pool on server boot (`create_app`)**:
   - Call `pipeline._analysis_pool(cfg)` in a background thread when `bar_close_watcher` starts so Python worker processes and `numpy`/`pandas`/`sklearn` imports are already warm before the first `:00/:15/:30/:45` boundary arrives.
2. **Single-writer SQLite persistence in the parent process**:
   - Have `_analyze_pair_worker` run in **read-only mode** (`save=False` flag on `analyze_symbol`), returning the serialized `analysis` dict to the parent process.
   - The parent process calls `store.save_analysis(...)` and `store.append_setups(...)` sequentially on its single WAL connection—eliminating multi-process SQLite write-lock contention completely.

---

### 3.4 Shared LLM Cache & Burst Concurrency Controls (`app/ai/llm.py`, `app/pipeline.py`, `config.yaml`)

Currently, `LLMAnalyzer._cache` lives in process memory, so every `ProcessPoolExecutor` task starts with an empty cache and `cache_ttl: 900` never hits across bar closes.

1. **SQLite-backed LLM response cache (`llm_cache` table in `app/data/store.py`)**:
   - Persist `(cache_key TEXT PRIMARY KEY, model TEXT, response TEXT, created_at INTEGER)` in SQLite so all worker processes and restarts share `ai.llm.cache_ttl`.
   - Identical setups across consecutive M15 bars return in `<1 ms` with zero network calls.
2. **Raise `ai.llm.min_ml_prob` and tighten `timeout` in `config.yaml`**:
   - Set `min_ml_prob: 0.40` (aligned with `execution.min_ml_prob: 0.41`) so LLM tokens and latency are spent only on setups that are near or above the actionable threshold, rather than low-probability `0.30` setups.
   - Reduce `ai.llm.timeout` from `45`s to `15`s so a slow API response cannot stall a bar-close batch.
3. **Run LLM calls in the parent process with a bounded `ThreadPoolExecutor(max_workers=6)`** after parallel SMC+ML finishes:
   - Prevents 8 worker processes × 4 threads = up to 32 simultaneous requests from hitting MiniMax rate limits at once.

---

### 3.5 Phase-Aware Watchdog Heartbeat (`app/dashboard/server.py`)

Update `watch["phase"]` and `watch["heartbeat"]` at each stage (`peek`, `pull`, `analyze`, `csm`, `telegram`) so the watchdog:
- Distinguishes an actual hung MT5 call (`phase in ("peek", "pull", "csm")` stuck >60s) from normal LLM/analysis work.
- Reports the exact pair and phase that is blocking (`WARNING: bar watcher stuck in phase=pull on EURUSD M15 for 65s`).

---

## 4. Configuration Tuning Summary (`config.yaml` & `app/config.py`)

```yaml
dashboard:
  bar_close_watcher: true
  bar_poll_seconds: 10
  bar_delta_bars: 50         # NEW: incremental bars pulled on bar close (full pull only on cold start)
  parallel_analysis: true
  analysis_workers: 0        # 0 = auto (CPU cores - 1, capped at 8)

ai:
  llm:
    min_ml_prob: 0.40        # skip LLM for low-probability setups (< 40%)
    timeout: 15              # fail fast on slow LLM responses (was 45)
    cache_ttl: 900           # backed by SQLite so all workers share cached narratives

csm:
  bars: 120                  # 120 bars is 4x lookback+ma_length (was 400)
```

---

## 5. Implementation Checklist & Verification

1. **`app/data/store.py`**: Add `latest_candle_time(symbol, tf)` and `llm_cache` table (`get_llm_cache` / `put_llm_cache`).
2. **`app/config.py` & `config.yaml`**: Add `dashboard.bar_delta_bars: 50`, update `csm.bars: 120`, and tune `ai.llm.min_ml_prob: 0.40` / `timeout: 15`.
3. **`app/dashboard/server.py`**:
   - Use delta bar pulls (`bar_delta_bars`) in `_bar_watcher` Phase 1 when SQLite already has `>= 90%` of `data.bars`.
   - Pre-warm `_analysis_pool(cfg)` on startup.
   - Persist worker results in the parent process and add phase-aware watchdog diagnostics.
4. **`app/csm.py`**: Reuse fresh SQLite candles when already up-to-date and use delta pulls (`30` bars) when history exists.
5. **`app/ai/llm.py` & `app/pipeline.py`**: Wire `LLMAnalyzer` to the shared SQLite `llm_cache` and support `save=False` in `_analyze_pair_worker`.
6. **Verification**:
   - Run `serve --on-bar-close` across all 23–30 symbols and verify in `logs/ai-trader.log` that a 30-pair M15 bar-close burst completes in **<15 seconds** (down from >269s) with zero watchdog warnings and identical SMC/ML/Telegram outputs.
