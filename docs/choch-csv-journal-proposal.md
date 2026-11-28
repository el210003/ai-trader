# Proposal: CHoCH Alert CSV Journal & 4-Variant Edge Simulator

## 1. Overview & Motivation

When a **Change of Character (`CHoCH`)** fires on a confirmed candle close, `app/notifications/telegram.py` dispatches a Telegram push alert and records deduplication state in SQLite (`telegram_alerts`). However, no structured market snapshot or forward trade outcome is saved for statistical review.

To answer the core research questions:
1. **"If I open a trade the moment a CHoCH alert arrives, is there a positive expectancy edge?"**
2. **"Which SL/TP & entry geometry (`Baseline ATR`, `Structural Market`, `OB Pullback Limit`, or `Structure Trail`) performs best on the exact same CHoCH signals?"**
3. **"Which confluence filters (liquidity sweep, HTF trend alignment, session, break displacement) separate winners from losers?"**

This proposal adds an append-only **CHoCH CSV Journal** (`data/choch_alerts.csv`) paired with an automatic **4-variant forward-outcome simulator** and a CLI comparison report (`python -m app.main choch-stats`).

---

## 2. The 4 Simulated Trade Variants

Every logged `CHoCH` evaluates all **4 trade geometries side-by-side** against subsequent closed candles (using the conservative **SL-wins-same-bar-ties** rule from `app/engine/outcomes.py` and enforcing `risk >= smc.min_risk_atr * ATR`):

| Variant | Prefix | Entry | Stop-Loss (SL) | Take-Profit / Exit | What It Tests |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Baseline Signal** | `v1_*` | Market at CHoCH Close | `entry ∓ 1.0 × ATR` | `entry ± 2.0 × ATR` (`2.0R`) | Pure directional drift after CHoCH |
| **2. Structural Market** | `v2_*` | Market at CHoCH Close | Impulse origin swing (or sweep wick) `∓ 0.40 × ATR` | Nearest opposing liquidity pool / swing (`1.5R–3.2R`, fallback `2.0R`) | Immediate market entry with true SMC invalidation & liquidity targets |
| **3. OB Pullback Limit** | `v3_*` | Limit at fresh CHoCH OB edge (`ob.top` / `ob.bottom`), valid `24` bars | `ob.bottom` / `ob.top` `∓ 0.40 × ATR` | Nearest opposing liquidity pool / swing (`1.5R–3.2R`, fallback `2.0R`) | Whether waiting for retrace improves R:R enough to offset missed breakouts |
| **4. Structure Trail** | `v4_*` | Market at CHoCH Close | Impulse origin swing (or sweep wick) `∓ 0.40 × ATR` | No fixed TP — exits at candle close of first opposing `CHoCH` (or `horizon_bars` time exit) | Captures multi-leg macro reversals (`4R+` tails) |

---

## 3. Proposed CSV Data Format (`data/choch_alerts.csv` — 85 Columns)

### 3.1 Identity & Timing (8 columns)

| Column | Type | Example | Purpose |
|---|---|---|---|
| `event_key` | `str` | `EURUSD\|M15\|choch\|bullish\|1732701600\|1732697100` | Primary key for deduplication across restarts and multiple Telegram bots |
| `logged_at_utc` | `str` | `2026-11-28T10:15:03Z` | Wall-clock UTC timestamp when the alert fired |
| `alert_delay_sec` | `int` | `3` | Seconds elapsed between `bar_close_ts` and alert dispatch (latency check) |
| `symbol` | `str` | `EURUSD` | Instrument symbol |
| `tf` | `str` | `M15` | Timeframe (`M15`, `H1`, `H4`) |
| `event_type` | `str` | `choch` | Structure event type (`choch`, extensible to `bos`) |
| `direction` | `str` | `bullish` | Structural break direction (`bullish` or `bearish`) |
| `side` | `str` | `BUY` | Implied order direction (`BUY` for bullish, `SELL` for bearish) |

### 3.2 Bar Timestamps & Session Context (5 columns)

