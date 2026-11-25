# LLM Usage & Implementation Review

**Scope:** End-to-end audit of LLM integration (`app/ai/llm.py`, `app/ai/hybrid.py`, `app/pipeline.py`, `app/data/store.py`, `app/dashboard/server.py`, `app/execution/executor.py`, `app/engine/insights.py`, `app/dashboard/static/index.html`, `config.yaml`) and empirical analysis against `data/trader.db` (`668` journaled setups, `478` resolved outcomes).
**Status:** All P0, P1, and P2 improvements implemented & verified.

---

## 1. Executive Summary

The codebase has a clean, modular design for LLM-assisted trade evaluation: an OpenAI-compatible `LLMAnalyzer` (`MiniMax-M3` via `https://api.minimax.io/v1`), ML probability gating (`min_ml_prob: 0.40`), a two-pass bar-close pipeline (fast SMC+ML first, async LLM enrichment second), SQLite + in-memory caching, and weighted hybrid score fusion.

However, inspecting both the implementation and live SQLite telemetry (`data/trader.db`) uncovered **four critical bugs/data-integrity issues**, **four prompt/context engineering flaws**, and **empirical evidence that the current numeric fusion degrades ranking quality**:

1. **Pass-2 persistence bug (`app/data/store.py:263-268`)**: `store.append_setups()` updates `verdict` and `score` on Pass 2, **but never updates `setups_history.payload`**, leaving `score_source="ml"`, `llm_score=null`, and `aligned=true` permanently stale in history and corrupting `insights.py`.
2. **Missing `llm_cache` table in `data/trader.db` + silent error swallowing (`app/ai/llm.py:72-80, 111-115`)**: Cross-worker and cross-restart caching fails silently because `llm_cache` does not exist in the live DB and all exceptions are caught with `pass`.
3. **Two-pass execution race (`app/execution/executor.py:253-264`)**: `ExecutionEngine` does not check `meta.llm_pending`, so it can place an order on a Pass-1 `BUY`/`SELL` setup before Pass-2 LLM enrichment runs.
4. **Numeric score degradation (`app/ai/hybrid.py`)**: Across `n = 290` resolved `ml+llm` setups, standalone `ml_prob` ROC-AUC is `0.5042` and `llm_score` ROC-AUC is `0.5027`, while `hybrid_score` drops to **`0.4699`**. Setups where the LLM contradicted (`aligned=False`) actually won **`50.0%` (`+0.385R`, `n = 70`)** vs **`38.9%` (`+0.128R`, `n = 221`)** when aligned.
5. **Broken context assembly (`app/pipeline.py:24-50`)**: `build_llm_context()` sends the **5 oldest/highest-priced** liquidity pools (often >3,500 bars / ~37 days old), mixes `last_close` dealing-range zone with `entry` range position without explanation, omits `csm_dir`, `setup_kind`, ATR-normalized stop/entry distances, and HTF zones, and ships raw Unix timestamps.

---

## 2. Current Implementation Map

| Layer | File | Responsibility & Current Behavior |
|---|---|---|
| **Client & Parser** | `app/ai/llm.py` | `LLMAnalyzer` posts JSON to `{base_url}/chat/completions` (`MiniMax-M3`, `temperature=0.2`, `timeout=15`), strips markdown fences, parses `{bias, confidence, narrative, invalidation, concerns, verdict}`, caches in memory (max 256) + SQLite (`cache_ttl=900`). |
| **Context Builder** | `app/pipeline.py:24-50` | `build_llm_context(setup, smc, htf_ctx)` serializes symbol, TF, trend, last 3 structure events, last 4 sweeps, last 5 pools, dealing range, setup fields, `ml_win_probability`, `atr`, `last_close`, and `{trend, equilibrium}` per HTF. |
| **Orchestration** | `app/pipeline.py:145-180, 343-405` | Gates calls on `ml_prob is None or ml_prob >= 0.40`. Single-pass in `run_all` / CLI; two-pass (`skip_llm=True` -> `enrich_analysis_llm`) in `server.py` bar-close watcher. |
| **Hybrid Fusion** | `app/ai/hybrid.py:5-61` | Weighted average: `0.75 * (ml_prob * 100) + 0.25 * llm.confidence`, `-5.0` if `llm.bias` opposes direction; forces `AVOID` if `not aligned and ml_prob < 0.42`; `BUY`/`SELL` if `final_score >= 44`. |
| **Persistence** | `app/data/store.py:125-130, 263-268, 559-587` | Stores `llm_cache` (keyed by MD5 hash + model) and journals setups in `setups_history`. |
| **Execution** | `app/execution/executor.py:240-264, 328-343` | Polls `analysis` every `15s` (`fresh_seconds=120`), requires `verdict in ("BUY", "SELL")`, `final_score >= 44`, `ml_prob >= 0.41`. |
| **Diagnostics & UI** | `app/engine/insights.py:166-185`, `app/dashboard/static/index.html:997-1023` | Compares win rate of `aligned=True` vs `aligned=False` in `setups_history`; renders bias, conviction, narrative, invalidation, and concerns on setup cards. |

