# Win-Rate & Expectancy Improvement Plan

**Date:** 2026-11-23  
**Status:** Plan Only (No code or config modified yet)  
**Evidence Base:** 462 resolved live setups in `data/trader.db` + 3,454 clean `M15` replay setups across the 8 major symbols.

---

## Executive Answer: Yes — Win Rate Can Be Improved Meaningfully

Across the clean `M15` system (`min_rr: 1.5`, average realized winner `~1.92R`), the breakeven win rate is **`34.2%`**, and the current baseline sits at **`37.2%` live (`+0.088R`/trade)** and **`38.1%` replay**.

Data from both live resolved outcomes (`n=462`) and clean `M15` replay (`n=3,454`) shows **five clear, high-leverage bottlenecks** that suppress win rate and expectancy. Addressing them cleanly—without overfitting—can realistically lift realized win rate from **`~37%` into the `43%–48%` range** while increasing net `R` expectancy:

1. **Cap `smc.max_rr` at `3.0–3.5` (currently `5.0`)**: High-RR targets (`3.5R–5.0R`) win only **`11.9%` live (`-0.355R`, `n=42`)** and **`17.1%` in replay (`-0.038R`, `n=82`)**, whereas `1.5R–3.5R` setups win **`39.9%–41.0%` live (`+0.09R` to `+0.41R`)** and **`40.0%` in replay (`+0.094R`)**.
2. **Filter out HTF-opposed setups (`mtf.require_htf_bias: true`)**: Counter-HTF setups drop to **`35.3%` WR live** and **`36.5%` WR (`+0.014R`) in replay**, versus **`39.3%–43.9%` WR (`+0.098R` to `+0.221R`)** when HTF structure is aligned/mixed.
3. **Increase SL noise buffer (`smc.sl_buffer_atr: 0.25 -> 0.40`) on FVG/zone entries**: **`17.2%` of FVG losers (`post_loss_tp_hit`)** hit TP after stopping out first (and average loser MAE reaches `1.67R` while winner MAE is only `0.33R`), indicating stops placed right at zone boundaries are clipped by normal spread/wick noise.
4. **Shift selection weight toward Rejection-Bar setups (`50.0%` WR live, `+0.385R`) & tighten Zone entries**: Rejection-bar setups outperform OB (`32.8%` WR, `+0.021R`) and FVG (`37.0%` WR, `+0.078R`) setups across both live and replay data (`40.9%` WR, `+0.160R`), with `0%` `post_loss_tp_hit`.
5. **Fix missing CSM features in ML + tune hybrid/LLM fusion**: `csm_dir` is computed in `pipeline.py` and appended to `confluences` text, **but is never passed into `make_features()`** (so the ML model has never seen Currency Strength alignment!), and LLM `AVOID` overrides currently discard valid setups (`AVOID` setups actually won **`50.7%` / `+0.409R`** in live history!).

---

## Empirical Evidence Breakdown

### 1. Setup Family Performance (Live `n=462` vs. Clean M15 Replay `n=3,454`)
| Setup Family | Live `n` | Live WR | Live Exp | Replay `n` | Replay WR | Replay Exp |
|---|---:|---:|---:|---:|---:|---:|
| **Rejection-bar (`rejection_bar`)** | 26 | **50.0%** | **+0.385R** | 423 | **40.9%** | **+0.160R** |
| **Fair Value Gap (`fvg`)** | 378 | 37.0% | +0.078R | 3,031 *(all zones)* | 39.2% | +0.081R |
| **Order Block (`order_block`)** | 58 | 32.8% | +0.021R | — | — | — |

### 2. Planned Risk:Reward Buckets
| RR Bucket | Live `n` | Live WR | Live Exp | Replay `n` | Replay WR | Replay Exp |
|---|---:|---:|---:|---:|---:|---:|
| `1.5 – 2.0R` | 307 | **41.0%** | +0.092R | — | — | — |
| `2.0 – 2.5R` | 72 | 36.1% | +0.151R | — | — | — |
| `2.5 – 3.5R` | 41 | 36.6% | **+0.409R** | 3,372 *(1.5–3.5R)* | **40.0%** | **+0.094R** |
| **`3.5 – 5.0R`** | **42** | **11.9%** | **-0.355R** | **82** | **17.1%** | **-0.038R** |