| Column | Type | Example | Purpose |
|---|---|---|---|
| `event_bar_ts` | `int` | `1732700700` | Unix epoch (seconds) of the CHoCH candle's **open** (`ev["time"]`) |
| `bar_close_ts` | `int` | `1732701600` | Unix epoch of the CHoCH candle's **close** (`event_bar_ts + tf_seconds`) |
| `bar_close_utc` | `str` | `2026-11-28T10:00:00Z` | Human-readable UTC close timestamp |
| `utc_hour` | `int` | `10` | Hour of day (`0`–`23` UTC) |
| `session` | `str` | `London` | Trading session (`Asia`, `London`, `LDN_NY_overlap`, `NY`, `Late_NY`) |

### 3.3 Market & Confluence Snapshot at Alert Time (22 columns)

| Column | Type | Example | Purpose |
|---|---|---|---|
| `entry_price` | `float` | `1.08620` | Confirmed CHoCH candle `close` |
| `broken_level` | `float` | `1.08550` | Swing high/low broken to trigger CHoCH (`ev["level"]`) |
| `swing_age_bars` | `int` | `14` | Bars between the broken swing pivot and CHoCH close (`index - level_index`) |
| `atr` | `float` | `0.00110` | 14-period Wilder ATR at the CHoCH close |
| `break_dist_atr` | `float` | `0.636` | `abs(entry_price - broken_level) / atr` — breakout displacement |
| `invalidation_price` | `float` | `1.08340` | Dealing-range origin swing (`bottom` for bullish, `top` for bearish) |
| `struct_sl_dist_atr` | `float` | `2.545` | `abs(entry_price - v2_sl) / atr` — structural stop distance |
| `nearest_pool_price` | `float` | `1.08890` | Nearest opposing liquidity target (`v2_tp`) |
| `nearest_pool_dist_atr` | `float` | `2.455` | Distance in ATR to nearest opposing liquidity target |
| `range_position` | `float` | `0.782` | Close position in dealing range (`0.0` = low, `1.0` = high) |
| `range_zone` | `str` | `premium` | `discount` (`< 0.5`), `premium` (`> 0.5`), or `equilibrium` |
| `has_recent_sweep` | `int` | `1` | `1` if a confirming liquidity sweep occurred within `sweep_lookback_bars`, else `0` |
| `last_sweep_kind` | `str` | `equal_lows` | Kind of most recent confirming sweep (`swing_low`, `equal_lows`, `prev_day_low`, etc.) |
| `last_sweep_bars_ago` | `int` | `3` | Bars between that sweep and the CHoCH candle (`""` if none) |
| `fresh_ob_count` | `int` | `1` | Active unmitigated Order Blocks aligned with the CHoCH direction |
| `fresh_fvg_count` | `int` | `2` | Active unfilled Fair Value Gaps aligned with the CHoCH direction |
| `htf_h1_trend` | `str` | `bullish` | H1 trend at `bar_close_ts` (`""` if `tf >= H1` or unavailable) |
| `htf_h4_trend` | `str` | `bullish` | H4 trend at `bar_close_ts` |
| `htf_trend_aligned` | `str` | `aligned` | `aligned`, `opposed`, `mixed`, or `none` |
| `csm_status` | `str` | `aligned` | Currency Strength Meter alignment (`aligned`, `opposed`, `neutral`) |
| `setup_verdict` | `str` | `BUY` | Best setup verdict produced on the same bar (`BUY`/`SELL`/`WAIT`/`AVOID`/`NONE`) |
| `setup_ml_prob` | `float` | `0.584` | ML win probability of that setup (`""` if no setup qualified) |

### 3.4 Multi-Horizon Forward Returns & Excursions (11 columns)

| Column | Type | Example | Purpose |
|---|---|---|---|
| `outcome_status` | `str` | `RESOLVED` | `PENDING` (still collecting future bars), `RESOLVED` (`horizon_bars` observed), or `VOID` |
| `bars_observed` | `int` | `96` | Closed future bars evaluated after `event_bar_ts` |
| `ret_1b_atr` | `float` | `+0.310` | Signed return after **1 bar** in ATR: `dir_sign * (close[+1] - entry_price) / atr` |
| `ret_3b_atr` | `float` | `-0.180` | Signed return after **3 bars** in ATR |
| `ret_6b_atr` | `float` | `+0.740` | Signed return after **6 bars** in ATR |
| `ret_12b_atr` | `float` | `+1.420` | Signed return after **12 bars** in ATR |
| `ret_24b_atr` | `float` | `+1.890` | Signed return after **24 bars** in ATR |
| `mfe_atr` | `float` | `+2.350` | Maximum Favorable Excursion in ATR over `horizon_bars` |
| `mae_atr` | `float` | `+0.620` | Maximum Adverse Excursion in ATR over `horizon_bars` (`>= 0`) |
| `bars_to_mfe` | `int` | `19` | Bar offset (`1..H`) when peak favorable excursion occurred |
| `bars_to_mae` | `int` | `2` | Bar offset (`1..H`) when worst drawdown occurred |