---

## 3. Empirical Findings from `data/trader.db`

Querying `setups_history` (`668` setups) joined with `outcomes` (`478` resolved `WIN`/`LOSS` setups):

### 3.1 Win Rate & Expectancy by Score Source and Alignment

| Segment | Total | Resolved (`WIN`/`LOSS`) | Win Rate | Avg R |
|---|---:|---:|---:|---:|
| `score_source = ml+llm` | 376 | 290 | **41.7%** | **+0.194R** |
| `score_source = ml` (below gate or LLM skipped) | 287 | 184 | 31.0% | -0.055R |
| `llm_group = aligned` (`aligned=True`) | 287 | 221 | 38.9% | +0.128R |
| `llm_group = contra` (`aligned=False`) | 90 | 70 | **50.0%** | **+0.385R** |
| Controlled `ml_prob >= 0.40` + `aligned=True` | 62 | 51 | 37.3% | +0.051R |
| Controlled `ml_prob >= 0.40` + `aligned=False` | 13 | 12 | **41.7%** | **+0.077R** |
| Controlled `ml_prob >= 0.40` + `no_llm` (LLM failed/timed out) | 25 | 12 | 16.7% | -0.543R |

### 3.2 Discrimination & Calibration Findings

- **Zero standalone ranking lift from `llm.confidence`**: On the `n = 290` resolved `ml+llm` subset:
  - `ml_prob` ROC-AUC: **`0.5042`**
  - `llm.confidence` ROC-AUC: **`0.5027`**
  - `hybrid.final_score` ROC-AUC: **`0.4699`** (combining the two hurts ranking)
  - Setups with `llm_score >= 50` won **`36.8%`** (`+0.013R`, `n = 38`) vs **`42.3%`** (`+0.216R`, `n = 253`) when `llm_score < 50`.
- **Confidence scale mismatch suppresses valid ML setups**: Historical `llm_score` has `p25 = 25`, `median = 32`, `p75 = 38`, `mean = 34.6` (`n = 377`). Because Platt-calibrated `ml_prob` caps around `0.485` (`48.5`), an **aligned** LLM response with median confidence (`32`) drags a `44%` ML setup down to `0.75 * 44 + 0.25 * 32 = 41.0 < 44` (`WAIT`). In `setups_history`, **`44` setups had `ml_prob >= 0.41` AND `aligned = True` yet were demoted to `WAIT` (`score < 44`) by low LLM confidence**.
- **Contrarian bias persistence**: Even after Phase 4 prompt edits, `MiniMax-M3` still tends to rate pullback/retest entries against the immediate impulse leg (`aligned=False` wins `50.0%` overall, `50.8%` in recent setups `id > 500`).

---

## 4. Bugs & Architectural Issues (Prioritized)

### Critical — Data Integrity & Execution Safety *(P0 — Implemented & Verified)*

