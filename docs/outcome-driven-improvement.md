# Outcome-Driven Improvement (IMPLEMENTED tiers 0–3)

**Status:** APPROVED & BUILT (tiers 0–3). Tier 4 (shadow testing) deferred.
**RL decision:** full RL rejected at current data scale (needs 10⁴–10⁶
interactions; simulator-gap reward hacking; the take/skip decision with fixed
SL/TP is already near-optimally solved by the supervised prob + threshold).
Revisit RL only for exit management later; the honest intermediate step is a
**contextual bandit** (e.g. Thompson sampling over `buy_threshold` per regime),
which becomes viable once the outcome table has months of data.
**Goal:** turn resolved outcomes from a scoreboard into a control loop:
**measure → diagnose → adapt → verify**.

## The core loop

```
resolve outcomes ──► insight engine finds weak spots ──► change config/features
        ▲                                                      │
        └────────── shadow test verifies before adoption ◄─────┘
```

## 0. Foundation: journal what the model sees (prerequisite)

`append_setups` payload currently stores only {confluences, entry_zone,
range_position, llm_score, aligned, score_source}. Add **`features`** (all 19)
and **`htf_metrics`** — every diagnosis below needs them. Also extend the
resolver: after a LOSS, keep scanning to the horizon and record
**`post_loss_tp_hit`** (did price reach TP after stopping us out?) — the
single most direct measure of SL placement quality.

## 1. Insight engine (`app/engine/insights.py`) — automatic diagnosis

Scans resolved outcomes + journaled features and emits findings with evidence,
a suggested config change, and a **minimum-sample guard** (buckets under
~20 resolved are suppressed — no noise-chasing):

| Insight | Evidence | Suggests |
|---|---|---|
| **SL too tight / too loose** | % of losers with `post_loss_tp_hit`; MAE of losers vs 1.0R | `sl_buffer_atr`, `min_risk_atr` |
| **TP too far** | MFE distribution of EXPIRED_OPEN vs planned RR | `min_rr`, TP at MFE percentile |
| **min_rr mispriced** | expectancy by RR bucket | raise/lower `min_rr` |
| **Dead triggers** | win rate by sweep_recency / zone_freshness / confluence_count terciles | prune or rework builder filters |
| **Threshold miscalibrated** | expectancy-maximizing score band; ML bucket realized vs predicted | `hybrid.buy_threshold`, `ai.llm.min_ml_prob` |
| **LLM adds no value** | outcomes when LLM agreed vs contradicted ML | `hybrid.llm_weight`, gate |
| **Losing regimes** | negative-expectancy session / symbol / direction | avoid list, symbol selection |
| **Horizon too short** | EXPIRED_OPEN share with rising MFE | `label_horizon_bars` |

Surfaced in the Perf tab ("Insights & Recommendations" section) and an
`insights` CLI. Never auto-applies — recommends.

## 2. Feature discrimination report

For each of the 19 ML features: tercile the feature value across resolved
outcomes → win rate per tercile. Flags **dead features** (flat terciles) and
**monotone-powerful** ones. This is the evidence base for the next feature-set
revision (v4) — keep powerful features, repair or drop dead ones.

## 3. Live-outcome training blend (ML improvement)

Training today uses replay labels (synthetic setups from historical candles).
Add resolved live outcomes as additional/override training rows — they are
ground truth for the exact distribution the engine trades (policy learning).
Guardrails: `ai.ml.live_outcomes: {enabled, min_samples (e.g. 200)}`,
dedupe vs replay rows, compare cv_auc replay-only vs blended, keep the better.

## 4. Shadow testing (paper A/B for builder changes)

Run the setup builder each cycle with a variant config (e.g.
`sl_buffer_atr: 0.5`, `min_rr: 2.0`, `require_htf_bias: true`) into a shadow
journal; resolve identically; the Perf tab compares production vs variant
expectancy with sample-size guards. Verify changes **before** touching live
config — closes the loop safely.

## Build order

- **Tier 0 — Foundation**: journal `features` + `htf_metrics` in the setup
  payload; resolver records `post_loss_tp_hit` (TP reached after a LOSS) and
  updates MFE/MAE on the resolution bar itself.
- **Tier 1 — Insight engine** (`app/engine/insights.py`): the diagnosis table
  below, each insight with evidence + suggested config key + n-guard
  (buckets < `min_bucket` resolved suppressed; never auto-applies). Surfaced
  in the Perf tab and the `insights` CLI.
- **Tier 2 — Feature discrimination report**: per-feature tercile win rates
  across resolved outcomes → powerful / weak / dead verdicts; feeds the next
  feature-set revision.
- **Tier 3 — Live-outcome training blend**: `ai.ml.live_outcomes` blends
  resolved live outcomes (WIN/LOSS → 1/0) into training alongside replay
  labels, deduped by (symbol, tf, formed_at, direction); trains both blended
  and replay-only, keeps the higher cv_auc; `metrics` reports both counts.
- **Tier 4 — Shadow testing** (deferred): paper A/B for builder changes.

## Sample-size honesty

M15 × ~21 symbols ≈ tens of setups/day. Buckets stabilize over weeks, not
days. The insight engine must show `n` for every claim and suppress
underpowered ones — otherwise it optimizes noise.
