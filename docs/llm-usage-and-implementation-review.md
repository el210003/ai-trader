# LLM Usage & Implementation Review

**Scope:** End-to-end audit of LLM integration (`app/ai/llm.py`, `app/ai/hybrid.py`, `app/pipeline.py`, `app/data/store.py`, `app/dashboard/server.py`, `app/execution/executor.py`, `app/engine/insights.py`, `app/dashboard/static/index.html`, `config.yaml`) and empirical analysis against `data/trader.db` (`668` journaled setups, `478` resolved outcomes).

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

### Critical — Data Integrity & Execution Safety

#### 1. `store.append_setups()` Corrupts `setups_history.payload` on Pass 2
- **Location:** `app/data/store.py:263-268`
- **Mechanism:** In bar-close mode, Pass 1 (`skip_llm=True`) inserts a new setup into `setups_history` with `score = ml_prob * 100` and `payload` containing `{"score_source": "ml", "llm_score": null, "aligned": true}`. When Pass 2 (`enrich_analysis_llm`) finishes seconds later and calls `store.append_setups()`, the `if ex:` branch executes:
  ```python
  self.conn.execute(
      "UPDATE setups_history SET verdict=?, score=?, ml_prob=?, close=?, "
      "logged_at=? WHERE id=?",
      (r[5], r[6], r[7], r[12], r[2], ex[0]))
  ```
  **`payload` is omitted from the `UPDATE`!**
- **Evidence in `data/trader.db`:** Multiple rows (e.g., `id=2386 EURUSD BUY score=46.4 ml=0.409`, `id=2392 USDCAD SELL score=49.6 ml=0.435`) have LLM-fused `score` and `verdict` columns, while their JSON `payload` still records `score_source: "ml"`, `llm_score: null`, `aligned: true`. If the LLM contradicted (`aligned=False`), `payload.aligned` stays `True` forever, directly skewing `app/engine/insights.py:167-184`.
- **Fix:** Include `payload=?` in the `UPDATE setups_history` statement in `store.append_setups()`.

#### 2. `llm_cache` Table Missing in Live `data/trader.db` + Silent Exception Swallowing
- **Location:** `app/ai/llm.py:72-80, 111-115` and `app/data/store.py:125-130`
- **Mechanism:** `data/trader.db` currently has no `llm_cache` table (the running server predates the schema addition or opened before migration), and `LLMAnalyzer.analyze_setup()` wraps both `get_llm_cache` and `put_llm_cache` in `except Exception: pass`. Every SQLite cache read/write silently fails without logging, leaving only per-process RAM caching.
- **Fix:** Ensure `Store._create()` runs on startup (restart server or run migration), and log cache/API failures via `debug()` instead of bare `except Exception: pass`.

#### 3. Two-Pass Race Condition with `ExecutionEngine`
- **Location:** `app/execution/executor.py:253-264`, `app/pipeline.py:158-160, 179-180`
- **Mechanism:** Pass 1 saves `analysis` with `meta.llm_pending = True` and an interim `ml`-only `verdict` (`BUY`/`SELL` whenever `ml_prob >= 0.44`). `ExecutionEngine` polls every `15s` (`fresh_seconds = 120`) and checks `payload.get("setups")` without inspecting `meta.llm_pending`. When multiple pairs queue for Pass 2 (`max_workers=4`), `ExecutionEngine` can fire an order during Pass 1 before the LLM has a chance to flag `AVOID` or lower the score.
- **Fix:** In `ExecutionEngine._candidates()`, skip analyses where `(payload.get("meta") or {}).get("llm_pending")` is `True` (with a safety fallback if `now - generated_at > 45s` in case Pass 2 crashed).

