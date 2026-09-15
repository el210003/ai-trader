# Live MT5 Execution

**Status:** IMPLEMENTED
**Motivation:** the pipeline already finds setups, scores them with the hybrid
AI, and journals them — but a human still has to read the dashboard and click
buttons in MT5. This module closes the loop: when a setup's verdict is
**BUY** or **SELL** (and every safety gate passes), the engine places the
corresponding order **in your MT5 terminal** with the setup's stop-loss and
take-profit attached.

```
analysis table ──► ExecutionEngine scan ──► gates ──► sizing ──► MT5 order (SL/TP attached)
     ▲                    │                                            │
     │                    ▼                                            ▼
  serve / CLI      trades log (SQLite)                          Trade tab / MT5
```

> **Disclaimer:** this is an educational tool, not financial advice. Auto-
> execution trades real money. Start in dry-run, use a demo account first,
> and never risk more than you can afford to lose.

## Safety model

The feature ships **OFF and in dry-run**. Nothing can place an order until you
explicitly turn it on — twice:

| Layer | Default | Meaning |
|---|---|---|
| `execution.enabled` | `false` | engine doesn't even start |
| `execution.dry_run` | `true` | orders are *recorded, not sent* |

Other structural safety properties:

- **Magic-number isolation** — every bot order is tagged with `execution.magic`
  (default `862001`). The engine counts, flattens, and dedups **only** its own
  orders; your manual trades in the same terminal are invisible to it, and it
  never modifies them.
- **Hard caps** — max open positions, max per symbol, max trades per day,
  per-symbol+direction cooldown (see config reference below).
- **SL/TP always attached** — a market or limit order is never sent without a
  valid stop-loss and take-profit on the correct side of entry (geometry is
  validated before sending).
- **Margin pre-check** — required margin is computed via `order_calc_margin`
  and must fit in free margin before the order is sent.
- **One owner of the trading connection** — order routing lives in the
  execution engine only. The ingest/analysis pipeline (including its
  ProcessPool workers) never sends orders.

## Architecture

`app/execution/executor.py::ExecutionEngine` **scans the SQLite `analysis`
table** every `execution.poll_seconds` (default 15s) instead of hooking the
pipeline. Why:

- every trigger path works identically — the bar-close watcher, the auto-run
  timer, the dashboard's "Run analysis" button, and the `analyze` CLI all
  write the same analysis snapshots;
- analysis runs in child processes for CPU parallelism; MT5 order IPC stays
  in exactly one thread of the main process;
- a crash in analysis can never take execution down with it (and vice versa).

Only setups on `mtf.entry_tf` (default `M15`) are traded — other timeframes
are context only, exactly like the rest of the app. Symbols are restricted to
the enabled symbol selection (same list the dashboard/pipeline uses).

Low-level MT5 plumbing lives in `app/execution/mt5_trader.py`: symbol
constraints (`volume_step`, `digits`, filling modes), risk sizing,
normalization, market/limit order sending, position/pending queries, close &
cancel. All functions take the `mt5` module as first argument, which makes the
whole layer testable with a stub (`scripts/smoke_execution.py` runs the full
engine against a fake MT5 — no terminal needed).

## The gate pipeline

Each fresh candidate setup must pass every gate, in order. The first failure
is the skip reason (shown on the Trade tab and in the engine summary):

