# ⚡ AI Trader — SMC + Hybrid AI Trading Assistant

Ingests OHLC forex data from a **local MetaTrader 5** terminal, builds **Smart Money
Concepts** trade setups (market structure, liquidity sweeps, premium/discount zones,
order blocks, FVGs), scores them with a **hybrid AI** (ML win-probability model +
LLM analyst), and serves everything on an **HTML dashboard** — with automatic
setup journaling for forward validation.

```
MT5 ──► SQLite ──► SMC engine ──► trade setups ──► ML (P(win)) ─┐
                                            └──► LLM analyst ───┤──► hybrid verdict ──► dashboard
                                setups journal (forward validation)
```

## Quick start (no MT5 needed)

The repo is self-contained and portable. Two ways to run it:

### A) Fresh machine — one-time setup

```bat
setup.bat        :: creates venv, installs requirements, verifies imports
setx MINIMAX_API_KEY your-key-here   :: your LLM key (see config.yaml -> ai.llm)
```
`setup.bat` is idempotent and requires only Python 3.10+ on PATH. After it,
you can use any launcher below; each auto-picks the project venv if present
(falls back to system Python).

### B) Run it (bundled portable launchers — pin everything to your venv)

```bat
ingest.bat        :: synthetic OHLC for all symbols/timeframes  (demo)
train.bat         :: label historical setups + train the ML model
analyze.bat       :: SMC + hybrid AI on stored candles
serve.bat         :: open http://127.0.0.1:8000

outcomes.bat      :: resolve pending setups + print performance summary
insights.bat      :: auto-diagnosis + feature-importance report
trade.bat         :: live MT5 execution engine (OFF + dry-run by default)
check_env.bat     :: verify deps + model/env consistency
```

Or with an activated venv:

```bash
pip install -r requirements.txt
python -m app.main ingest --demo      # synthetic data so you can try it offline
python -m app.main train
python -m app.main analyze
python -m app.main serve             # dashboard refreshes on each bar close
```

**Recommended day-to-day:** `serve.bat` is the main loop — it runs the
bar-close watcher (analyzes confirmed M15/H1/H4 closes in parallel across your
CPU cores), keeps the CSM strip and setup journal fresh, resolves outcomes
automatically, and auto-retrains the model when the feature set or setup
builder changes. Editing `config.yaml` → `mtf.entry_tf` (default `M15`) sets
which timeframe generates setups; H1/H4 are context.

## Live MT5 setup

1. Install MetaTrader 5 on Windows and log in to your broker.
2. `pip install MetaTrader5` (already in requirements — Windows only).
3. Optionally set `terminal_path` / `login` / `password` / `server` in `config.yaml`
   (leave `login: null` to attach to an already-running terminal).
4. Make sure every symbol you want is visible in **Market Watch**
   (right-click → Show All, or add them manually).
5. ```bash
   python -m app.main ingest        # pull live history
   python -m app.main train --baseline
   python -m app.main analyze
   python -m app.main serve         # or: python -m app.main run --interval 60
   ```

## Live execution (auto-trading on qualifying setups)

The app can **place real MT5 orders** when a setup's hybrid verdict is
BUY/SELL: the engine scans fresh analyses every 15s, applies safety gates
(score ≥ 70, ML prob ≥ 0.50, position caps, cooldown, spread cap, optional
trading hours), sizes the lot to `risk_percent` of balance using the setup's
stop distance, and sends a market or limit order **with the setup's SL/TP
attached**. It only ever touches orders tagged with its own `magic` number.

It ships **OFF + DRY-RUN**. Safe rollout:

```bat
:: 1. rehearse: orders are logged, not sent — watch the Trade tab
ingest.bat && train.bat && analyze.bat
::    set execution.enabled: true (dry_run stays true), then:
serve.bat            :: Trade tab shows every would-be order with lot/price/SL/TP

:: 2. go live small: flip dry_run off (Trade tab button or config.yaml),
::    lower execution.risk_percent to 0.25-0.5

:: 3. manage: runtime toggle / flatten / trade log live on the Trade tab
::    (gates + magic/comment order identity are editable there too — persisted)
python -m app.main trade --status
python -m app.main trade --flatten
```

Full gate list, sizing math, entry types (market vs limit), failure modes and
the recommended validation workflow: [`docs/execution.md`](docs/execution.md).
**Validate with the setups journal first** — auto-trading an unvalidated
engine is gambling, not trading.

