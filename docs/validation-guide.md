# Validation Guide

How to validate the full workflow on **real MT5 data** — from environment
sanity to forward paper validation. Follow the stages in order; each builds
on the previous one.

---

## Overview

| Stage | What it validates | Time |
|---|---|---|
| 0. Environment sanity | MT5 connection + symbol discovery | 5 min |
| 1. Data quality | Real OHLC is clean and current | 10 min |
| 2. Train + interpret | ML model quality on YOUR data | 15 min |
| 3. Analysis pipeline | SMC + AI produce sensible setups | 5 min |
| 4. Dashboard | UI renders everything correctly | 5 min |
| 5. Forward validation | Model calibration on live data | 2–4 weeks |

---

## Stage 0 — Environment sanity

First, verify the environment is consistent (this catches the #1 setup
problem — training and serving from different Pythons/numpy versions):

```bat
check_env.bat
```

**Expect:** `[OK] environment is consistent` — all 9 deps present, model
loads, and the model's training numpy matches the runtime numpy.

Then, with MT5 open and logged in:

```bat
python -m app.main list-symbols --all-symbols
```

**Expect:** every symbol in your Market Watch, majors sorted first.

**If empty / warn:** MT5 not running, wrong `terminal_path` in
`config.yaml`, or session not logged in.

---

## Stage 1 — Data quality

```bat
python -m app.main ingest
```

**Expect:** ~1500 bars per symbol/timeframe; latest timestamp ≈ current
market time (MT5 returns UTC — mind your timezone when reading it).

Then spot-check data quality:

```python
from app.config import load_config
from app.data.store import Store
cfg = load_config(); store = Store(cfg["storage"]["path"])

for sym in cfg["symbols"]:
    for tf in cfg["timeframes"]:
        df = store.load_candles(sym, tf)
        if df.empty:
            print(f"{sym} {tf}: EMPTY"); continue
        bad_ohlc = ((df.high < df.low) | (df.high < df.close) | (df.low > df.close)).sum()
        gaps = (df.time.diff().dropna() > 0).sum()
        print(f"{sym} {tf}: {len(df)} bars | bad OHLC rows: {bad_ohlc} | gaps: {gaps} | "
              f"last: {df.time.iloc[-1]}")
```

**Expect:** `bad OHLC rows: 0` everywhere. A few gaps are normal (weekends,
holidays, illiquid hours).

**Red flag:** any bad OHLC rows → broker data issue; do not train on that
pair until resolved.

---

## Stage 2 — Train + interpret metrics

```bat
python -m app.main train --baseline
```

Read the output against these thresholds:

| Metric | Red | Yellow | Green | Notes |
|---|---|---|---|---|
| `n_samples` | < 100 | 100–500 | > 500 | Pull more symbols/bars if red |
| `cv_auc` | < 0.52 | 0.52–0.58 | 0.58–0.65 | > 0.68 = suspect look-ahead, investigate |
| `win_rate` | < 25% | 25–40% | > 40% | See note below on break-even |
| Feature importances | all ~0 | — | spread out | `symbol_*` features should register |

### About win_rate and the 40% break-even

Break-even win rate at `min_rr = 1.5` is `1 / (1 + 1.5) = 40%`. But the
labeled win rate is **conservative**:

- same-bar SL-first assumption
- no spread/slippage modeling
- limit-order fills only

Real trading with limit entries typically realizes slightly *better* than
the label. So a labeled 30–35% win rate can still be viable. Below 25%
labeled means the setup filter is selecting noise on your data — try
tightening `min_rr` to `2.0` and retraining; if quality doesn't improve,
the pair/timeframe mix needs work.

### Ablation check — do the per-symbol features matter on YOUR data?

```python
from app.config import load_config
from app import pipeline
from app.ai import features as F

cfg = load_config()
orig = list(F.FEATURES)

F.FEATURES[:] = [f for f in orig if not f.startswith("symbol_")]   # ablate
m_old = pipeline.train(cfg, verbose=False)
print("without symbol_*:", {k: m_old.get(k) for k in ("n_samples", "cv_auc")})

F.FEATURES[:] = orig                                               # restore
m_new = pipeline.train(cfg, verbose=False)
print("with symbol_*:   ", {k: m_new.get(k) for k in ("n_samples", "cv_auc")})
```

**Decision rule:**

- `cv_auc(with) − cv_auc(without) ≥ +0.005` → per-symbol features earn
  their place.
- Delta ≤ 0 → drop the weak features (usually `symbol_setup_density`,
  which often shows ~0 importance) and keep the rest.

### Baseline comparison over time

```bat
python -m app.main train --baseline    :: save reference metrics
python -m app.main train --compare     :: later: delta vs reference
```

Use this whenever you change features, config, or symbol list. A
meaningful change is **±0.005 AUC or more**.

---

## Stage 3 — Analysis pipeline

```bat
python -m app.main analyze
```

**Expect:**

- Each pair reports setups or "no qualified setups" — the filter is strict,
  so zero setups on some pairs is *normal*, not a bug.
- ML% values spread across a range (roughly 5–60%), not all pinned at one
  number (all-same ⇒ model degenerate → check `n_samples`).
