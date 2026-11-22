# ML & Training Pipeline Review

**Date:** 2026-11-22  
**Scope:** End-to-end audit of `app/ai/*`, `app/pipeline.py`, `app/engine/{labeler,outcomes,insights,setup_builder,mtf}.py`, `app/smc/fast_context.py`, `data/models/setup_classifier.joblib`, and `data/trader.db`.

---

## Executive Summary

The ML subsystem (`HistGradientBoostingClassifier` + out-of-fold `IsotonicRegression` calibrator over 20 engineered SMC/MTF/rejection features) has solid engineering bones: fast single-pass `FastSMC`/`FastHTFContext` replay, causal HTF bar slicing (`htf_open + htf_sec <= up_to_time`), conservative limit-order labeling (`labeler.py`), environment/feature-hash staleness checks, and a guarded live-outcome blending path.

However, auditing the live SQLite database (`data/trader.db`) and the deployed model (`data/models/setup_classifier.joblib`, trained `2026-11-22 16:22:00`, `cv_auc = 0.589`, `n_samples = 13,805`) uncovered **seven concrete data, training-inference skew, and validation issues**—including **synthetic `--demo` candle contamination inside the production database** that severely corrupts ATR and `symbol_avg_atr_pct` features across 20 symbols.

---

## 1. Current State Snapshot *(Updated after P0)*

| Component | Status / Finding |
|---|---|
| **Deployed model** | `data/models/setup_classifier.joblib` (`sklearn` `HistGradientBoostingClassifier`, `max_iter=250`, `max_depth=4`, `lr=0.06`, `min_samples_leaf=10`, `l2=1.0` + OOF `IsotonicRegression`), retrained `2026-11-22 22:42:00` on clean candles |
| **Builder / Feature hash** | `BUILDER_VERSION = "v4-rejection-family"`, 20 features (`hash = 546fc382bc52`), `needs_retrain = False` |
| **Training metrics (post-P0)** | `n_samples = 20,350`, base `win_rate = 0.378`, `cv_auc = 0.561`, `cv_accuracy = 0.606` (prior pre-cleanup model: `n_samples = 13,805`, `cv_auc = 0.589`) |
| **Clean candle & replay corpus** | `353,949` clean candles (`3,386` demo bars purged); 22 enabled symbols × `{M15, H1, H4}` = **20,350** labeled setups |
| **Live journal (`setups_history`)** | 648 setups (`638` in Sep 2026, `10` in Nov 2026); 462 resolved `WIN`/`LOSS` (`172` wins, `37.2%` WR), 115 `EXPIRED_UNFILLED`, 20 `EXPIRED_OPEN` |
| **Live OOS discrimination (pre-P0)** | Logged `ml_prob` AUC = **0.557** (`n=458`); pre-cleanup model on v4 20-feature live setups (`n=143`) AUC = **0.543** |
| **Strict walk-forward OOS (clean M15)** | Train first 70% (`n=4,078`) → test last 30% (`n=1,916`, 96-bar purge): **OOS AUC = 0.575**, Brier = `0.241` |
| **Live-outcome blend status** | `185` unique non-overlapping live samples (`< min_samples: 200`, so P0 trained on clean replay only) |

### P0 Execution Log (2026-11-22)
- **Demo isolation (`app/config.py`, `app/main.py`):** `load_config(path, demo=True)` now routes `storage.path` → `data/demo_trader.db` and `ai.ml.model_path` → `data/models/demo_setup_classifier.joblib`.
- **Database cleanup (`data/trader.db`):** Backed up to `data/trader.db.bak_1795358257`, deleted **3,386** synthetic `--demo` bars (`2,869` non-BTCUSD weekend bars + `517` bars in `2026-09-06 13:00..24:00` UTC), and ran `VACUUM`. Verified `symbol_avg_atr_pct` normalized (e.g., `EURAUD M15`: `0.9455%` → `0.0496%`, `NZDUSD M15`: `0.0714%`).
- **Baseline retrain:** Ran `python -m app.main train --baseline` on all 22 enabled symbols (`20,350` samples) and updated `data/ml_baseline.json`.

---

## 2. Critical & High-Severity Findings

### Finding 1 (CRITICAL): Production `data/trader.db` Is Polluted by `--demo` Candles
20 symbols on `M15` contain **41 synthetic `--demo` bars** (`2026-09-06 13:45` to `2026-09-06 23:45` UTC) where prices jump from normal FX quotes (~`0.55–2.30`) to **`~100.00`** and drop back at `2026-09-07 00:00`:

- **Affected symbols (20):** `AUDCAD`, `AUDCHF`, `AUDNZD`, `CADCHF`, `CHFJPY`, `EURAUD`, `EURCAD`, `EURCHF`, `EURGBP`, `EURJPY`, `EURNZD`, `GBPAUD`, `GBPCAD`, `GBPCHF`, `GBPJPY`, `GBPNZD`, `NZDCAD`, `NZDCHF`, `NZDUSD`, `USDCHF`.
- **Impact on ML features:**
  - At `2026-09-07 00:00`, the `100.00 → 1.61` bar creates a single-bar True Range of `~98.4`, spiking EWM `atr_pct` on `EURAUD M15` from **`0.06%` to `457.7%`** (still `12.17%` 50 bars later).
  - `SymbolStats.static_features()` computes `symbol_avg_atr_pct` across the entire `candles` table: for `EURAUD M15`, `symbol_avg_atr_pct` is **`0.946%`** (and **`1.609%`** when recorded in September) instead of the true **`~0.045%`**—a **20×–35× distortion**.
  - In `EURAUD M15` replay alone, 17 training samples have `atr_pct > 1.0%` (up to >400%), and `range_position` in live v4 setups ranged from **`-76.33` to `+7.04`** (90% of setups normally sit inside `[-0.2, 1.2]`).
- **Root cause:** Running `--demo` against the default `config.yaml` (`storage.path: data/trader.db`) upserted synthetic `100.0`-seeded bars into the live database.

### Finding 2 (HIGH): Training–Inference Skew in `SymbolStats` (Scale, Window, and Cold-Start)
`symbol_avg_atr_pct`, `symbol_recent_win_rate`, and `symbol_avg_rr_realized` are computed differently during training vs. live inference:

1. **Multi-TF scale mismatch on `symbol_avg_atr_pct` (`app/pipeline.py:553`, `app/ai/symbol_stats.py:33`):**
   - Training pools `M15`, `H1`, and `H4` rows into a single model. For `EURUSD`, `symbol_avg_atr_pct` is **`0.049%` on M15** vs **`0.257%` on H4** (5.3× larger)—completely redundant with `atr_pct` (which already measures current TF volatility) and acting mainly as a timeframe proxy.
   - Worse, during training `static_features(df, symbol)` averages over the *entire* 3,000–7,958-bar history (`up to ~2 years` on H4), whereas live `refresh_pair` caches the static result for the process lifetime.
2. **Per-TF vs. cross-TF dynamic history (`app/pipeline.py:554` vs `app/ai/symbol_stats.py:85`):**
   - Training tracks `history_by_pair[(symbol, tf)]` (separate rolling buffer per `(symbol, tf)`), while live `dynamic_features_live(store, symbol)` queries `store.outcome_history_for_symbol(symbol)` across **all timeframes**.
3. **100% cold-start null rate in live v4 setups (`min_samples = 30`):**
   - In `SymbolStats`, `min_samples = 30` (`symbol_stats.py:21`). In the live database, the highest resolved `WIN`/`LOSS` count for any single symbol is `GBPUSD = 32` (most symbols have `14–29`).
   - Consequently, across **all 143 live v4 setups**, `symbol_recent_win_rate` and `symbol_avg_rr_realized` were **`None` 100% of the time** (imputed to `0.5` and `1.0`), whereas during training replay (~400–500 setups/symbol) they are populated for ~90% of rows and carry non-trivial permutation importance (`0.0081` / `0.0046`).

### Finding 3 (HIGH): Minor Look-Ahead Leak in Replay `symbol_recent_win_rate` & `symbol_avg_rr_realized`
In `app/pipeline.py:595`:
```python
prior = [h for h in history if h["formed_index"] < i]
```
A setup formed at bar `i - 4` does not resolve until up to `i - 4 + 96 = i + 92`. Filtering only on `formed_index < i` leaks future outcomes (`horizon_end_index > i`) into `symbol_recent_win_rate` and `symbol_avg_rr_realized` during training. While the fraction of overlapping prior rows is small (`~0.6%` on a 7,600-bar series), it is strictly non-causal and easily fixed by tracking the actual resolution bar `resolved_index <= i`.