## The SMC engine (`app/smc/`)

| Module | What it detects |
|---|---|
| `structure.py` | Fractal swing highs/lows, **BOS** (continuation break) and **CHoCH** (change of character) on candle *closes* |
| `liquidity.py` | Equal highs/lows **liquidity pools**; **sweeps** (wick beyond a level, close back inside) of the last swing level and of pools; pools consumed once closed through |
| `pd_zones.py` | **Dealing range** from the last confirmed swing high/low, **equilibrium (50%)**, premium/discount classification |
| `order_blocks.py` | Last opposite candle before the structure-breaking impulse; tracks **mitigation** (retest) |
| `fvg.py` | 3-candle **fair value gaps**; tracks full fills |

## Trade setups (`app/engine/`)

A setup requires confluence of:
- market structure aligned **or** a fresh opposing **liquidity sweep** (trigger),
- a **fresh order block or FVG** on the right side of price as the entry zone,
- the zone sitting in **discount** (longs) / **premium** (shorts) of the dealing range,
- **RR ≥ min_rr** with the target taken at the nearest opposing liquidity pool.

Stop-loss goes beyond the zone edge / sweep wick (+ ATR buffer, with a minimum-risk floor).
Every qualified setup is **journaled** to the `setups_history` table for forward validation.

## Hybrid AI (`app/ai/`)

- **ML model** — `HistGradientBoostingClassifier` trained on historically generated
  setups labeled by forward simulation (TP before SL within `label_horizon_bars`).
  **15 features**: 11 setup-specific (RR, ATR%, range position, sweep depth/recency,
  confluence count, zone freshness, OB-vs-FVG, trend alignment, time-of-day) plus
  **4 per-symbol context features** (avg volatility, setup density, rolling win rate,
  realized RR) — one global model that learns pair-specific quirks without
  per-symbol models or cold-start problems.
- **LLM analyst** — any OpenAI-compatible endpoint (OpenAI, **Ollama** at
  `http://localhost:11434/v1`, LM Studio at `http://localhost:1234/v1`). Receives the
  full SMC + ML context JSON, returns bias / confidence / narrative / invalidation / concerns.
- **Fusion** — weighted score (`ml_weight` / `llm_weight`); contradictory LLM bias caps
  the verdict at `AVOID`; below threshold → `WAIT`. With no AI configured it falls back
  to a transparent confluence heuristic.
- **Drift safety net** — the model file records its training time and environment;
  the dashboard shows its age and a **↻ Retrain** button (always visible, amber when
  stale). Three modes via `ai.ml.auto_retrain.on_refresh`: `warn` (default — badge +
  button), `auto` (stale model retrains on every refresh), `manual`.
- **Validation tooling** — `train --baseline` saves reference metrics,
  `train --compare` prints the delta; every training run reports permutation
  feature importances.

## Outcome tracking & setup effectiveness

Every journaled setup is resolved against subsequent candles (WIN / LOSS /
EXPIRED, same conservative rules as the ML labeler — SL wins same-bar ties).
Outcomes power the Journal badges + summary strip (win rate, expectancy in R,
fill rate), the **Perf** tab (by verdict, score band, ML calibration, HTF
alignment, symbol, session), the `outcomes` CLI, and the live per-symbol ML
features. See `docs/outcome-tracking.md`.

## Multi-timeframe confluence (M15 entries, H1/H4 context)

M15 is the entry timeframe; H1/H4 are projected into every M15 setup as
confluence: HTF structure alignment, entry in the HTF discount/premium half,
entry at an HTF order block/FVG, HTF liquidity pools as take-profit targets,
plus CSM alignment. Four ML features (`htf_trend_align`, `htf_pd_alignment`,
`entry_in_htf_zone`, `htf_tp_distance_atr`) and an optional
`require_htf_bias` hard filter (off by default), and `entry_tf: M15` —
only the entry timeframe generates trade setups (others are context only) — see
`docs/mtf-confluence.md`. HTF bars are sliced with no look-ahead (only bars
closed before the entry bar), identical in training and live analysis.

## Currency Strength Meter (CSM28 port)

The CSM28 Pine concept is built in (`app/csm.py`): for each of the 28 standard
FX pairs, a z-score of close price over `lookback` bars; currency strength =
mean of the signed z-scores (base +1 / quote −1), smoothed over `ma_length`.
The engine auto-ingests the 28 pairs for every configured timeframe and stores
snapshots, which power:

