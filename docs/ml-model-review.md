# ML Model Review & Calibration (record)

**Date:** recorded after model review on the live deployment.
**Context:** user asked to review the current ML model and propose improvements.
**Outcome:** calibration added, dead features pruned, retrain-detection hardened.

## What the review found (deployed model: `setup_classifier.joblib`)

| Metric | Value | Reading |
|---|---|---|
| cv_auc | 0.543 | weak ranking, but real FX with ~33% base rate + asymmetric RR is hard; partly the market |
| n_samples (replay train) | 5,089 | enough for boosting, NOT the bottleneck |
| win_rate (train) | 0.33 | by design for RR≥1.5 asymmetric setups |
| live resolved outcomes | ~234 | the REAL shortage — calibration/reporting data is thin |
| `trained_at` | correct (09/11 18:41) | inspection initially misread top-level key; no bug |

### Probability calibration (the key problem)
Predicted vs realized win rate across resolved live outcomes:
- 0.1–0.2 bucket: predicted 15% → realized 24% (**undershoot −9%**)
- 0.2–0.3 bucket: predicted 26% → realized 38% (**undershoot −12%**)
- 0.4–0.5 bucket: predicted 43% → realized 22% (**overshoot +21%**)

→ The model systematically **undershoots the 0.2–0.4 band (where ~72% of trades
sit) and overshoots its highest-conviction calls.** The `ai.llm.min_ml_prob`
(0.30) gate and the `hybrid.buy_threshold` (60) were acting on wrong numbers.

### Dead features (from live feature importance)
- `is_order_block` — **0.0** (OB-vs-FVG adds nothing)
- `entry_in_htf_zone` — 0.0013
- `symbol_setup_density` — 0.0016

### Hybrid score ranking
Score buckets were **not monotonic** with win rate (0–10: 29%, 20–30: 40%,
40–50: 12%) — consistent with a miscalibrated ML feeding the fusion.

## Changes implemented

1. **Isotonic probability calibration** (`app/ai/ml_model.py`)
   - `SetupML.train` fit an `IsotonicRegression` on **out-of-fold** predictions
     (cross_val_predict, cv=5) so the calibrator is honest, not self-referential.
   - `predict()` applies the calibrator; persisted in the blob; reloaded on `load()`.
   - Monotonic → preserves ranking (cv_auc unchanged) but makes `p(win)` a true
     probability, so the gate/threshold/fusion mean something.
   - Guard: skipped when < ~120 training samples (tiny datasets).

2. **Pruned dead features** (`app/ai/features.py`): removed
   `is_order_block`, `entry_in_htf_zone`, `symbol_setup_density` →
   **19 → 16 features**. `feature_set_hash` changed → model auto-retrains.

3. **Hardened retrain detection** (`app/pipeline.py::model_needs_retrain`)
   - Now also compares the **deployed model's own recorded feature list +
     `builder_version`** against current, not just the marker file.
   - Closes the bug class where a marker/file mismatch silences a stale model.

4. **`BUILDER_VERSION` moved to `app/ai/ml_model.py`** so the blob records it
   and the check can compare it.

## Caveats / honest constraints

- **Calibration preserves cv_auc** — it fixes probability *values*, not ranking.
  The model's ranking power is capped by data quality, not calibration.
- **The calibration curve itself needs data.** Only ~234 live resolved outcomes
  exist (deduped); buckets of 9–102 are noisy, and most setups take ~14h to
  resolve. The undershoot/overshoot findings above are directional, not final.
- Live data gen rate (post-dedup, entry_tf=M15): ~21 setups/day → ~7–10 resolved
  WIN/LOSS/day → meaningful calibration + tier-3 blend in **~2–4 weeks**.

## Follow-up plan

1. Verify the auto-retrain produces the 16-feature + calibrated model
   (restart serve.bat → one-time ensure_model_current retrain).
2. In ~2–3 weeks (≥400–500 resolved outcomes): re-draw the calibration curve,
   re-run the feature-tercile report (drop any feature confirmed dead on live),
   and decide `hybrid.ml_weight`/`llm_weight` with real numbers.
3. Consider the tier-3 live-outcome blend engaging once `min_samples: 200` is
   comfortably passed.