### Finding 4 (HIGH): Cross-Validation Overestimates AUC and Distorts Isotonic Calibration
In `app/ai/ml_model.py:135` & `149`:
```python
auc = cross_val_score(model, X, y, cv=n_splits, scoring="roc_auc")
cv_prob = cross_val_predict(model, X, y, cv=5, method="predict_proba")[:, 1]
```
1. **Grouped symbol ordering in `cv=3`:** `rows` are concatenated by symbol (`[AUDUSD_M15, AUDUSD_H1, AUDUSD_H4, EURUSD_M15, ...]`). Unshuffled `StratifiedKFold(3)` splits contiguous blocks of symbols—putting different currency regimes in train vs. test (`cv_auc = 0.587` unshuffled vs `0.604` shuffled `SKF(5)` vs `0.595` `GroupKFold(symbol)`).
2. **Temporal leakage within folds:** Adjacent replay steps (`step = 4`, `horizon = 96`) share overlapping forward price windows (`24` steps overlap per horizon) and identical HTF contexts (`H4` bar spans 16 `M15` steps). Random/contiguous K-fold puts overlapping bars in both train and validation, inflating OOF `predict_proba` confidence and causing `IsotonicRegression` to overfit step edges.
3. **Empirical live calibration mismatch (v4, `n=143`):**
   - `[0.25, 0.35)`: predicted `31.8%` vs realized **`50.0%`** (`+0.673R`, `n=14`)
   - `[0.35, 0.42)`: predicted `38.3%` vs realized **`44.4%`** (`+0.272R`, `n=45`)
   - `[0.42, 0.50)`: predicted `44.4%` vs realized **`50.0%`** (`+0.340R`, `n=66`)
   - `[0.50, 1.00)`: predicted `55.5%` vs realized **`22.2%`** (`-0.408R`, `n=9`)
   - Furthermore, only **4 of 644** journaled setups ever reached `ml_prob >= 0.50` (`execution.min_ml_prob: 0.50`), starving the execution engine (`BUY` + `SELL` total = **3** out of 648 setups!).

### Finding 5 (MEDIUM): Training on H1/H4 Slightly Hurts Live M15 v4 Discrimination
`mtf.entry_tf` is set to `M15` in `config.yaml`—the system **only** generates live setups and trades on `M15`. However, `train()` replays all `timeframes: [M15, H1, H4]` (`10,226` M15 rows + `10,403` H1/H4 rows):
- On `H4` (the highest TF), `htf_trend_align` is always `0.5` and `htf_tp_distance_atr` is always `20.0` (no higher TF exists), teaching the tree split rules that only apply when HTF context is absent.
- Evaluating on live v4 `M15` setups (`n=143`): an **M15-only** model achieves **AUC = 0.580** vs **0.532** for the **All-TF** model (`+0.048` AUC gain on current setups).

### Finding 6 (MEDIUM): Legacy 19-Feature Live Samples & Missing `setup_kind` Feature
1. **Legacy schema in `live_training_samples()` (`app/data/store.py:479`):** Of the `220` live training samples, `77` are pre-v4 rows missing `rej_exp`, `rej_clv`, `rej_is_momentum`, and `rej_is_agrade`. When `feature_vector()` runs on them, `rej_exp` defaults to `0.0` and `rej_clv` defaults to **`0.0`**—whereas `make_features()` sets `rej_clv = 50.0` (neutral) for non-rejection setups (`features.py:100`)! Thus legacy zone setups get misencoded as extreme `clv = 0` rejections.
2. **No explicit `is_rejection` indicator:** `setup_builder.py` emits two distinct setup families (`zone` with 24-bar validity and `0.75 ATR` min risk vs `rejection` with 8-bar validity and `0.30 ATR` min risk). Live outcomes show rejection setups have `50.0%` WR (`+0.385R`, `n=26`) vs `37.0%` for FVG (`+0.078R`, `n=378`) and `32.8%` for OB (`+0.021R`, `n=58`), yet the feature vector relies on `rej_exp == 0` implicitly rather than an explicit `is_rejection` flag.

