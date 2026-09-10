# Hybrid Fusion: ML + LLM → Verdict

This document explains how the **ML win-probability** and the **LLM analyst**
are combined into the final score and verdict shown on the dashboard.

Entry point: `app/ai/hybrid.py::fuse(setup, ai_cfg)`.

---

## What fusion has to solve

A single number can't capture both *quantitative* (RR, ATR, structure
alignment) and *qualitative* (narrative context, macro concerns, news
proximity) signals. The hybrid design:

1. Lets the **ML model** act as a calibrated probability engine trained on
   historical outcomes.
2. Lets the **LLM** act as an expert SMC trader that reads the full context
   and produces a narrative judgment.
3. Combines them with **configurable weights** and **alignment checks**, with
   a transparent **fallback** when either is unavailable.

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
`None` if disabled / key missing / endpoint unreachable / parse failed.

```python
{
  "bias": "bullish" | "bearish" | "neutral",
  "confidence": 0..100,
  "narrative": "<2-4 sentence professional analysis>",
  "invalidation": "<what specifically invalidates this setup>",
  "concerns": ["<risk factor>", ...],
  "verdict": "BUY" | "SELL" | "WAIT" | "AVOID",
  "model": "<model name>"
}
```

The LLM never raises — failures are swallowed and return `None` so the
dashboard keeps working.

---

## Fusion algorithm

```python
direction = setup["direction"]                   # "long" or "short"
want_bias = "bullish" if direction == "long" else "bearish"

scores, weights = [], []
if ml_prob is not None:                          # ML present?
    scores.append(ml_prob * 100)
    weights.append(ml_weight)                    # default 0.6
if llm and llm.confidence is not None:            # LLM present?
    scores.append(llm.confidence)
    weights.append(llm_weight)                    # default 0.4

if scores:
    final = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
    source = "ml+llm" if len(scores) == 2 else ("ml" if ml else "llm")
else:
    # no AI available -> transparent confluence-based heuristic
    final = min(90.0,
                35.0
                + 8.0 * len(setup.confluences)
                + (5.0 if setup.rr >= 2.0 else 0.0))
    source = "heuristic"

# alignment: LLM contradicts the trade direction?
aligned = True
if llm and llm.bias in ("bullish", "bearish") and llm.bias != want_bias:
    aligned = False
    final -= 20.0                                  # penalty for contradiction
final = max(0.0, min(100.0, final))

# verdict assignment
if not aligned:
    verdict = "AVOID"
elif final >= buy_threshold:                      # default 60
    verdict = "BUY" if direction == "long" else "SELL"
else:
    verdict = "WAIT"

return {final_score, verdict, aligned, score_source, ml_score, llm_score}
```

---

## Worked examples

### Example 1 — ML and LLM both agree

```
direction = "long"
ml_prob   = 0.68     -> 68
llm.bias  = "bullish"
llm.conf  = 72

weighted  = (68 * 0.6 + 72 * 0.4) / (0.6 + 0.4)
          = (40.8 + 28.8) / 1.0
          = 69.6

aligned   = True (LLM bullish == want bullish)
verdict   = "BUY"   (69.6 >= 60)
```

### Example 2 — LLM contradicts

```
direction = "long"
ml_prob   = 0.65     -> 65
llm.bias  = "bearish"
llm.conf  = 70

weighted  = (65 * 0.6 + 70 * 0.4) / 1.0  = 67.0
aligned   = False → final -= 20  → 47.0
verdict   = "AVOID"   (regardless of score)
```

This is deliberate: if the LLM disagrees with the trade direction, the
system refuses to enter. Even a high ML score is overridden.

### Example 3 — LLM only

```
direction = "short"
ml_prob   = None              # model not trained
llm.bias  = "bearish"
llm.conf  = 78

weighted  = 78 * 0.4 / 0.4 = 78.0
aligned   = True
verdict   = "SELL"   (78 >= 60)
```

The `score_source` field in the response is `"llm"` — the dashboard can
disclose that the score is LLM-only.

### Example 4 — Neither

```
direction = "long"
ml_prob   = None
llm       = None
confluences = ["market structure aligned",
               "sellside liquidity sweep",
               "entry in discount zone",
               "fresh order block"]    # 4 confluences
rr = 2.4

final = min(90, 35 + 8 * 4 + 5) = min(90, 72) = 72
verdict = "BUY"   (72 >= 60)
```

`score_source = "heuristic"` — the dashboard shows the score without an ML
bar and without LLM narrative.

### Example 5 — Neutral LLM bias (the LLM hedges)