#### 4. Dashboard vs. Executor Gate Split-Brain
- **Location:** `config.yaml:141, 146, 207-208`
- **Mechanism:** `ai.llm.min_ml_prob` is `0.40` and `ai.hybrid.buy_threshold` is `44`, while `execution.min_ml_prob` is `0.41`. With `MiniMax-M3` returning confidences around `58–69`, a setup with `ml_prob = 0.399` (e.g., `id=2400 USDJPY`) reaches `final_score = 44.4` and displays **`BUY`** on the dashboard, yet `ExecutionEngine` unconditionally skips it (`ml_prob 0.40 < min_ml_prob 0.41`).
- **Fix:** Align `ai.llm.min_ml_prob` with `execution.min_ml_prob` (`0.41`) — which also saves tokens on setups the executor will never trade — or align `execution.min_ml_prob` with the intended hybrid floor.

---

### High — Prompt & Context Engineering Flaws

#### 5. `build_llm_context()` Sends 37-Day-Old Liquidity Pools Instead of Nearby Active Pools
- **Location:** `app/pipeline.py:35` (`"pools": smc["liquidity_pools"][-5:]`)
- **Mechanism:** `cluster_equal_levels()` (`app/smc/liquidity.py:39`) sorts `liquidity_pools` **ascending by price**, not chronologically or by distance to price. Taking `[-5:]` grabs the **5 highest-priced pools across the entire 7,258-bar history**. In the live `AUDCHF M15` analysis (`last_close = 0.5559`), `build_llm_context` sends 5 pools at `0.5855–0.5874` (~300 pips away, `last_index` `3600–3727` = **>3,500 bars / ~37 days old**), complete with raw internal `members` bar-index arrays.
- **Fix:** Filter `smc["liquidity_pools"]` to active/un-swept pools within e.g. `±5 * atr` of `last_close` (or nearest 3 buyside + nearest 3 sellside pools), expressed with `distance_atr` and `age_bars`, and strip raw `members` arrays.

#### 6. Contradictory Dealing-Range Fields & Raw Unix Timestamps
- **Location:** `app/pipeline.py:30-43`
- **Mechanism:**
  - `dealing_range.position` (`0.463`, `"discount"` in the live `AUDCHF` payload) measures **`last_close`**, whereas `setup.range_position` (`0.662`, `"entry in premium zone"`) measures the **limit `entry`**. Passing both unlabeled causes the LLM to see conflicting `"discount"` vs `"premium"` strings for the same setup.
  - `last_events` and `recent_sweeps` include raw 10-digit Unix epochs (`1795176900`) and internal bar indices (`7211`, `7222`) without `n_bars`, making it impossible for the LLM to tell whether an event happened 2 bars ago or 90 bars ago unless it happens to be mentioned in `confluences`.
- **Fix:** Rename `dealing_range.position` -> `close_range_position` and `setup.range_position` -> `entry_range_position`, and convert event/sweep timestamps to `bars_ago` integers.

#### 7. Missing High-Signal Context (`csm_dir`, `setup_kind`, ATR Distances, HTF Zones, Session)
- **Location:** `app/pipeline.py:24-50`, `app/pipeline.py:378`
- **Mechanism:**
  - `csm_dir` (Currency Strength alignment) is never passed to `build_llm_context()` (and is completely absent when CSM **opposes** the trade).
  - `setup_kind` (`"rejection"` vs OB/FVG zone retest) and rejection candle metrics (`exp`, `clv`, `depth`) are omitted.
  - `entry_distance_atr` and `(abs(entry - stop_loss) / atr)` are omitted, forcing the LLM to do floating-point division in its head.
  - `htf_context` strips out `zones` (`H1`/`H4` OBs/FVGs) and `buyside`/`sellside` pools, so the LLM cannot check whether TP runs straight into an opposing HTF order block.
  - Note also that in Pass 2 (`app/pipeline.py:378`), if `len(df) < 250` fallback triggers, `htf_ctx` becomes `{}` even though `analysis["htf"]` was already saved in Pass 1.
  - Session / UTC hour (`Asia` vs `London` vs `LDN/NY` vs `NY`) is omitted despite strong session win-rate differences.