#### 1. `store.append_setups()` Corrupted `setups_history.payload` on Pass 2 *(FIXED)*
- **Location:** `app/data/store.py:263-269`
- **Mechanism:** In bar-close mode, Pass 1 (`skip_llm=True`) inserted a new setup into `setups_history` with `score = ml_prob * 100` and `payload` containing `{"score_source": "ml", "llm_score": null, "aligned": true}`. When Pass 2 (`enrich_analysis_llm`) finished seconds later and called `store.append_setups()`, the `if ex:` branch updated `verdict`, `score`, `ml_prob`, `close`, and `logged_at`, **omitting `payload`**. Historical rows (e.g., `id=2386`, `id=2392`) retained stale `score_source: "ml"`, `llm_score: null`, and `aligned: true`, skewing `app/engine/insights.py:167-184`.
- **Fix Applied:** Added `payload=?` (`r[15]`) to the `UPDATE setups_history` statement in `store.append_setups()`.

#### 2. `llm_cache` Table Missing in Live `data/trader.db` + Silent Exception Swallowing *(FIXED)*
- **Location:** `app/ai/llm.py:75-86, 114-126` and `app/data/store.py:125-138`
- **Mechanism:** `data/trader.db` lacked the `llm_cache` table (opened prior to schema addition) and `LLMAnalyzer.analyze_setup()` swallowed all cache and API exceptions with bare `except Exception: pass`.
- **Fix Applied:** Added `CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_cache(created_at)` in `Store._create()`, migrated `data/trader.db` so `llm_cache` and `idx_llm_cache_created` exist, and added `debug()` logging in `LLMAnalyzer` for SQLite cache errors, unparseable responses, and request exceptions.

#### 3. Two-Pass Race Condition with `ExecutionEngine` *(FIXED)*
- **Location:** `app/execution/executor.py:259-261`, `app/pipeline.py:158-160, 179-180`
- **Mechanism:** Pass 1 saves `analysis` with `meta.llm_pending = True` and an interim `ml`-only `verdict` (`BUY`/`SELL` whenever `ml_prob >= 0.44`). `ExecutionEngine` polled `analysis` every `15s` without inspecting `meta.llm_pending`, risking order placement before Pass-2 LLM enrichment completed.
- **Fix Applied:** Updated `ExecutionEngine._candidates()` to defer analyses where `(payload.get("meta") or {}).get("llm_pending")` is `True`, with a `45s` timeout fallback (`(now - generated_at) <= 45`) so a stalled Pass 2 cannot block execution indefinitely.

#### 4. Dashboard vs. Executor Gate Split-Brain *(FIXED)*
- **Location:** `config.yaml:141, 208` and `app/config.py:71`
- **Mechanism:** `ai.llm.min_ml_prob` was `0.40` while `execution.min_ml_prob` was `0.41`, allowing setups in `[0.40, 0.41)` (e.g., `id=2400 USDJPY`, `ml=0.399`, `score=44.4`) to show `BUY` on the dashboard while always being rejected by `ExecutionEngine`.
- **Fix Applied:** Aligned `ai.llm.min_ml_prob` to `0.41` in both `config.yaml` and `app/config.py`.

---

### High — Prompt & Context Engineering Flaws *(P1 — Implemented & Verified)*

#### 5. `build_llm_context()` Sent 37-Day-Old Liquidity Pools Instead of Nearby Active Pools *(FIXED)*
- **Location:** `app/pipeline.py:34-70`
- **Mechanism:** `cluster_equal_levels()` (`app/smc/liquidity.py:39`) sorts `liquidity_pools` ascending by price, so `[-5:]` sent the 5 highest-priced pools across the entire 7,258-bar history (~300 pips away, >3,500 bars old) along with raw `members` bar-index arrays.
- **Fix Applied:** Added `_nearby_active_pools()` to filter `smc["liquidity_pools"]` to un-swept pools within `±5.0 * atr` of `last_close` (nearest 3 buyside + nearest 3 sellside), enriched with `distance_atr` and `age_bars` and stripped of raw `members` arrays.

