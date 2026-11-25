# Hybrid Fusion: Calibrated ML Anchor + Bounded LLM Risk Audit

This document explains how the **ML win-probability** and the **LLM risk auditor**
are combined into the final score and verdict shown on the dashboard and used by
the execution engine.

Entry point: `app/ai/hybrid.py::fuse(setup, ai_cfg)`.

---

## Why ML-Anchored + Bounded Adjustment

Earlier iterations blended `ml_prob * 100` (75%) with an uncalibrated `0–100`
LLM confidence score (25%) and applied a `-20` contradiction penalty. Empirical
review across `290` resolved setups showed that uncalibrated LLM confidences
(median `32/100`) dragged down Platt-calibrated ML probabilities (`max ~48.5`),
lowered ROC-AUC (`0.504 -> 0.470`), and demoted valid setups into `WAIT`.

The hybrid design solves this by separating responsibilities:

1. **ML model = calibrated probability anchor (`ml_prob * 100`)** trained on
   historical outcomes with out-of-fold Platt/isotonic calibration.
2. **LLM = qualitative SMC risk auditor (`score_adjustment` ∈ `[-8, +5]`)**
   that inspects spatial/structural nuances (opposing HTF OB/FVG blocking TP,
   excessive ATR stop width, sweep exhaustion, CSM opposition, session liquidity)
   and returns structured `quality`, `risk_flags`, `score_adjustment`, `narrative`,
   `invalidation`, and `concerns`.
3. **Configurable alignment guard (`contradiction_penalty`, `avoid_override_ml_prob`)**
   with transparent fallbacks when either signal is unavailable.

---

## Inputs

### ML signal

`setup["ml_prob"]` — `P(win)` from the trained classifier, in `[0, 1]`, or
`None` if no model is loaded.

```python
def predict(self, feats: dict):
    if self.model is None: return None
    x = np.array([feature_vector(feats)], dtype=float)
    return float(self.model.predict_proba(x)[0, 1])
```

### LLM signal

`setup["llm"]` — a normalized dict from `LLMAnalyzer.analyze_setup`, or
`None` if disabled / below `min_ml_prob` gate / endpoint unreachable / parse failed.

```python
{
  "bias": "bullish" | "bearish" | "neutral",
  "quality": "high" | "medium" | "low",
  "score_adjustment": -8..5,
  "risk_flags": ["<snake_case_flag>", ...],
  "narrative": "<2-3 sentence concise structural & risk analysis>",
  "invalidation": "<what specifically invalidates this setup>",
  "concerns": ["<specific risk factor>", ...],
  "model": "<model name>"
}
```

The LLM never raises — failures are logged via `debug()` and return `None` so
the pipeline and dashboard continue cleanly.

---

## Fusion algorithm

```python
direction = setup["direction"]                   # "long" or "short"
want_bias = "bullish" if direction == "long" else "bearish"

hyb = (ai_cfg or {}).get("hybrid", {})
threshold = float(hyb.get("buy_threshold", 44))
contra_penalty = float(hyb.get("contradiction_penalty", 5.0))
avoid_override_ml = float(hyb.get("avoid_override_ml_prob", 0.42))
min_adj = float(hyb.get("min_llm_adjustment", -8.0))
max_adj = float(hyb.get("max_llm_adjustment", 5.0))

adj = max(min_adj, min(max_adj, float(llm["score_adjustment"])))

if ml is not None and has_adj:
    final = float(ml) * 100.0 + adj
    source = "ml+llm"
elif ml is not None and has_legacy_conf:          # legacy 0-100 confidence fallback
    final = (ml * 100.0 * ml_weight + llm["confidence"] * llm_weight) / (ml_weight + llm_weight)
    source = "ml+llm"
elif ml is not None:
    final = float(ml) * 100.0
    source = "ml"
elif has_adj:
    final = 50.0 + adj
    source = "llm"
else:
    # no AI available -> transparent confluence-based heuristic
    final = min(90.0, 35.0 + 8.0 * len(setup.get("confluences", []))
                + (5.0 if setup.get("rr", 0) >= 2.0 else 0.0))
    source = "heuristic"

aligned = True
if llm and llm.get("bias") in ("bullish", "bearish") and llm["bias"] != want_bias:
    aligned = False
    final -= contra_penalty
final = max(0.0, min(100.0, final))

ml_strong = ml is not None and float(ml) >= avoid_override_ml
if not aligned and not ml_strong:
    verdict = "AVOID"
elif final >= threshold:                           # default 44
    verdict = "BUY" if direction == "long" else "SELL"
else:
    verdict = "WAIT"

return {
    "final_score": round(final, 1),
    "verdict": verdict,
    "aligned": aligned,
    "score_source": source,
    "ml_score": round(ml * 100.0, 1) if ml is not None else None,
    "llm_score": round(adj, 1) if has_adj else None,
    "llm_adjustment": round(adj, 1) if has_adj else None,
    "llm_quality": llm.get("quality") if llm else None,
    "llm_risk_flags": list(llm.get("risk_flags") or []) if llm else [],
}
```