1. **Verdict** — must be `BUY` or `SELL` (i.e. the hybrid score cleared the
   dashboard's threshold), and must match the setup direction.
2. **Geometry** — SL and TP must exist on the correct sides
   (long: `sl < entry < tp`; short: `tp < entry < sl`).
3. **Dedup** — the setup identity (`symbol|tf|direction|zone_origin|entry`,
   same scheme as the setups journal) must not have been executed before.
   *Any* attempt (filled, placed, dry-run, or rejected) blocks re-entry
   forever — so one setup can never fire twice.
4. **Score / ML gates** — `final_score ≥ min_score` (default 70, stricter
   than the dashboard's 60) and `ml_prob ≥ min_ml_prob` (default 0.50).
   Both are adjustable live from the Trade tab or `POST /api/execution/params`
   (see [Runtime overrides](#runtime-overrides)) — no restart needed.
5. **Position caps** — `max_open_positions` total and `max_per_symbol` per
   symbol, counting bot positions **and** pending orders.
6. **Daily cap** — `max_trades_per_day` filled/placed/dry-run executions since
   local midnight.
7. **Cooldown** — `cooldown_minutes` must have passed since the last bot
   execution on the same symbol + direction.
8. **Broker permissions** — symbol `trade_mode` must allow the direction
   (skips disabled / close-only / wrong-direction-only symbols).
9. **Spread cap** — current ask−bid spread must be ≤ `max_spread_points`.
10. **Trading hours** — optional `trading_hours: "07-20"` window, evaluated
    in **broker-server time** (from the tick timestamp).

Transient skips (spread, cooldown, caps) are **not persisted** — the same
setup may execute on a later pass while still fresh. Real attempts are always
recorded.

## Position sizing

By default each trade risks `risk_percent` % of the account **balance**,
measured to the setup's stop-loss:

```
risk_amount = balance × risk_percent / 100
loss_per_lot_at_sl = |entry − sl| × tick_value / tick_size     (account currency)
lot = risk_amount / loss_per_lot_at_sl
```

Example: EURUSD, $10,000 balance, 1% risk, 20-pip stop → $100 / $200-per-lot
= **0.5 lots**. The result is floored to the broker's `volume_step` and
clamped to `[volume_min, volume_max]`. If the sized lot is below the broker
minimum, the setup is skipped (`allow_min_lot: true` trades the minimum
instead — this increases real risk beyond `risk_percent`, hence default off).

Set `execution.fixed_lot` (e.g. `0.10`) to bypass risk sizing entirely.

## Entry types

| `entry_type` | Behavior |
|---|---|
| `market` (default) | Instant market order at ask (long) / bid (short). Requotes and stale prices are retried with a fresh tick (2 retries). |
| `limit` | Pending limit order parked **at the setup's zone entry** (`setup.entry`). No chasing: you're filled only if price retraces into the zone. If the zone is already at/past the market, it falls back to a market order. Pendings use GTC + engine-side expiry (portable across brokers that reject `ORDER_TIME_SPECIFIED`). |

Limit orders are usually the better match for SMC logic — setups are zone
retests, and `max_entry_distance_atr` already filters setups that ran too far
— but a market entry guarantees participation when the signal bar closes.

## Order lifecycle & the `trades` table

Every attempt writes one row to the new `trades` table:

```
trades(id, identity, symbol, tf, direction, verdict, score, ml_prob,
       entry, stop_loss, take_profit, rr, order_type, lot, requested_price,
       fill_price, ticket, deal, retcode, status, reason, placed_at, payload)
status ∈ filled | placed | dry_run | rejected | error
```

- `filled` — market order accepted (`TRADE_RETCODE_DONE`); `fill_price`/`ticket`/`deal` populated
- `placed` — limit order accepted (`TRADE_RETCODE_PLACED`)
- `dry_run` — recorded instead of sent
- `rejected` — broker refused; `retcode` + human-readable `reason` (e.g.
  *invalid stops*, *insufficient funds*, *autotrading disabled by terminal*)
- `error` — engine-side failure (MT5 connection etc.)

The `payload` column keeps the full setup snapshot (confluences, entry zone,
hybrid breakdown, engine parameters) so every execution is auditable after
the fact. Broker retcode 10008/10009/10010 count as success; transients
(10004/10020/10021) are retried.

## Stale pending expiry

Pendings older than `pending_expiry_minutes` (default 240) are canceled by
the engine on each scan. This mirrors the journal's `entry_valid_bars`
concept: a limit that never filled inside its validity window is stale — the
setup behind it will have been superseded by newer analyses.

## Configuration reference (`config.yaml → execution:`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | master switch |
| `dry_run` | `true` | record orders instead of sending |
| `magic` | `862001` | bot order tag; change if you run two bots on one terminal (**runtime-adjustable**, new orders only) |
| `entry_type` | `market` | `market` or `limit` |
| `deviation` | `20` | max slippage for market orders (points) |
| `risk_percent` | `1.0` | % of balance risked per trade (SL-distance sizing) |
| `fixed_lot` | `null` | fixed lot bypassing risk sizing |
| `allow_min_lot` | `false` | trade broker-minimum when sized lot is smaller |
| `min_score` | `70` | minimum hybrid final score to execute (**runtime-adjustable**, see gate overrides) |
| `min_ml_prob` | `0.50` | minimum ML win probability to execute (**runtime-adjustable**, see gate overrides) |
| `max_open_positions` | `3` | total bot positions + pendings |
| `max_per_symbol` | `1` | bot positions + pendings per symbol |
| `max_trades_per_day` | `5` | daily cap (local midnight reset) |
| `cooldown_minutes` | `240` | pause per symbol+direction after a trade |
| `fresh_seconds` | `120` | only setups from analyses newer than this |
| `max_spread_points` | `40` | skip when the spread is wider |
| `trading_hours` | `null` | `"07-20"` broker-server hours window |
| `pending_expiry_minutes` | `240` | engine cancels older pendings |
| `poll_seconds` | `15` | scan interval |
| `comment` | `ai-trader` | order comment visible in MT5 (**runtime-adjustable**, max 31 chars) |

> **Runtime overrides:** values set from the Trade tab or
> `POST /api/execution/params` (`min_score`, `min_ml_prob`, `magic`,
> `comment`) are stored in `data/execution_overrides.json` and take
> precedence over the values in this table (which are the static config.yaml
> baseline). Delete the overrides file to fall back to config.

## Using it

### Recommended rollout

1. **Watch first (dry-run):** set `execution.enabled: true` (keep
   `dry_run: true`), restart `serve.bat`. Every qualifying setup now shows up
   on the Trade tab as `DRY-RUN` with the exact lot, price, SL and TP the
   engine would send. Compare against your own judgment for a few days.
2. **Go live small:** flip `dry_run: false` (dashboard button or config),
   drop `risk_percent` to 0.25–0.5, keep a demo account if your broker
   offers one. Watch the first fills in MT5's Toolbox.
3. **Tighten:** tune `min_score` / `min_ml_prob` upward until the trade rate
   matches your tolerance — both are editable right on the Trade tab, so you
   can walk them up without touching config or restarting. The Journal +
   Performance tabs show which verdict buckets actually pay.

### Runtime overrides

Four execution parameters can be changed **while the engine is running** —
the two strictness gates plus the bot's order identity:

| Parameter | Range / validation | Effective |
|---|---|---|
| `min_score` | 0–100 | next engine scan |
| `min_ml_prob` | 0–1 | next engine scan |
| `magic` | positive integer | **new orders only** — see warning below |
| `comment` | any text, truncated to 31 chars (MT5 display limit) | next order |

| Surface | How |
|---|---|
| **Trade tab** | the *Gates* row (min score / min ML prob) and the *Order identity* row (magic / comment), each with an **Apply** button |
| **HTTP API** | `POST /api/execution/params` with any of `{"min_score": 65, "min_ml_prob": 0.6, "magic": 424242, "comment": "my-bot"}` |
| **config.yaml** | `execution.*` — the static baseline, read at engine start |

Behavior:

- API/UI changes are **persisted to `data/execution_overrides.json`** and
  re-applied on every engine start — so an adjustment survives restarts.
- Overrides **beat config.yaml**; delete the overrides file (or set the value
  back) to return to the config baseline.
- Gate values are clamped to valid ranges; `magic ≤ 0` is rejected.
- The live values are always visible: the Trade tab chips show the current
  magic/gates, and `trade --status` prints them (from `status()`, which reads
  the live snapshot — including overrides).
- ⚠️ **Changing the magic re-tags the bot.** Orders already placed under the
  old magic are no longer recognized as the bot's own: they disappear from
  the positions table and **Flatten will not close them** — close them
  manually in MT5 first if that's not intended. The engine logs a warning
  with the orphaned order count when this happens while connected. Typical
  reason to change it: running two bots on one terminal (each needs its own
  magic).
- The `comment` change also only applies to new orders; existing orders keep
  the comment they were placed with.
- Everything else (caps, cooldown, sizing, entry type, enabled/dry_run
  baseline) still requires a config edit + restart of `serve.bat` /
  `trade --loop`.

### Dashboard — Trade tab

New tab next to Perf. Shows: engine mode (OFF / DRY-RUN / LIVE), account
strip (balance/equity/free margin, warns when terminal autotrading is off),
open bot positions & pendings with live P/L, the full trade log with skip
reasons, and the controls: **Enable/Disable engine** (runtime toggle, not
persisted), **Switch to LIVE orders / Back to DRY-RUN**, **Flatten all**
(closes every bot position + cancels every bot pending in one click), and
two editor rows — **Gates** (*min score* / *min ML prob*) and **Order
identity** (*magic* / *comment*), each with an Apply button (persisted, see
[Runtime overrides](#runtime-overrides)). Editor inputs sync from the engine
status on every refresh, but never fight you while you're typing; changing
the magic asks for confirmation because it orphans existing bot orders.
Deep-link: `http://127.0.0.1:8000/#trade`.

### CLI

```bat
python -m app.main trade --status     :: config, account, bot positions, trade log
python -m app.main trade --once       :: single execution pass over fresh analyses
python -m app.main trade --loop 30    :: continuous scanning (standalone, no dashboard)
python -m app.main trade --flatten    :: panic button — close all bot positions/pendings
python -m app.main trade --dry-run --once   :: force dry-run regardless of config
```

`trade --loop` is for headless use; the dashboard runs the same engine on a
background thread when `execution.enabled: true` — don't run both against
the same `data/trader.db`.

### HTTP API

| Endpoint | Purpose |
|---|---|
| `GET /api/execution/status` | engine config, account, positions, pendings, last scan |
| `GET /api/execution/trades?limit=100` | trade log + aggregate stats |
| `POST /api/execution/toggle` | `{"enabled": bool, "dry_run": bool?}` runtime switch |
| `POST /api/execution/params` | `{"min_score"?, "min_ml_prob"?, "magic"?, "comment"?}` adjust gates + order identity at runtime (persisted) |
| `POST /api/execution/flatten` | close all bot positions + cancel pendings |

## Verification without a terminal

`python scripts/smoke_execution.py` runs the full engine — dry-run, dedup,
cooldown, score gate, live fill, limit placement, direction-mismatch gate,
short-side execution — against a stubbed MT5 module and temp databases. Use
it after any change to the execution layer.

## Failure modes & FAQ

| Symptom | Cause / fix |
|---|---|
| Trade tab: *autotrading OFF in terminal* | MT5 → Tools → Options → Expert Advisors → enable **Allow algorithmic trading**; also the toolbar AutoTrading button must be green. |
| Everything rejected with *market closed* | Forex weekend / rollover; the engine keeps the rejection in the log and moves on. |
| *symbol not available* | Add the symbol to Market Watch (or rely on `symbol_select` — the engine auto-attaches selectable symbols). |
| *sizing: tick value* error | Broker reports zero `trade_tick_value` for the symbol (rare, e.g. some indices). Set `fixed_lot`. |
| Lots skipped as *below broker minimum* | Small balance + wide stop. Raise `risk_percent` or set `allow_min_lot: true` (accepts extra risk). |
| No trades even with BUY/SELL setups on screen | Check the skip reasons under the last scan — usually `min_score` / `min_ml_prob` / cooldown / caps. Analyses older than `fresh_seconds` are also ignored (run an analysis cycle). |
| Two bots on one terminal | Give each a different `magic`. |
| Netting accounts | MT5 netting merges positions per symbol; with `max_per_symbol: 1` the engine opens at most one position per symbol anyway, so behavior matches. On hedging accounts positions stay separate tickets. |

## Deliberate limitations (v1)

- **No trade management** beyond the attached SL/TP — no trailing stops, no
  partial takes, no breakeven moves. MT5 manages the exit; the engine's job
  is entry + risk.
- **No outcome linking** — `trades` rows are not auto-resolved against the
  setups journal (the journal continues to track hypothetical outcomes for
  model validation; realized P/L lives in your MT5 account history).
- **One setup → one order** — no scaling in/out.
- The engine trusts the terminal it attaches to via the existing `mt5:`
  config (same credentials model as ingestion).

Related: [`docs/setup-builder.md`](setup-builder.md) (how entry/SL/TP are
derived), [`docs/hybrid-fusion.md`](hybrid-fusion.md) (how BUY/SELL verdicts
are produced), [`docs/outcome-tracking.md`](outcome-tracking.md) (how setups
are validated — run it *before* enabling live execution).