### Finding 7 (LOW / HYGIENE): Dead/Redundant Features & Stale Artifacts
- **Redundant features:** `symbol_recent_win_rate` and `symbol_avg_rr_realized` have `r = 0.878` correlation; `rej_exp` and `rej_clv` have `r = 0.776`.
- **Near-zero importance features:** `rej_is_momentum` (`0.0000`), `rej_is_agrade` (`0.0004`), `htf_trend_align` (`0.0005`), `trend_alignment` (`0.0008`).
- **Documentation drift:** `docs/ml-method.md` still describes 15/16 features and old `.feature_set_v2_per_symbol` markers instead of the current 20-feature `v4-rejection-family` schema.
- **Workspace clutter:** `data/` contains 14 temporary test DB/model artifacts (`_test_*.db`, `_review_copy.db`, `_test_gpu.joblib`, etc.).

---

## 3. Prioritized Action Plan

### P0 — Clean Contaminated Data & Guard Ingestion *(DONE)*
1. **Purge the `2026-09-06 13:45 .. 23:45` `--demo` bars** from `data/trader.db` — **Done:** backed up to `data/trader.db.bak_1795358257` and removed `3,386` synthetic weekend/`~100.0` bars (`VACUUM` complete). Note: re-run `python -m app.main ingest` with MT5 connected if you also want to backfill any missing real Friday/Monday boundary bars around `2026-09-06`.
2. **Isolate `--demo` storage** in `app/config.py` and `app/main.py` — **Done:** `load_config(..., demo=True)` now redirects `storage.path` to `data/demo_trader.db` and `ai.ml.model_path` to `data/models/demo_setup_classifier.joblib`.
3. **Retrain immediately** after cleaning `trader.db` — **Done:** retrained on `20,350` clean replay rows (`cv_auc = 0.561`, `win_rate = 0.378`) and saved the new baseline to `data/ml_baseline.json`.

### P1 — Fix Feature Skew & Causal Replay
1. **Fix `feature_vector()` default for `rej_clv` (`app/ai/features.py:134`):** Default missing `rej_clv` to `50.0` (matching `make_features()`), not `0.0`, so legacy pre-v4 live samples do not masquerade as extreme rejection bars.
2. **Fix replay causal filter (`app/pipeline.py:595`):** Record the exact bar index `resolved_index` in `label_setup()` and filter `prior = [h for h in history if h["resolved_index"] <= i]` so unresolved future outcomes never leak into `symbol_recent_win_rate`.
3. **Align `SymbolStats` cold-start & scale (`app/ai/symbol_stats.py`):**
   - Lower `min_samples` from `30` to `10` (or use Bayesian shrinkage toward the global prior `(wins + 10 * 0.38) / (n + 10)`) so live inference actually uses symbol feedback instead of outputting `None` for 100% of setups.
   - Compute `symbol_avg_atr_pct` over a trailing window (e.g., last 500 bars of `entry_tf`) rather than all-time history so regime shifts and multi-TF scales stay aligned.

### P2 — Improve Validation, Calibration, and Training Scope
1. **Train on `entry_tf` (`M15`) when `mtf.entry_tf` is configured** (or add `tf_minutes` / test M15-only vs all-TF via `--compare`), since live v4 setups are exclusively `M15` and M15-only replay scored `+0.048` higher AUC on live v4 outcomes.
2. **Use purged time-series / `GroupKFold` CV (`app/ai/ml_model.py`):** Replace unshuffled `cv=3` with purged chronological folds (e.g., 5 time blocks with a `label_horizon_bars` purge gap) for both `cv_auc` reporting and out-of-fold probability calibration.
3. **Replace step-function `IsotonicRegression` with `CalibratedClassifierCV(..., method="sigmoid")` (Platt scaling) or smoothed isotonic/logistic calibration:** Isotonic regression on noisy ~0.58-AUC probabilities creates flat plateaus and sharp jumps (`0.485 -> 0.523 -> 0.596`) that distort downstream `hybrid.fuse()` scores and choke the `execution.min_ml_prob: 0.50` gate.

### P3 — Feature & Threshold Tuning (Once Clean Data & ≥400 v4 Live Outcomes Exist)
1. **Add `is_rejection` (0/1)** and prune zero-importance binary flags (`rej_is_momentum`, `rej_is_agrade`) if permutation importance remains `<= 0` on clean M15 data.
2. **Re-calibrate `execution.min_ml_prob` and `ai.hybrid.buy_threshold`:** With a ~38% base win rate and 1.85R average win, positive expectancy begins around `P(win) >= 0.36–0.40`; requiring `min_ml_prob: 0.50` blocks >99% of valid setups (`640/644`).
3. **Update `docs/ml-method.md`** to match the current 20-feature `v4-rejection-family` implementation and clean up `_test_*` artifacts in `data/`.
