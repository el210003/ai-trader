# MTF Confluence Engine (Proposal B — implemented)

**Date:** recorded at implementation time
**Status:** ACCEPTED (Option B)
**Author context:** user trades M15 entries only; asked whether MTF is worth it.

## Problem

The pipeline analyzed M15/H1/H4 **independently** — three parallel setup
generators with no cross-timeframe awareness. An M15 long against an H1
downtrend scored identically to one aligned with it. H1/H4 setups were cards
the user would never trade.

## Decision

**M15 is the only entry timeframe. H1/H4 become bias + context, projected
into the M15 analysis.** Option B (confluence engine) over Option A (UI
filtering only) and Option C (hard HTF gating only):

| Option | Description | Verdict |
|---|---|---|
| A | UI only: show M15 setups, hide H1/H4 | least value |
| **B** | **MTF confluence engine: project HTF trend/zones/PD/liquidity into M15 setups + ML features + optional hard filter** | **chosen** |
| C | M15 setups only created when H1 bias agrees | strictest; measure first |

## Design

```
H4  → narrative: dealing-range position, draw on liquidity
H1  → bias filter (hard filter available, off by default)
M15 → the only entry/trigger TF
CSM → currency strength confluence (already live in the strip)
```

### What gets projected into every M15 setup

1. **HTF trend** — "H1 structure aligned" / "H4 structure aligned" confluence
   lines; mean alignment across HTFs becomes a feature.
2. **HTF premium/discount** — entry in the H1/H4 discount (long) / premium
   (short) half → confluence line + feature.
3. **HTF zones** — M15 entry zone overlapping/near (≤ 0.5 ATR) a same-direction
   HTF order block or FVG → "entry at H1 order block" confluence + feature.
4. **HTF liquidity as TP** — H1/H4 buyside/sellside pools join the M15 pool
   candidates when choosing the take-profit target (structural targets instead
   of "nearest M15 pool").
5. **CSM alignment** — when the currency-strength snapshot says the pair is
   aligned (base > quote on all TFs for a long) → confluence line.

### New ML features (feature set v3)

| Feature | Meaning | Neutral when |
|---|---|---|
| `htf_trend_align` | mean(1.0 HTF trend matches direction, 0.0 opposed) | 0.5 (no HTF ctx) |
| `htf_pd_alignment` | mean(1.0 entry in correct HTF half) | 0.5 |
| `entry_in_htf_zone` | 1.0 entry zone overlaps/near same-direction HTF zone | 0.0 |
| `htf_tp_distance_atr` | entry → nearest HTF pool in profit direction (ATR, cap 20) | 20.0 |

Feature-set tracking changed from a named marker file to a **content hash of
the FEATURES list** (`data/models/.feature_set`), so any future feature
change auto-triggers retrain detection without renaming markers.

### No look-ahead (training-safe)

For a setup forming on M15 bar with open time `T`, an HTF bar is only used if
its **close time ≤ T** (`htf_open + htf_sec ≤ T`). The same slicing rule runs
in live analysis and in the training replay, so live inference matches
training exactly. HTF SMC contexts are cached per (symbol, htf, last-closed
HTF bar) during replay — consecutive M15 steps inside the same H1 bar reuse
the context.

### Config

```yaml
mtf:
  enabled: true
  require_htf_bias: false   # true = drop M15 setups whose nearest HTF (H1) trend opposes
  zone_buffer_atr: 0.5      # M15-zone proximity to an HTF zone, in entry-TF ATRs
```

`require_htf_bias` ships **off** so outcomes can be compared before enforcing;
the per-setup HTF metrics are stored either way.

## Rollout / measurement

1. Retrain after deploy (auto-retrain detects the feature-set change).
2. Compare cv_auc / feature importances (`htf_*` should rank).
3. After enough journal history: filter outcomes by `htf_trend_align` and
   decide whether to flip `require_htf_bias: true`.

## Non-goals / follow-ups

- H1/H4 setups still appear in the UI (they are context; can be hidden later
  via an entry-TF filter if cluttered).
- CSM strength as its own ML feature (currently only a confluence line).
- Live outcome tracking in the journal (separate known gap).