#### 6. Contradictory Dealing-Range Fields & Raw Unix Timestamps *(FIXED)*
- **Location:** `app/pipeline.py:113-147`
- **Mechanism:** `dealing_range.position` measured `last_close` while `setup.range_position` measured `entry`, and `last_events` / `recent_sweeps` passed raw Unix epochs and internal indices without `bars_ago`.
- **Fix Applied:** Renamed `dealing_range.position` -> `close_range_position` (with `close_zone`) and `setup.range_position` -> `entry_range_position` (with `entry_zone_half`), replaced `origin_time`/`origin_index` with `zone_age_bars`, and converted event/sweep timestamps into `bars_ago` integers.

#### 7. Missing High-Signal Context (`csm_dir`, `setup_kind`, ATR Distances, HTF Zones, Session) *(FIXED)*
- **Location:** `app/pipeline.py:24-32, 150-221`
- **Mechanism:** `csm_dir`, `setup_kind` + rejection candle stats, ATR-normalized stop/TP/entry distances, HTF zones/pools, and UTC session were omitted from `build_llm_context()`.
- **Fix Applied:** Added `csm` (`aligned_direction` + `aligned|opposed|neutral` status), `setup_kind` and `rejection` metrics (`bucket`, `exp`, `clv`, `depth`, `mode`), `entry_distance_atr`, `stop_distance_atr`, `tp_distance_atr`, `session` (`utc_hour` + session bucket from `formed_at`), and per-HTF `entry_vs_equilibrium`, `nearby_zones`, `nearest_buyside_atr`, and `nearest_sellside_atr`.

#### 8. `llm.verdict` Was Requested and Paid For, Then Completely Ignored *(FIXED in P2)*
- **Location:** `app/ai/llm.py:12-34, 198-235`, `app/ai/hybrid.py:5-78`, `app/dashboard/static/index.html:1000-1035`
- **Mechanism:** `SYSTEM_PROMPT` instructed the model to output an unused `"verdict": "BUY|SELL|WAIT|AVOID"` enum alongside raw `0–100` `confidence`.
- **Fix Applied:** Replaced `confidence` + unused `verdict` with a focused qualitative risk-audit schema (`bias`, `quality`, bounded `score_adjustment` in `[-8, +5]`, `risk_flags`, `narrative`, `invalidation`, `concerns`) while preserving backward-compatible fallback parsing for legacy `0–100` `confidence` responses.

---

### Medium — Performance, Caching & API Reliability *(P1 — Implemented & Verified)*

#### 9. Redundant Full SMC + MTF Re-computation in Pass 2 (`enrich_analysis_llm`) *(FIXED)*
- **Location:** `app/pipeline.py:73-91, 368, 535-560`
- **Mechanism:** Pass 1 already computed `smc` and `htf_ctx`, yet Pass 2 reloaded candles from SQLite and re-ran `smc_mod.analyze()` + `build_htf_context()`.
- **Fix Applied:** Persisted slimmed HTF zones and liquidity levels via `_serialize_htf_for_storage(htf_ctx)` in `analysis["htf"]` during Pass 1, and updated `enrich_analysis_llm()` to reuse `analysis["smc"]` + `analysis["htf"]` + `store.load_csm()` directly (only falling back to candle reload for legacy rows lacking `atr`/`dealing_range`).

#### 10. Incomplete `_cache_key` in `LLMAnalyzer` *(FIXED)*
- **Location:** `app/ai/llm.py:36, 166-184`
- **Mechanism:** `_cache_key()` ignored `ml_win_probability`, `market_structure.trend`, `htf_context`, `csm_dir`, `entry_distance_atr`, and prompt changes.
- **Fix Applied:** Added `PROMPT_VERSION` (MD5 hash of `SYSTEM_PROMPT`) and included `pv`, `setup_kind`, rounded `ml_win_probability`, `market_structure.trend`, HTF trends, `csm.aligned_direction`, and quantized `entry_distance_atr` in `_cache_key()`.

