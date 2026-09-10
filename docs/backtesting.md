# Backtesting

This document explains how to **evaluate the SMC engine end-to-end against
historical data**, going beyond just training the ML model. It covers the
philosophy, a practical methodology, and a concrete implementation plan.

> The repo currently uses a *training-time* labeler (which only counts setups
> for ML training). This doc describes how to add a **standalone
> backtester** that simulates full trading sessions and reports performance.

---

## Why backtest separately

The training labeler (`app/engine/labeler.py`) and a real backtest share the
same forward simulator at heart, but they answer different questions:

| Question | Tool |
|---|---|
| "Given this setup's features, was the outcome a win?" | `labeler.label_setup` (ML training) |
| "If I had traded every setup over the last year, what would my equity curve look like?" | Backtester |
| "Does my strategy beat a buy-and-hold baseline?" | Backtester |
| "What's my drawdown, exposure, profit factor, Sharpe?" | Backtester |

The backtester needs more than win/loss labels — it needs to **track
positions across time**, handle **multiple overlapping setups** (or enforce
that we only have one position at a time), and account for **costs**.

---

## Methodology

### Step 1 — Define the trading rules

Be explicit about every decision the strategy makes:

| Decision | Rule |
|---|---|
| **Entry trigger** | Place a limit order at the setup's entry price when it forms. |
| **Entry validity** | Cancel the order after `N` bars if not filled. |
| **Stop-loss** | At the setup's `stop_loss` (ATR-buffered zone edge). |
| **Take-profit** | At the setup's `take_profit` (nearest opposing liquidity pool). |
| **Position sizing** | Fixed fractional (e.g., 1% of equity risked per trade). |
| **Concurrent positions** | At most one open position per symbol/timeframe. |
| **Session filter** | Only enter during London / NY overlap (or whatever you choose). |
| **News filter** | Optional — skip setups formed within N hours of high-impact news. |

These rules must be written down *before* running the backtest — otherwise
you'll fit them to the result.

### Step 2 — Walk forward, never look ahead

```
for each bar i in history:
    snapshot = df.iloc[:i+1]
    setups = run_smc(snapshot)
    for setup in setups:
        if setup.formed_index != i: continue   # only act on fresh setups
        if already in position: continue        # optional rule
        place_limit_order(setup.entry, setup.sl, setup.tp)
```

The setup's `formed_index` is `i` (the last bar of the snapshot). Anything
older is already in the order book or irrelevant.

### Step 3 — Simulate order execution

For each pending limit order, scan forward:

```python
for k in range(i+1, i+1+H):
    if not filled:
        if long  and df.low.iloc[k]  <= entry: filled = True
        elif short and df.high.iloc[k] >= entry: filled = True
        else: continue
    # in trade — conservative SL first on same bar
    if long:
        if df.low.iloc[k]  <= sl: close(sl, k); break
        if df.high.iloc[k] >= tp: close(tp, k); break
    else:
        if df.high.iloc[k] >= sl: close(sl, k); break
        if df.low.iloc[k]  <= tp: close(tp, k); break
else:
    if filled: close(close_price, k)   # timeout exit at last close
    else: cancel(order)                # never filled
```

### Step 4 — Track equity

```python
equity = initial_equity
positions = {}
trades = []

for i in range(...):
    ... (as above) ...
    if trade_closed:
        pnl = (exit - entry) * size - costs   # for longs
        equity += pnl
        trades.append({entry_at, exit_at, pnl, rr, ...})
```

Where `size = risk_dollars / (entry - sl)` (long) — fixed fractional risk.

### Step 5 — Compute metrics