```
direction = "long"
ml_prob   = 0.55  -> 55
llm.bias  = "neutral"      # LLM declined to commit
llm.conf  = 50

weighted  = (55 * 0.6 + 50 * 0.4) / 1.0 = 53.0
aligned   = True (neutral doesn't contradict)
verdict   = "WAIT"   (53 < 60)
```

`bias = neutral` is treated as non-contradictory — only a firm opposite bias
triggers `AVOID`. This is intentionally lenient; tightening this is a
configurable knob (see Possible improvements).

---

## Score source transparency

`fuse` always returns `score_source` ∈ `{"ml+llm", "ml", "llm", "heuristic"}`
so the dashboard can disclose how the score was produced:

| Source | Meaning | Dashboard shows |
|---|---|---|
| `ml+llm` | Both ML and LLM responded and agreed. | Full card: ML bar + LLM narrative. |
| `ml` | LLM disabled / unreachable. | ML bar, no narrative. |
| `llm` | ML model not trained. | LLM narrative, no ML bar. |
| `heuristic` | Neither available. | Confluence count + RR only. |

The `aligned` flag is also surfaced so you can spot setups where the LLM
disagreed but the verdict didn't end up `AVOID` (i.e., LLM was neutral or
unavailable).

---

## Why a weighted average, not a hard rule

Considered alternatives:

| Approach | Why we didn't pick it |
|---|---|
| **ML wins, LLM only narrates** | Wastes the LLM's value as an independent second opinion. |
| **LLM wins, ML only informs** | Throws away the hard-won calibration from ~2,000 labeled samples. |
| **Either passes → take the trade** | No alignment check → ML and LLM could silently disagree on direction. |
| **AND-gate: both must pass** | Too strict — only fires when ML is already strong AND LLM is bullish. Loses setups where ML is borderline but LLM sees something the model can't. |
| **Hard-coded weights** | Intransparent. Configurable weights let you tune for your risk tolerance. |

The weighted average with an **alignment veto** is a compromise: both voices
count, but a strong contradiction short-circuits to `AVOID`.

---

## Configuration knobs (`config.yaml` → `ai.hybrid.*`)

| Key | Default | Effect |
|---|---|---|
| `ml_weight` | `0.6` | How much the ML score counts. Higher = trust the model more. |
| `llm_weight` | `0.4` | How much the LLM confidence counts. Higher = trust the LLM more. |
| `buy_threshold` | `60` | Final score needed for `BUY`/`SELL`. Raise for stricter entry, lower for more trades. |

The weights are renormalized when only one of the two is available — so
turning off the LLM doesn't artificially suppress the ML score.

---

## Failure modes handled

| Situation | Behavior |
|---|---|
| No ML model trained | `ml_prob = None`; weighted average uses LLM only; score source = `"llm"` or `"heuristic"`. |
| LLM disabled in config | `llm = None`; uses ML only or heuristic. |
| LLM endpoint unreachable / 401 | `LLMAnalyzer.analyze_setup` returns `None`; treated as LLM disabled. |
| LLM returns malformed JSON | Robust parser (`_parse`) strips fences, finds outermost `{...}`, drops fields that fail validation. Returns normalized dict. |
| LLM bias is "neutral" | Treated as non-contradictory; doesn't trigger `AVOID`. |
| ML returns 0% or 100% probability | Used as-is. The system trusts the model's calibration; retrain if you see extreme outputs too often. |
| Both `score` components missing | Falls back to confluence heuristic — the dashboard still shows *something*. |

---

## Possible improvements

- **Tiered alignment** — `neutral` LLM bias could count as a half-contradiction
  (penalize but don't AVOID).
- **Calibrated LLM confidence** — wrap the LLM confidence in a sigmoid so
  `50/100` actually means `P(win) = 0.5`, not just a 0–100 scale.
- **Configurable scoring rubric** — `weights` as a dict per confluence type
  (sweep depth × k, RR × k, etc.) instead of a single heuristic bonus.
- **Persistence of LLM disagreements** — log `(setup_id, llm_bias, ml_prob)`
  pairs so you can audit where the two signals diverge over time.
- **Multi-model ensemble** — train 3 models with different seeds / feature
  subsets and average their probabilities for more stable predictions.

---

## End-to-end pipeline recap

```
MT5 → SQLite → SMC engine → setup with features + LLM context
                       ↓                          ↓
                   ML predict_proba         LLM analyst (JSON)
                       ↓                          ↓
                    P(win)                  bias/confidence/narrative
                       \___________  ___________/
                                   ↓
                          hybrid.fuse(setup, ai_cfg)
                                   ↓
                          {final_score, verdict, aligned, ...}
                                   ↓
                          dashboard setup card
```