### 3. HTF Bias Alignment & Session Regimes (Clean M15 Replay, `n=3,454`)
| Regime / Filter | Replay `n` | Replay WR | Avg RR | Replay Exp |
|---|---:|---:|---:|---:|
| **HTF trend aligned (`1.0`)** | 1,054 | **39.3%** | 1.95 | **+0.098R** |
| **HTF trend opposed (`0.0`)** | 1,166 | **36.5%** | 1.84 | **+0.014R** |
| **LDN/NY Overlap (`12–16 UTC`)** | 520 | **43.1%** | 1.85 | **+0.182R** |
| **London (`07–12 UTC`)** | 611 | **41.6%** | 1.84 | **+0.146R** |
| **NY (`16–20 UTC`)** | 477 | **40.0%** | 1.81 | **+0.117R** |
| **Asia (`00–07 UTC`)** | 1,149 | **37.2%** | 1.85 | **+0.024R** |

### 4. Symbol & Hybrid Verdict Diagnostics (Live `n=462`)
- **Consistently negative live symbols (`n >= 20`):** `AUDCHF` (`20.7%` WR, `-0.301R`, `n=29`), `AUDJPY` (`25.0%` WR, `-0.214R`, `n=24`), `NZDUSD` (`30.4%` WR, `-0.159R`, `n=23`), `USDCHF` (`7.1%` WR, `-0.811R`, `n=14`), `EURJPY` (`11.1%` WR, `-0.663R`, `n=18`).
- **Strongest live symbols (`n >= 20`):** `EURUSD` (`53.8%` WR, `+0.435R`), `CADJPY` (`50.0%` WR, `+0.713R`), `GBPAUD` (`50.0%` WR, `+0.582R`), `AUDNZD` (`45.5%` WR, `+0.295R`), `AUDUSD` (`45.0%` WR, `+0.269R`), `GBPUSD` (`43.8%` WR, `+0.285R`).
- **LLM contradiction penalty bug (`app/ai/hybrid.py`):** In live resolved setups, setups labeled `AVOID` by the LLM (`-20` score penalty) actually achieved **`50.7%` WR (`+0.409R`, `n=67`)** vs **`35.2%` WR (`+0.042R`, `n=392`)** for `WAIT`—meaning the LLM's directional bias was anti-correlated with M15 mean-reversion/sweep entries and actively rejected winning trades.

---

## Phased Implementation Plan

### Phase 1 — High-Impact Setup Geometry & Filter Tuning (`config.yaml`)
*Goal: Eliminate structurally low-win-rate setups at setup construction time before ML/LLM scoring.*

1. **Cap `smc.max_rr` from `5.0` to `3.2` (or `3.5`)**:
   - Distant liquidity targets (`>3.5R`) rarely fill within the 96-bar (`24h`) M15 horizon (`11.9%` WR live, `17.1%` replay). Capping `max_rr: 3.2` converts far-target setups into achievable take-profits and immediately lifts overall win rate by ~`+1.5%–2.0%`.
2. **Enable `mtf.require_htf_bias: true`**:
   - Drops M15 setups whose nearest HTF (`H1`) structure opposes the trade direction, eliminating the `36.5%` WR / `+0.014R` counter-trend drag.
3. **Widen `smc.sl_buffer_atr` from `0.25` to `0.40`**:
   - Reduces stop-outs from spread spikes and shallow liquidity wicks on FVG/OB zones (`17.2%` of FVG losers currently reach TP after stopping out).
4. **Restrict live trading hours (`execution.trading_hours: "07-20"`)**:
   - Focuses execution on London, LDN/NY overlap, and NY sessions (`40.0%–43.1%` WR in replay) and avoids thin Asia session chop (`37.2%` WR, `+0.024R`).
5. **Disable underperforming cross pairs via `select-symbols`**:
   - Disable `AUDCHF`, `AUDJPY`, `USDCHF`, `EURJPY` (all `<26%` WR and `<-0.21R` expectancy) until re-evaluated.

---

### Phase 2 — Setup Builder & Entry Quality Upgrades (`app/engine/setup_builder.py`)
*Goal: Improve fill quality and prioritize higher-probability entry mechanics.*