#### 11. Missing `response_format`, `max_tokens`, Connection Pooling, and Telemetry *(FIXED)*
- **Location:** `app/ai/llm.py:39-165, 187-194`, `app/dashboard/server.py:755-761`, `config.yaml:143`, `app/config.py:73-74`
- **Mechanism:** No `<think>` stripping before JSON extraction, no `max_tokens` or `json_mode`, unpooled `requests.post` calls, and no latency/error/token telemetry.
- **Fix Applied:** Added `<think>...</think>` stripping in `_parse()`, persistent `requests.Session` with `HTTPAdapter(pool_connections=8, pool_maxsize=8)`, configurable `max_tokens` (`450`) and `json_mode` (`False`), thread-safe `LLMAnalyzer.stats()` counters (`calls`, `succeeded`, `cache_hits_mem`, `cache_hits_db`, `timeouts`, `http_errors`, `parse_errors`, `avg_latency_ms`, `prompt_tokens`, `completion_tokens`), and exposed `llm` stats on `GET /api/status`.

#### 12. Minor UI & Maintenance Drift *(FIXED in P2)*
- **Dashboard AI analyst card (`app/dashboard/static/index.html:1000-1035`):** `aiBoxHtml()` now renders a muted notice (`"AI analyst unavailable (timeout or endpoint error) — scored by ML only."`) when `llm_enabled` is true and `s.llm` is absent above the ML gate, and displays `quality`, signed `score_adjustment` (`+N pts` / `-N pts`), and `risk_flags` pills (with legacy `confidence/100` fallback).
- **Dynamic insight recommendations (`app/engine/insights.py:166-197`):** Reads live `ai.hybrid.contradiction_penalty` and `ai.hybrid.max_llm_adjustment` instead of hardcoded `-20` / `0.4` strings, and alerts when LLM contradictions outperform agreements.
- **Updated documentation (`docs/hybrid-fusion.md`):** Rewritten to reflect the calibrated ML anchor + bounded `[-8, +5]` LLM risk-audit fusion architecture and current `config.yaml` defaults.

---

## 5. Strategic Recommendation: Rethinking the LLM's Role

Right now, the LLM is used as a **0–100 numeric probability estimator** blended linearly (`25%` weight) with a Platt-calibrated gradient-boosted classifier. Empirically (`n = 290` resolved setups), that numeric blend **reduces** ROC-AUC from `0.5042` (`ml_prob` alone) to `0.4699` (`hybrid_score`), while simultaneously:
- Demoting `44` ML-qualified, LLM-aligned setups into `WAIT` due to uncalibrated confidence numbers (`median = 32`), and
- Penalizing contrarian sweep/retest setups that actually won `50.0%` (`+0.385R`).

### Recommended Two-Mode Architecture

1. **Keep ML as the calibrated score anchor** (or reduce `llm_weight` to `0.05–0.10` / use a centered residual `±5` pts around `ml_score` rather than raw `0–100` confidence) so uncalibrated LLM confidence numbers cannot override Platt-calibrated probabilities.
2. **Shift the LLM to structured qualitative risk-auditing + trader briefing**:
   - Ask the LLM to evaluate **specific structural/execution red flags** that tabular ML features struggle to synthesize (e.g., *opposing HTF OB/FVG directly blocking TP*, *excessive stop width in ATRs*, *exhausted multi-sweep range*, *CSM divergence*, *session liquidity risk*) and return:
     ```json
     {
       "quality": "high|medium|low",
       "risk_flags": ["htf_zone_blocking_tp", "wide_stop_atr", ...],
       "score_adjustment": -5,
       "narrative": "2-3 concise sentences explaining the structural thesis and key risk",
       "invalidation": "Specific price/structure invalidation condition",
       "concerns": ["..."]
     }
     ```
   - Bounded `score_adjustment` (`[-8, +5]`) eliminates the `0–100` calibration mismatch completely, preserves ML probability calibration, and turns the LLM into a targeted **sanity-check & explanation layer**.

---

## 6. Concrete Action Plan