- a **thin strip overlay on the chart** — 8 currencies sorted strongest → weakest
  (colored dot + symbol + signed σ value), strongest in gold, weakest in red
- the **MTF ALIGN pill** — base-vs-quote strength per timeframe for the selected pair
- the **alignment transitions feed** — pairs that just became aligned on all TFs

Config: `csm:` section (`lookback`, `ma_length`, `ma_type`, `display: MA|ROC`,
`bars`, `min_pairs`, `min_interval_seconds`).

## Dashboard

- Candlestick chart (TradingView lightweight-charts) with order-block & FVG boxes,
  premium/discount shading, EQ line, sweep arrows, BOS/CHoCH markers, entry/SL/TP lines.
- Setup cards: verdict badge, hybrid score, ML win-probability bar, LLM narrative +
  invalidation, confluence chips.
- **Symbol filter** — live search box narrows the symbol dropdown; **⚙ modal** to
  enable/disable symbols per pair (persisted in `data/symbol_selection.json`).
- **Model health** — ML badge shows the model's age; turns amber when stale;
  **↻ Retrain** button retrains on demand.
- "Run analysis" triggers a live ingest + analyze cycle; auto-refresh keeps it current.

## Commands

| Command | Purpose |
|---|---|
| `ingest [--demo] [--all-symbols]` | Pull OHLC into SQLite |
| `analyze [--all-symbols]` | Run SMC + ML + LLM on stored candles (auto-retrains after feature-set or builder upgrades) |
| `train [--all-symbols] [--baseline] [--compare]` | Label history, train ML; save/compare metrics baseline |
| `serve [--all-symbols] [--auto N] [--on-bar-close]` | Start the dashboard (bar-close watcher analyzes in parallel) |
| `run --interval 60` | Continuous ingest + analyze loop |
| `history [--symbol X] [--tf X] [--limit N]` | Print the journaled setups (forward-validation data) |
| `outcomes` | Resolve pending setups + print win rate / expectancy / calibration summary |
| `trade [--once] [--loop N] [--status] [--flatten] [--dry-run]` | Live MT5 execution engine (scan / inspect / panic-close) |
| `insights` | Auto-diagnosis (SL/TP/threshold/regime findings) + per-feature report |
| `migrate-journal` | Collapse duplicate journal rows into one per real setup (backs up first) |
| `list-symbols [--all-symbols]` | Print the symbol list the app will use |
| `select-symbols [--enable X ...] [--disable X ...] [--all] [--none]` | Manage the persistent enable/disable list |

## Symbol sources

By default the app trades the explicit list in `config.yaml → symbols:`. To
trade **every symbol currently visible in your MT5 Market Watch** instead,
either:

- Add `--all-symbols` to any command:
  ```bat
  python -m app.main list-symbols --all-symbols
  python -m app.main ingest --all-symbols
  python -m app.main train --all-symbols
  python -m app.main serve --all-symbols
  ```
- Or turn on permanent discovery in `config.yaml`:
  ```yaml
  discover:
    enabled: true
    group: "*"            # MT5 filter pattern: "*", "*USD*", "Forex\\*", ...
    only_tradeable: true  # skip non-tradeable instruments
    only_visible: true    # skip symbols not currently in Market Watch
    max_count: 200        # safety cap
  ```

Discovery filters, in order:
1. `group` — MT5 pattern (`mt5.symbols_get(group=...)`).
2. `only_visible` — keeps only symbols currently shown in Market Watch.
3. `only_tradeable` — drops symbols whose `trade_mode == DISABLED`.
4. `max_count` — caps the list (majors like EURUSD are sorted first).

The discovered list is persisted to `data/discovered_symbols.json` so the
dashboard can populate its dropdown even when MT5 isn't currently open.
On top of that, the per-symbol **enable/disable selection**
(`data/symbol_selection.json`) decides what actually gets ingested and
analyzed — manage it from the dashboard ⚙ modal or `select-symbols`.

**Tip**: open MT5 → right-click Market Watch → "Show All" to see every
symbol your broker offers, then run with `--all-symbols`. To restrict to a
category, use a narrower group pattern: `"*USD*"`, `"Forex\\*"`, `"Metals*"`,
etc.

## Keeping data current while `serve` runs

`serve` alone never pulls from MT5 — MT5 is pull-only. Three ways to automate
the pull, best first:

```bat
:: 1. BAR-CLOSE WATCHER (recommended): polls MT5 cheaply every 10s; analyzes
::    each pair the moment its bar CLOSES, using only confirmed bars —
::    matching how the ML model was trained. M15 fires at :00/:15/:30/:45.
serve.bat --on-bar-close

:: 2. TIMER: full ingest+analyze cycle every N seconds (partial last bar included)
serve.bat --auto 900

:: 3. Separate terminal running the loop
run.bat --interval 900
```

All three coexist with the dashboard's auto-refresh (which is view-only).
The header shows a `BAR-CLOSE` or `AUTO` badge when a mode is active, and the
status line reports each trigger (e.g. `bar closed EURUSD M1 → updated 14:30`).
Persist your choice in `config.yaml → dashboard` (`bar_close_watcher: true` or
`auto_run_interval: 900`).

**Why bar-close matters for the ML model:** the trainer always computed setups
on *closed* bars (labels are simulated forward from the bar after the setup).
The watcher analyzes with the still-forming bar dropped (`drop_forming_bar`),
so live inference sees exactly the same kind of data the model was trained on.

## Important: one environment for everything

The model file records the numpy version it was trained with. Training and
serving from **different environments** (e.g., a venv vs. system Python)
breaks loading. Always run every command from the same environment —
the included `.bat` launchers pin everything to `venv\Scripts\python.exe`:

```bat
setup.bat        :: bootstrap (venv + deps + verify)
serve.bat        :: dashboard (venv)
train.bat        :: retrain ML model (venv)
ingest.bat       :: pull MT5 data (venv)
analyze.bat      :: SMC + AI analysis (venv)
outcomes.bat     :: resolve setups + performance summary (venv)
insights.bat     :: diagnosis + feature report (venv)
check_env.bat    :: verify deps + model/env consistency (venv)
```

If the dashboard ML badge shows an env mismatch (model trained on a different
numpy than the running server), run `check_env.bat`, then `train.bat` from
the environment you serve with.

## Notes & disclaimer

- The ML model drifts with regime — `ai.ml.auto_retrain` (default `warn`)
  nags you via the dashboard badge; retrain weekly or after major regime shifts.
- The labeler is a simplified forward simulator (no spread/commission/slippage modeling).
- **Educational tool, not financial advice.** Trade at your own risk.

## Documentation

- [`docs/ml-method.md`](docs/ml-method.md) — how the win-probability ML model is
  trained: label simulation, the 15 features (setup-specific + per-symbol context),
  hyperparameters, metric interpretation, baseline comparison, and the
  auto-retrain modes.
- [`docs/smc-engine.md`](docs/smc-engine.md) — every SMC detector explained:
  fractal swings, BOS/CHoCH, equal-high/lows liquidity pools, sweeps, dealing
  range / premium / discount, order blocks, FVGs, and the orchestrator.
- [`docs/setup-builder.md`](docs/setup-builder.md) — how raw SMC context
  becomes a concrete trade setup: direction gate, zone selection, entry,
  stop-loss with ATR floor, take-profit from liquidity pools, RR cap, and
  confluence scoring.
- [`docs/hybrid-fusion.md`](docs/hybrid-fusion.md) — how the ML score and the
  LLM verdict combine into the final score and `BUY`/`SELL`/`WAIT`/`AVOID`
  verdict, with worked examples and failure-mode coverage.
- [`docs/dashboard-architecture.md`](docs/dashboard-architecture.md) — the
  FastAPI server, every API endpoint, vanilla-JS UI structure, lightweight-charts
  rendering, the zone-overlay trick, setup-card layout, and the refresh/polling
  lifecycle.
- [`docs/symbol-management.md`](docs/symbol-management.md) — how symbol lists
  are resolved (config / discovery / cache), per-symbol enable/disable selection,
  the dashboard filter and settings modal, and CLI for batch selection.
- [`docs/execution.md`](docs/execution.md) — live MT5 auto-trading: safety
  model, the ordered gate pipeline, risk-percent sizing, market vs limit
  entries, the `trades` log, dashboard Trade tab, CLI, and failure modes.
- [`docs/validation-guide.md`](docs/validation-guide.md) — staged validation
  workflow for real MT5 data: environment sanity, data quality, metric
  thresholds, per-symbol-feature ablation, dashboard checks, and forward
  paper-validation with the setups journal and ML-bucket calibration.
- [`docs/backtesting.md`](docs/backtesting.md) — methodology for evaluating the
  engine end-to-end against history, including a concrete implementation plan
  that reuses the existing walk-forward setup generator.