- Exotic symbols with < 250 bars are skipped gracefully.

**LLM check:** set `OPENAI_API_KEY` (or point `ai.llm.base_url` at Ollama),
re-run. Qualifying setups should gain narrative + invalidation text on the
dashboard. If the LLM is down, everything still works — the hybrid falls
back to ML-only scoring.

---

## Stage 4 — Dashboard

```bat
python -m app.main serve
```

Open http://127.0.0.1:8000 and verify:

- [ ] Candles render for the selected symbol/timeframe
- [ ] OB / FVG boxes appear (toggle in legend)
- [ ] Sweep arrows + BOS/CHoCH circles visible
- [ ] Setup cards show ML probability bars and confluence chips
- [ ] "Run analysis" works — status dot amber → green
- [ ] ⚙ modal lists your MT5 symbols with paths
- [ ] Symbol filter (search box) narrows the dropdown live
- [ ] Disabling a symbol in ⚙ removes it from dropdown + refresh after Save

---

## Stage 5 — Forward validation (the real test)

Backtest/training metrics are suggestive; forward paper validation is
proof. Run the continuous loop:

```bat
python -m app.main run --interval 900
```

Each cycle: fresh ingest → fresh analysis → updated dashboard. **Every
qualified setup is automatically journaled** to the `setups_history` table
(deduped per symbol/tf/bar/direction), so no manual spreadsheet is needed.

Review the journal any time:

```bat
python -m app.main history                       :: last 50, all pairs
python -m app.main history --symbol EURUSD --tf H1 --limit 100
```

or via the API:

```
GET /api/setups/history?symbol=EURUSD&tf=H1&limit=200
```

Each row stores: formed_at, direction, verdict, hybrid score, ML
probability, RR, entry/SL/TP, and confluences. After **30+ journaled
trades**, compute:

### 1. Calibration by ML bucket (the key check)

Group journaled trades by predicted ML probability and compare bucket win
rates (add the outcome column manually, or export with sqlite3):

```bat
sqlite3 data/trader.db "SELECT ml_prob, verdict, COUNT(*) FROM setups_history
                        GROUP BY 1, 2 ORDER BY 1;"
```

| ML bucket | Trades | Actual win rate |
|---|---|---|
| 20–30% | ... | ... |
| 30–40% | ... | ... |
| 40–50% | ... | ... |
| 50–60% | ... | ... |

**If the model is calibrated, higher bucket → higher realized win rate.**
If buckets are flat (same win rate everywhere), the model's ranking is
useless on live data regardless of its CV AUC.

### 2. Realized vs labeled win rate

Forward results should land close to (usually slightly above) the training
label rate. A big gap downward means the live setup distribution differs
from the training distribution (regime change, or broker feed differences).

### 3. Verdict quality (threshold tuning)

- Most skipped WAIT setups would have lost → filter works; consider
  *raising* `buy_threshold` for even stricter entries.
- Most skipped WAIT setups would have won → lower `buy_threshold`
  (e.g. 50) and re-validate.

### 4. Cost reality check

Track average realized R per trade vs the setup's stated RR. With spread +
slippage, expect realized ≈ stated RR − ~10%. A larger haircut means your
broker's costs are eating the edge — prefer pairs/timeframes with bigger
targets.

---

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `MT5 initialize() failed` | Terminal closed / wrong path / not logged in | Open MT5 first; set `terminal_path` in config |
| `No rates returned for X` | Symbol not in Market Watch | Add it in MT5 (right-click → Symbols) |
| `n_samples` tiny | Not enough history | Raise `data.bars` to 3000+; add symbols; lower `label_step` to 2 |
| Everything `WAIT` | Model not yet calibrated on your data | Expected early; lower `buy_threshold` temporarily to 45 to inspect borderline setups |
| Training > 5 min | Many symbols × small step | Normal for 20+ pairs; use `label_step: 6` |
| Ingest re-downloads everything | — | It's `INSERT OR REPLACE` — idempotent, only new bars appended |
| cv_auc > 0.70 on first train | Look-ahead leak (shouldn't happen) | Report it — inspect `features.py` and the labeler before trusting anything |

---

## Sign-off checklist

Before trusting the system with real risk:

- [ ] Stage 1: zero bad OHLC rows across all pairs
- [ ] Stage 2: `n_samples` > 500, `cv_auc` in 0.55–0.65 band
- [ ] Stage 2: ablation shows per-symbol features help (≥ +0.005 AUC)
- [ ] Stage 3: ML probabilities are spread, not pinned
- [ ] Stage 4: all dashboard checks pass
- [ ] Stage 5: ≥ 30 forward trades logged
- [ ] Stage 5: ML buckets show monotonic win rates (calibration)
- [ ] Retrain cadence scheduled (weekly, or after major regime shifts)

---

## TL;DR

1. `list-symbols --all-symbols` → MT5 sees your symbols
2. `ingest` → clean, current OHLC
3. `train --baseline` → n_samples, cv_auc, win_rate thresholds
4. Ablation snippet → prove symbol_* features help on your data
5. `analyze` + `serve` → setups + dashboard behave
6. `run --interval 900` for 2–4 weeks → log trades → check ML-bucket
   calibration — **this is the validation that actually matters**