#### 8. `llm.verdict` Is Requested and Paid For, Then Completely Ignored
- **Location:** `app/ai/llm.py:22-26, 148`, `app/ai/hybrid.py:5-61`, `app/dashboard/static/index.html:1013-1022`
- **Mechanism:** `SYSTEM_PROMPT` instructs the model to output `"verdict": "BUY|SELL|WAIT|AVOID"` and `_parse()` validates it, but neither `hybrid.py` nor the UI ever reads `llm["verdict"]`. In the live `AUDCHF` row, `llm.verdict` is `"SELL"` (`confidence: 64`), while the setup card shows `verdict: "WAIT"` (`final_score: 54.7`? Actually `54.7` was from an earlier heuristic run, or `52.4` under `0.75/0.25`). Asking for an unused enum wastes output tokens and creates silent internal contradictions (`bias: "bullish"` with `verdict: "WAIT"`).

---

### Medium — Performance, Caching & API Reliability

#### 9. Redundant Full SMC + MTF Re-computation in Pass 2 (`enrich_analysis_llm`)
- **Location:** `app/pipeline.py:363-378`
- **Mechanism:** Pass 1 already computed `smc_mod.analyze(df)` and `build_htf_context(...)` and persisted `smc` and `htf` into `store.save_analysis(symbol, tf, payload)`. Pass 2 reloads candles from SQLite and re-runs the entire SMC + H1/H4 analysis from scratch just to build the LLM prompt.
- **Fix:** Either reuse `analysis["smc"]` + `analysis["htf"]` directly in `enrich_analysis_llm` (storing slimmed HTF zones/pools in `analysis["htf"]`), or pass the in-memory `(smc, htf_ctx)` dict from Pass 1 to Pass 2.

#### 10. Incomplete `_cache_key` in `LLMAnalyzer`
- **Location:** `app/ai/llm.py:119-127`
- **Mechanism:** `_cache_key()` hashes only `(symbol, timeframe, direction, entry, stop_loss, take_profit, origin_time, len(confluences))`. It omits:
  - `ml_win_probability` (a retrain or updated `symbol_recent_win_rate` changes the input prompt, yet returns the stale cached JSON)
  - `market_structure.trend` and `htf_context` (an H1 bar close or M15 CHoCH within the 15-minute TTL changes the structural context)
  - A `PROMPT_VERSION` hash (editing `SYSTEM_PROMPT` or `build_llm_context` still serves pre-edit cached responses).
- **Fix:** Include `round(ml_prob, 3)`, `smc["trend"]`, quantized `last_close` (or `entry_distance_atr`), HTF trends, and a `PROMPT_VERSION` constant in `_cache_key`.

#### 11. Missing `response_format`, `max_tokens`, Connection Pooling, and Telemetry
- **Location:** `app/ai/llm.py:81-117`
- **Mechanism:**
  - **No `response_format` or `max_tokens`:** Reasoning/chat models (`MiniMax-M3`) can emit `<think>...</think>` blocks or verbose prose, increasing latency toward the `15s` timeout. Furthermore, if `<think>` contains curly braces `{...}`, `text.find("{")` to `text.rfind("}")` in `_parse()` will fail `json.loads()`.
  - **New TCP/TLS connection per call:** Uses module-level `requests.post(...)` instead of a persistent `requests.Session()` with connection pooling.
  - **Zero telemetry:** No logging of API latency, HTTP status errors, timeouts, parse failures, or token usage (`usage.prompt_tokens` / `completion_tokens`).