| Metric | Formula | What it tells you |
|---|---|---|
| **Net profit** | `equity - initial_equity` | Absolute P&L. |
| **Profit factor** | `sum(winning_pnl) / abs(sum(losing_pnl))` | >1 means winners outweigh losers. >1.5 is good for SMC. |
| **Win rate** | `wins / total_trades` | Fraction of trades that hit TP first. |
| **Avg RR realized** | `mean(pnl / risk)` per trade | Compared to setup's `rr`: should match if same-bar SL-first is rare. |
| **Max drawdown** | `max(peak_equity - equity) / peak_equity` | Worst peak-to-trough loss. Critical for risk management. |
| **Sharpe ratio** | `mean(daily_returns) / std(daily_returns) * sqrt(252)` | Risk-adjusted return (annualized). |
| **Exposure** | `mean(time_in_market)` | How much of your time is actually deployed. Low exposure = opportunity cost. |
| **Trade count** | total trades | Statistical significance. <100 = don't trust the result. |

### Step 6 — Validate

- **Out-of-sample test** — train on bars `[0..T]`, test on `[T..end]`. Walk
  forward in 3-month windows.
- **Different symbols** — don't optimize on EURUSD then claim the strategy
  works on GBPJPY. Test on at least 3 unrelated pairs.
- **Different regimes** — 2020 (COVID crash), 2021 (low vol), 2022 (rising
  rates), 2023 (range). A robust strategy works in all of them.
- **Monte Carlo** — shuffle trade order and recompute metrics 1000 times.
  Check that your max DD isn't a tail event you'd hit again.

---

## Implementation plan

The repo doesn't ship a backtester yet — it's the natural next addition.
Below is a concrete plan that reuses existing code with minimal duplication.

### New module: `app/engine/backtest.py`

```python
@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_per_trade_pct: float = 1.0    # % of equity risked
    spread_bps: float = 0.5            # 0.5 bps = ~0.5 pip on EURUSD
    slippage_bps: float = 0.3
    max_concurrent: int = 1             # per symbol/tf
    order_validity_bars: int = 96       # cancel after N bars if not filled
    cooldown_bars: int = 0              # bars to wait after a close
    bars: int = 5000

def run_backtest(df, setups_by_index, cfg) -> BacktestResult:
    """Replay history bar-by-bar with realistic execution.
    setups_by_index: dict mapping formed_index -> list of setups at that bar.
    """
    ...
```

### Reuse existing code

```python
from app import smc as smc_mod
from app.engine.setup_builder import build_setups
from app.data.store import Store

df = store.load_candles("EURUSD", "H1")
n = len(df); step = 4

# 1. Walk forward, snapshot each bar, run SMC, build setups at each step.
setups_by_index = {}
for i in range(300, n - 100, step):
    sub = df.iloc[:i+1].reset_index(drop=True)
    smc = smc_mod.analyze(sub, cfg["smc"])
    setups = build_setups("EURUSD", "H1", sub, smc, cfg["smc"])
    setups_by_index[i] = setups

# 2. Feed setups + df + cfg into the backtester.
result = run_backtest(df, setups_by_index, cfg)
print(result.summary())
```

The walk-forward pass is **identical to the ML trainer** — that's by
design. We can extract a helper:

```python
def walk_forward_setups(store, cfg, symbols, timeframes, step, warmup):
    yield from ...   # produces (symbol, tf, df, formed_index, setup)
```

Then both `pipeline.train` and `backtest.run_backtest` consume it.

### Realistic costs

`labeler.label_setup` uses raw highs/lows with no costs. The backtest
should model:

```python
# Long entry: fill at the worse of limit price and bar low (depending on assumption)
fill_price = max(entry, df.low.iloc[k])    # conservative
fill_price -= spread_bps * 0.0001 * fill_price

# SL: assume the gap, fill slightly worse
sl_fill = sl - slippage_bps * 0.0001 * sl
tp_fill = tp - slippage_bps * 0.0001 * tp   # longs get less on TP
```

Realistic backtest of a retail FX strategy adds ~1 pip of cost per
round-trip. A strategy with 20% win rate at RR 2 needs more than 25% win
rate net of costs to break even — verify this.

### Output: a results dataclass