### 3.5 Variant End-Result Columns (37 columns)

- **Variant 1 — Baseline Signal (9 cols)**:
  `v1_entry`, `v1_sl`, `v1_tp`, `v1_rr`, `v1_result` (`PENDING`/`WIN`/`LOSS`/`EXPIRED_OPEN`/`VOID`), `v1_r`, `v1_bars`, `v1_mfe_r`, `v1_mae_r`
- **Variant 2 — Structural Market (9 cols)**:
  `v2_entry`, `v2_sl`, `v2_tp`, `v2_rr`, `v2_result` (`PENDING`/`WIN`/`LOSS`/`EXPIRED_OPEN`/`VOID`), `v2_r`, `v2_bars`, `v2_mfe_r`, `v2_mae_r`
- **Variant 3 — OB Pullback Limit (12 cols)**:
  `v3_has_ob` (`1`/`0`), `v3_entry`, `v3_sl`, `v3_tp`, `v3_rr`, `v3_filled` (`1`/`0`), `v3_fill_bars`, `v3_result` (`PENDING`/`WIN`/`LOSS`/`EXPIRED_UNFILLED`/`EXPIRED_OPEN`/`NO_OB`/`VOID`), `v3_r`, `v3_bars`, `v3_mfe_r`, `v3_mae_r`
- **Variant 4 — Structure Trail (9 cols)**:
  `v4_entry`, `v4_sl`, `v4_exit_price`, `v4_exit_reason` (`PENDING`/`SL`/`OPPOSING_CHOCH`/`TIME_EXIT`/`VOID`), `v4_result` (`PENDING`/`WIN`/`LOSS`/`BREAKEVEN`/`VOID`), `v4_r`, `v4_bars`, `v4_mfe_r`, `v4_mae_r`

### 3.6 Manual Review (2 columns)

| Column | Type | Example | Purpose |
|---|---|---|---|
| `user_taken` | `str` | `1` | Blank on creation; user-editable (`1`/`0`) to flag alerts you actually traded |
| `user_notes` | `str` | `Clean London sweep` | Blank on creation; preserved verbatim across outcome updates |

---

## 4. Configuration (`config.yaml` & `app/config.py`)

```yaml
telegram:
  enabled: true
  # ...
  csv_journal:
    enabled: true                  # log confirmed CHoCH alerts to CSV for edge analysis
    path: data/choch_alerts.csv    # output CSV path (relative to project root)
    events: [choch]                # event types to journal: [choch] or [choch, bos]
    horizon_bars: 96               # future closed bars to simulate outcomes (96 M15 bars = 24h)
    sl_atr: 1.0                    # Variant 1 fixed ATR stop distance
    tp_atr: 2.0                    # Variant 1 fixed ATR target distance
    ob_valid_bars: 24              # Variant 3 OB pullback limit order validity window
```

---

## 5. CLI End-Result Summary (`python -m app.main choch-stats`)

Running `python -m app.main choch-stats [--symbol EURUSD] [--tf M15]`:
1. Resolves any pending rows in `data/choch_alerts.csv` against the latest SQLite candles.
2. Prints a side-by-side comparison table of **all 4 variants** showing:
   - Signals, Filled count & Fill Rate
   - Wins / Losses / Open / Unfilled
   - Win Rate (`W / (W + L)`)
   - Expectancy per filled trade (`Avg R`) and per signal
   - Total Net R & Profit Factor
   - Average MFE (R), MAE (R), and Bars to Outcome
3. Prints filter breakdowns (`direction`, `has_recent_sweep`, `htf_trend_aligned`, `session`) so you can immediately see where the edge concentrates.