- **Fix:**
  - Strip `<think>...</think>` before JSON extraction in `_parse()`.
  - Pass `"max_tokens": int(self.cfg.get("max_tokens", 450))` and optional `"response_format": {"type": "json_object"}` (configurable via `ai.llm.json_mode: true`, as some local Ollama/LM Studio models prefer `json_object` or grammar).
  - Use a shared `requests.Session()` with `HTTPAdapter(pool_connections=8, pool_maxsize=8)`.
  - Track and log counters (`calls`, `cache_hits`, `timeouts`, `http_errors`, `parse_errors`, `avg_latency_ms`) and expose them in `/api/status` or `/api/meta`.

#### 12. Minor UI & Maintenance Drift
- **Silent LLM failure in UI (`app/dashboard/static/index.html:999-1006`):** `aiBoxHtml()` explains why the LLM was skipped when `ml_prob < gate`, but renders nothing when `ml_prob >= gate` and the LLM call timed out or failed (`score_source == "ml"`). Showing a muted notice (`"AI analyst unavailable (timeout/error) — scored by ML only"`) makes failures visible.
- **Stale text in `app/engine/insights.py:176, 183`:** Hardcodes `"keep the −20 penalty"` and default `llm_weight = 0.4` instead of reading `ai.hybrid.contradiction_penalty` (`5.0`) and `ai.hybrid.llm_weight` (`0.25`).
- **Stale docs in `docs/hybrid-fusion.md`:** Still documents pre-Phase-4 defaults (`0.6`/`0.4` weights, `buy_threshold: 60`, `-20` hard `AVOID`).

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

| Priority | Change | Target Files | Impact |
|---|---|---|---|
| **P0** | Add `payload=?` to `UPDATE setups_history` in `append_setups()` | `app/data/store.py` | Stops Pass-2 corruption of historical `llm_score`, `aligned`, and `score_source` |
| **P0** | Skip `meta.llm_pending == True` setups in `ExecutionEngine._candidates()` (with 45s timeout guard) | `app/execution/executor.py` | Prevents live orders firing on Pass-1 scores before Pass-2 LLM review completes |
| **P0** | Align `ai.llm.min_ml_prob` (`0.40 -> 0.41`) with `execution.min_ml_prob` and ensure `llm_cache` table exists | `config.yaml`, `app/config.py`, `app/data/store.py` | Eliminates dashboard `BUY` vs executor skip mismatch & restores cross-worker SQLite caching |
| **P1** | Fix `build_llm_context()`: filter `liquidity_pools` to nearest active pools within `±5 ATR`, label `close_range_position` vs `entry_range_position`, convert timestamps to `bars_ago`, add `csm_dir`, `setup_kind`, `stop_distance_atr`, `entry_distance_atr`, HTF zones/pools, and UTC session | `app/pipeline.py` | Cuts prompt noise/tokens while giving the LLM accurate spatial & MTF context |
| **P1** | Reuse Pass-1 `smc` + `htf` in `enrich_analysis_llm()` instead of re-running `smc_mod.analyze()` | `app/pipeline.py` | Eliminates redundant CPU work during Pass-2 bar-close enrichment |
| **P1** | Harden `LLMAnalyzer`: `requests.Session` pooling, `<think>` stripping, `max_tokens`, optional `response_format`, expanded `_cache_key` (`ml_prob`, `trend`, `PROMPT_VERSION`), and error/latency logging | `app/ai/llm.py` | Faster bar-close bursts, fewer parse/timeout failures, and full observability |
| **P2** | Replace raw `0–100` `llm.confidence` linear blend with bounded `score_adjustment` (`[-8, +5]`) or centered residual, and remove unused `llm.verdict` | `app/ai/llm.py`, `app/ai/hybrid.py`, `config.yaml` | Fixes ROC-AUC degradation (`0.4699 -> >=0.504`) and stops suppressing valid ML setups |
| **P2** | Update UI fallback message when LLM fails, fix stale `-20` / `0.4` strings in `insights.py`, and update `docs/hybrid-fusion.md` | `app/dashboard/static/index.html`, `app/engine/insights.py`, `docs/hybrid-fusion.md` | Transparent UI diagnostics and accurate documentation |