```python
@dataclass
class BacktestResult:
    trades: list[dict]
    equity_curve: list[float]
    timestamps: list[int]
    metrics: dict

    def summary(self) -> str:
        m = self.metrics
        return (
            f"trades: {m['trade_count']:4d}  "
            f"win_rate: {m['win_rate']:.1%}  "
            f"profit_factor: {m['profit_factor']:.2f}  "
            f"max_dd: {m['max_drawdown']:.1%}  "
            f"sharpe: {m['sharpe']:.2f}\n"
            f"net: {m['net_profit']:+.2f}  "
            f"avg_rr_realized: {m['avg_rr']:.2f}  "
            f"exposure: {m['exposure']:.1%}"
        )
```

### CLI: `python -m app.main backtest`

```python
# in app/main.py
def cmd_backtest(cfg, args):
    from app.engine.backtest import run_backtest, BacktestConfig
    bt_cfg = BacktestConfig(initial_equity=10_000, risk_per_trade_pct=1.0,
                            spread_bps=0.5, slippage_bps=0.3)
    store = Store(cfg["storage"]["path"])
    # ... walk forward, run, print ...
```

---

## Common pitfalls

### 1. Survivorship bias in symbol selection

Don't backtest only on EURUSD and claim the strategy works. Test on:

- Majors: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD, USDCHF
- Crosses: GBPJPY, EURJPY, AUDNZD
- Metals: XAUUSD
- Indices: US30, NAS100

A strategy that only works on one symbol is curve-fitted to that symbol's
personality.

### 2. Look-ahead bias in setup parameters

Any time you "tune" `min_rr` or `sl_buffer_atr` based on backtest results,
you're fitting to the past. **Split data first**: tune on `train_period`,
validate on `validation_period`, report on `test_period`.

### 3. Position sizing masking the truth

A 0.1% risk-per-trade strategy can hide a -50% win-rate disaster. Always
report metrics at a realistic risk level (1–2% per trade).

### 4. Counting wins that never happened

The forward simulator's same-bar SL-first rule is intentionally
conservative. If your backtester uses "if TP was hit on bar k, count as win
regardless of SL" you'll overestimate win rate.

### 5. Ignoring the spread

EURUSD spread is 0.1–1.0 pip depending on broker/time. On a 5-pip target,
that's 10–20% of your reward. Strategies that don't account for it look
great in tests and bleed in production.

---

## What to report (and what to ignore)

### Always report

- Total trade count (need ≥100 for any confidence)
- Win rate, profit factor, max drawdown, Sharpe
- Per-symbol breakdown (some symbols will be net losers)
- Equity curve (visual inspection beats any single number)
- Worst 10 trades (understand your tail)

### Ignore

- **Single backtest net profit** — meaningless without context (DD, time,
  trade count).
- **Optimized parameter sets** — the more you tune, the more you're fitting
  to noise.
- **Backtests < 1 year** — too few trades and one regime.

---

## Validation checklist

Before believing any backtest result:

- [ ] ≥ 100 trades in the result
- [ ] Multiple symbols tested
- [ ] Out-of-sample period (not used for tuning)
- [ ] Costs modeled (spread + slippage)
- [ ] Position sizing fixed (not "what would have made the most money")
- [ ] Equity curve looks plausible (no flat-then-vertical spikes)
- [ ] Worst 10 trades are believable (you can rationalize why each one
      would have lost)
- [ ] Re-run on synthetic data and confirm the result is *not* suspiciously
      good (sanity check)

---

## Possible improvements

- **Partial take-profits** — close 50% at TP1, trail stop to breakeven,
  let the rest run to TP2.
- **Walk-forward optimization** — re-tune parameters every quarter on the
  most recent N months, then test on the next month.
- **Regime tagging** — annotate each trade with the market regime
  (trending/ranging/volatile) and report per-regime metrics.
- **Monte Carlo** — shuffle the trade sequence 1000 times to estimate the
  probability distribution of max drawdown.
- **Live paper trading** — connect the backtester to a paper-trading broker
  API for forward validation on unseen data.

---

## TL;DR

The repo's `labeler` gives you per-setup win/loss labels for ML training.
A full backtester is **the same forward simulator, run continuously across
the whole history**, with position tracking, costs, and equity accounting.
Reuse the walk-forward setup generator; add position/exit logic; report
profit factor, max DD, and Sharpe; validate on out-of-sample data.