| Priority | Status | Change | Target Files | Impact |
|---|---|---|---|---|
| **P0** | **DONE** | Add `payload=?` to `UPDATE setups_history` in `append_setups()` | `app/data/store.py` | Stops Pass-2 corruption of historical `llm_score`, `aligned`, and `score_source` |
| **P0** | **DONE** | Skip `meta.llm_pending == True` setups in `ExecutionEngine._candidates()` (with 45s timeout guard) | `app/execution/executor.py` | Prevents live orders firing on Pass-1 scores before Pass-2 LLM review completes |
| **P0** | **DONE** | Align `ai.llm.min_ml_prob` (`0.40 -> 0.41`) with `execution.min_ml_prob`, ensure `llm_cache` table + index exist, and log cache/API errors | `config.yaml`, `app/config.py`, `app/data/store.py`, `app/ai/llm.py` | Eliminates dashboard `BUY` vs executor skip mismatch & restores observable cross-worker SQLite caching |
| **P1** | **DONE** | Fix `build_llm_context()`: filter `liquidity_pools` to nearest active pools within `±5 ATR`, label `close_range_position` vs `entry_range_position`, convert timestamps to `bars_ago`, add `csm_dir`, `setup_kind`, `stop_distance_atr`, `entry_distance_atr`, HTF zones/pools, and UTC session | `app/pipeline.py` | Cuts prompt noise/tokens (`2,794 -> 2,020` chars on `AUDCHF`) while giving accurate spatial & MTF context |
| **P1** | **DONE** | Reuse Pass-1 `smc` + `htf` in `enrich_analysis_llm()` instead of re-running `smc_mod.analyze()` | `app/pipeline.py` | Eliminates redundant SQLite candle loads & SMC/MTF recomputation during Pass-2 enrichment |
| **P1** | **DONE** | Harden `LLMAnalyzer`: `requests.Session` pooling, `<think>` stripping, `max_tokens`, optional `json_mode`, expanded `_cache_key` (`ml_prob`, `trend`, `PROMPT_VERSION`), and telemetry exposed on `/api/status` | `app/ai/llm.py`, `app/dashboard/server.py`, `config.yaml`, `app/config.py` | Faster bar-close bursts, resilient JSON parsing, and full latency/token/error observability |
| **P2** | **DONE** | Replace raw `0–100` `llm.confidence` linear blend with bounded `score_adjustment` (`[-8, +5]`) around `ml_prob * 100`, add `quality` + `risk_flags`, and remove unused `llm.verdict` | `app/ai/llm.py`, `app/ai/hybrid.py`, `app/data/store.py`, `app/config.py`, `config.yaml` | Preserves Platt-calibrated ML ranking (`ROC-AUC >= 0.504`) and stops uncalibrated confidences from demoting valid setups |
| **P2** | **DONE** | Update UI fallback message when LLM fails + render `quality`/`score_adjustment`/`risk_flags`, fix stale `-20` / `0.4` strings in `insights.py`, and update `docs/hybrid-fusion.md` | `app/dashboard/static/index.html`, `app/engine/insights.py`, `docs/hybrid-fusion.md` | Transparent UI diagnostics, accurate insight recommendations, and up-to-date docs |

### Verification Log (P0, P1 & P2)
- **P0:** `scripts/smoke_execution.py` passed (`exit 0`); verified `store.append_setups()` updates `payload` on Pass-2 re-save; verified `ExecutionEngine._candidates()` defers `meta.llm_pending == True` for up to `45s`; verified `llm_cache` + `idx_llm_cache_created` in `data/trader.db` and `min_ml_prob == 0.41`.
- **P1:** `scripts/smoke_execution.py` and `scripts/smoke_telegram.py` passed (`exit 0`); verified `build_llm_context()` (`2,794 -> 2,020` chars, active nearby pools, `bars_ago`, ATR distances, CSM, session), zero-candle Pass-2 reuse in `enrich_analysis_llm()`, `<think>` stripping, expanded `_cache_key`, and `LLMAnalyzer.stats()`.
- **P2:** `scripts/smoke_execution.py` and `scripts/smoke_telegram.py` passed (`exit 0`); verified `LLMAnalyzer._parse()` clamping (`[-8, +5]`), `risk_flags` normalization, and legacy `confidence` fallback; verified `hybrid.fuse()` across positive/negative adjustments, directional contradiction overrides, and legacy confidence records; verified `store.append_setups()` persistence of `llm_adjustment`/`llm_quality`/`llm_risk_flags`, `insights.py` live config reporting, and `index.html` AI box rendering.