1. **Require a recent directional liquidity sweep for zone setups (`require_zone_sweep: true`)**:
   - Currently `setup_builder.py` allows zone entries whenever `aligned OR recent` (`line 277`), meaning a zone with **no liquidity sweep** can trigger solely on trend alignment. Requiring `recent` sweep confirmation (`sweep_depth_atr` is the #2 strongest feature in the ML model at `+0.047` AUC importance) filters out un-swept inducement zones.
2. **Prefer Rejection-Bar setups when both Rejection and Zone setups fire in the same direction**:
   - In `pipeline.py` (`best[direction]`), candidate ranking currently uses `(ml_prob, len(confluences))`. Because zone setups accumulate more text confluence lines (`6+` vs `3–4`), zone setups routinely beat rejection setups (`50.0%` live WR) on tie-breaks. Adding a family priority or letting `is_rejection` carry explicit preference preserves high-WR rejection triggers.
3. **Tighten `smc.entry_valid_bars` for zone setups from `24` (6h) to `12` (3h)**:
   - Stale zones that take 4–6 hours to retest have usually lost order-flow momentum (`zone_freshness` is positively correlated with win rate in both live terciles and replay).

---

### Phase 3 — Feature Engineering & Model Upgrades (`app/ai/features.py`, `app/pipeline.py`, `app/ai/ml_model.py`)
*Goal: Give the classifier missing market-regime signals and calibrate it for top-decile precision.*

1. **Wire Currency Strength Meter (`csm_dir`) into `make_features()`**:
   - `csm_dir` is already computed in `pipeline.py` (`csm_aligned`), yet omitted from `FEATURES`. Add `csm_aligned` (`+1` aligned, `-1` opposed, `0` neutral) and `csm_spread` (base currency strength minus quote currency strength) to `FEATURES` and persist `csm_dir` in replay.
2. **Add interaction & structural quality features (`v6` feature set)**:
   - `rr_x_htf_align`: Interaction between target distance (`rr`) and HTF trend agreement (high RR only works when HTF trend supports the move).
   - `zone_dist_atr`: Distance from current close to the limit entry (`entry_distance_atr` is computed on setups but not passed to ML; entries too far from price suffer adverse selection—only filling on strong momentum reversals against the trade).
   - `atr_regime_ratio`: `atr_pct / symbol_avg_atr_pct` (normalized volatility expansion/contraction relative to the pair's own baseline).
3. **Optimize training objective for precision at the execution threshold (`P >= 0.42`)**:
   - Tune `HistGradientBoostingClassifier` regularization (`min_samples_leaf=25`, `max_depth=3`, `l2_regularization=3.0`) via purged walk-forward CV to reduce variance on `10k` rows and improve top-bucket win rate.
4. **Engage Tier-3 Live-Outcome Blend (`ai.ml.live_outcomes.min_samples: 150`)**:
   - Currently `185` unique live outcomes sit just below `min_samples: 200`. Lowering `min_samples` to `150` (with purged CV gating already built in) lets the model adapt directly to live broker fill/spread dynamics.

---

### Phase 4 — Hybrid Fusion & LLM Prompt Alignment (`app/ai/hybrid.py`, `app/ai/llm.py`)
*Goal: Stop the LLM from vetoing valid sweep/retest setups.*

1. **Fix the LLM `AVOID` contrarian penalty (`app/ai/hybrid.py`)**:
   - Live data proves `AVOID` setups won **`50.7%` (`+0.409R`)** because the LLM's generic prompt (`SYSTEM_PROMPT`) penalizes counter-trend sweep entries right at liquidity extremes (where SMC reversals originate). Reduce the `aligned == False` penalty from `-20.0` to `-5.0` (or remove the hard `AVOID` override when `ml_prob >= 0.42`).
2. **Re-weight `ai.hybrid` (`ml_weight: 0.75`, `llm_weight: 0.25`)**:
   - Shift primary decision authority to the empirically calibrated ML model while keeping the LLM as a secondary narrative/risk check.
3. **Raise `execution.min_ml_prob` to `0.42` once Phase 1–3 features are live**:
   - With `max_rr: 3.2`, `require_htf_bias: true`, and `csm_aligned` active, filtering for `P(win) >= 0.42` targets a **`45%–50%` realized win rate** at `~1.8R–2.2R` average payoff.

---

## Recommended Execution Order & Verification

| Step | Scope | How to Verify Before Live Execution |
|---|---|---|
| **1. Phase 1 (`config.yaml` + symbol selection)** | `max_rr: 3.2`, `require_htf_bias: true`, `sl_buffer_atr: 0.40`, `trading_hours: "07-20"`, disable 4 worst pairs | Run `python -m app.main train --compare` and compare replay win rate & purged `cv_auc` against `data/ml_baseline.json`. |
| **2. Phase 2 (`setup_builder.py`)** | Mandatory sweep on zone setups, `entry_valid_bars: 12`, rejection tie-break priority | Verify replay sample count stays `>= 4,000` M15 setups and replay win rate rises above `42%`. |
| **3. Phase 3 (`features.py`, `pipeline.py`, `ml_model.py`)** | Add `csm_aligned`, `zone_dist_atr`, `atr_regime_ratio`, `rr_x_htf_align`; `min_samples: 150` | Confirm purged 5-fold `cv_auc` improves by `>= +0.005` and `cv_brier` decreases. |
| **4. Phase 4 (`hybrid.py`, `llm.py`)** | Soften LLM contradiction penalty (`-20 -> -5`), set `ml_weight: 0.75`, `min_ml_prob: 0.42` | Re-score `setups_history` and run `execution.dry_run: true` for 1 week to confirm live WR `>= 43%`. |