---

## Worked examples

### Example 1 — Clean setup with positive LLM risk audit

```
direction        = "long"
ml_prob          = 0.43     -> 43.0
llm.bias         = "bullish"
llm.quality      = "high"
llm.score_adj    = +3

final   = 43.0 + 3.0 = 46.0
aligned = True
verdict = "BUY"   (46.0 >= 44)
```

### Example 2 — Borderline ML setup with HTF zone red flag

```
direction        = "long"
ml_prob          = 0.45     -> 45.0
llm.bias         = "bullish"
llm.quality      = "low"
llm.risk_flags   = ["htf_zone_blocking_tp", "wide_stop_atr"]
llm.score_adj    = -5

final   = 45.0 - 5.0 = 40.0
aligned = True
verdict = "WAIT"  (40.0 < 44 — red flags hold back a borderline entry)
```

### Example 3 — LLM contradicts direction (weak vs. strong ML)

```
Case A (ml_prob = 0.41 < avoid_override_ml_prob 0.42):
  aligned = False, ml_strong = False -> verdict = "AVOID"

Case B (ml_prob = 0.47 >= avoid_override_ml_prob 0.42, score_adj = 0):
  final   = 47.0 - 5.0 (contradiction_penalty) = 42.0 < 44
  aligned = False, ml_strong = True -> verdict = "WAIT" (not hard AVOID)
```

### Example 4 — Neutral LLM bias

```
direction     = "short"
ml_prob       = 0.45  -> 45.0
llm.bias      = "neutral"
llm.score_adj = 0

final   = 45.0
aligned = True (neutral does not contradict)
verdict = "SELL"  (45.0 >= 44)
```

---

## Score source transparency

`fuse` always returns `score_source` ∈ `{"ml+llm", "ml", "llm", "heuristic"}`:

| Source | Meaning | Dashboard shows |
|---|---|---|
| `ml+llm` | Both ML and LLM responded. | Full card: ML bar + LLM quality/adjustment, risk flags, and narrative. |
| `ml` | LLM disabled, skipped below `min_ml_prob`, or timed out/failed. | ML bar + transparent reason notice in AI box. |
| `llm` | ML model not trained (`50 + score_adjustment`). | LLM narrative, no ML bar. |
| `heuristic` | Neither available. | Confluence count + RR only. |

---

## Configuration knobs (`config.yaml` → `ai.hybrid.*`)

| Key | Default | Effect |
|---|---|---|
| `buy_threshold` | `44` | Final score needed for `BUY`/`SELL` (calibrated for Platt probabilities). |
| `min_llm_adjustment` | `-8.0` | Floor on LLM qualitative score adjustment (points). |
| `max_llm_adjustment` | `5.0` | Cap on LLM qualitative score adjustment (points). |
| `contradiction_penalty` | `5.0` | Score deduction when `llm.bias` actively opposes setup direction. |
| `avoid_override_ml_prob` | `0.42` | Minimum `ml_prob` required to bypass hard `AVOID` on directional disagreement. |
| `ml_weight` / `llm_weight` | `0.75` / `0.25` | Legacy fallback weights used only when re-scoring historical `confidence` records. |

---

## End-to-end pipeline recap

```
MT5 → SQLite → SMC engine → setup with features + LLM context
                       ↓                          ↓
                   ML predict_proba         LLM risk auditor (JSON)
                       ↓                          ↓
                    P(win)               bias / quality / score_adjustment
                       \___________  ___________/
                                   ↓
                          hybrid.fuse(setup, ai_cfg)
                                   ↓
                          {final_score, verdict, aligned, llm_adjustment, ...}
                                   ↓
                          dashboard card + execution gate
```
