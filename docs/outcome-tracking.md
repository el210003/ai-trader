# Outcome Tracking & Setup Effectiveness (Proposal)

**Status:** IMPLEMENTED (approved with phase 2 included)
**Motivation:** setups are generated every M15 bar close, but nothing measures
whether they actually work. The ML model trains on replayed labels; humans
never see how live verdicts perform. This closes the feedback loop.

## Goal

For every journaled setup, resolve what price actually did afterwards, and
surface aggregate performance so the user can answer:

1. Do BUY/SELL verdicts win more than WAIT ones? (validates the 60 threshold)
2. Does the hybrid score predict outcomes? (calibration)
3. Do HTF-aligned setups outperform? (decides `require_htf_bias`)
4. Which symbols / sessions / directions earn money? (expectancy, not win rate)

## Design

### 1. Outcome resolution engine (`app/engine/outcomes.py`)

New `outcomes` table, one row per setup:

```
outcomes(setup_id, symbol, tf, direction, resolved_at, result,
         filled, fill_time, r_multiple, mfe_r, mae_r, bars_to_outcome)
result ∈ WIN | LOSS | EXPIRED_UNFILLED | EXPIRED_OPEN | VOID
```

Resolution replays the entry-TF candles strictly after `formed_at`, using the
SAME conservative rules as the ML labeler (`app/engine/labeler.py`):

- limit order fills when price touches `entry`
- before fill: if SL is hit first → EXPIRED (never filled)
- after fill: **SL wins same-bar ties** (conservative)
- WIN → r_multiple = +rr of the setup; LOSS → −1.0
- horizon: `ai.ml.label_horizon_bars` bars after formation → EXPIRED_OPEN
  (partial: report MFE/MAE reached)
- VOID: data gap (candles missing) — retried next cycle

Runs automatically at each M15 bar-close cycle (cheap: only unresolved rows
of the last few days) and on demand via `POST /api/outcomes/resolve` +
`python -m app.main outcomes` CLI. Idempotent.

### 2. Journal upgrade (existing Journal tab)

- Each row gets an outcome badge: **WIN +2.1R · LOSS −1R · OPEN · EXPIRED**
  with bars-to-outcome and MFE/MAE on hover
- Summary strip on top: win rate, expectancy (R per setup), avg win R,
  fill rate, open count

### 3. Performance tab (new)

Aggregated views over resolved outcomes, each row filterable by symbol:

| View | Answers |
|---|---|
| By verdict (BUY/SELL/WAIT/AVOID) | is the 60 threshold meaningful? |
| By score band (60–70 / 70–80 / 80+) | does the hybrid score rank quality? |
| ML calibration (prob bucket vs realized win rate) | is the model honest? |
| By HTF trend alignment (from payload htf_metrics) | flip `require_htf_bias`? |
| By symbol / direction / session hour | where is the edge? |

Plain HTML tables + inline SVG bars — no new chart dependency.

### 4. Feedback into the model (phase 2 — implemented)

`SymbolStats.dynamic_features_live(store, symbol)` reads the rolling win rate
and realized RR from RESOLVED live outcomes (WIN/LOSS only — same semantics
as training labels; cold-start below `min_samples` returns None exactly like
training). `analyze_symbol` feeds these into every prediction, so the two
per-symbol dynamic features stay current between retrains. Training keeps
using replay labels (unchanged).

## Non-goals / notes

- No broker execution, no slippage/spread modeling — this is paper
  measurement of the generated levels (documented caveat).
- WAIT/AVOID setups ARE resolved (hypothetical outcomes) — they are exactly
  the counterfactual needed to validate thresholds.
- Old journal rows (H1/H4 era) resolve too, flagged by their tf — can be
  excluded from stats via the entry_tf filter.
- Training keeps using replay labels (unchanged, consistent with live rules).

## Effort

| Piece | Size |
|---|---|
| Store: outcomes table + queries | small |
| Resolver engine + pipeline hook + CLI | medium |
| Journal badges + summary strip | small |
| Performance tab | medium |
| Docs | small |

## Rollout decision after ~2–4 weeks of data

- expectancy by verdict → adjust `hybrid.buy_threshold`
- HTF-aligned vs not → decide `require_htf_bias`
- calibration curve → recalibrate ML weights or retrain
