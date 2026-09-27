# RejectionProto research log — engulfing + level-sweep rejection detector

Companion prototype to KissEA (`RejectionProto.mq5`, v1.01→v1.13): a **detection-only**
M15 EA that journals and chart-marks rejection bars (engulfing + level sweep), swings,
breaks, and their outcomes — plus a tick-exact virtual-trade simulator (`TRD`, v1.12);
no real trading. Built to answer one owner question with data:

> *Is an engulfing bar with momentum a good entry point — and if I only enter on the
> rejection bars (engulf + level sweep) with momentum, what do I gain or lose?*

This document records the method and the corrected conclusion. See `decision-log.md`
(DEC-033, superseded by DEC-034) for the disposition.

---

> ## ⚠ CORRECTION (2026-09-26) — all pre-audit positive results void; Sections 3–7 rewritten with corrected figures
>
> Every **positive P&L number** this document originally reported (momentum +0.85/+0.78/+0.74/+0.68R,
> pooled +0.76R, "all 16 year-quarter cells positive", "symbols-positive 71/72") was an
> artifact of **two Python analysis bugs**, not a property of the strategy:
>
> 1. **Bear-side TP placed on the wrong side.** The offline trade sim computed
>    `tp = entry + 2·risk` for *both* directions. For bear entries the target landed
>    *above* the entry (the loss direction), so `low ≤ tp` was true on nearly every bar
>    and virtually every non-stopped bear trade "hit TP" instantly at +2R. Half the
>    trade population (bears) was scored with fabricated wins.
> 2. **TRD aggregation read the wrong field.** The first tick-exact aggregation parsed
>    the exit *price* (`xp=`) as if it were the R-multiple (`r=`), producing nonsense
>    averages that mixed price scales across symbols (the absurd "TP slippage −199R"
>    diagnostic was the tell).
>
> **How it was found:** the owner requested Model=4 (every-tick-from-real-ticks)
> validation. v1.12 added an in-EA tick-exact virtual-trade simulator (`TRD` journal
> lines). Tick-resolved exits agreed with the corrected offline rules on ~98.6% of
> trades — but the original offline sim disagreed *massively*, and per-trade
> reconciliation isolated every disagreement to bear entries. The direction of the
> aggregate divergence (tick-exact TP-rate *below* the pessimistic offline TP-rate) is
> logically impossible, which forced the audit that exposed the bug.
>
> **Corrected results** (Model=4 tick-exact `TRD`, 72,326 trades, 18 pairs × 2022–2025,
> net of 1-pip round trip; corrected offline sim agrees within ~0.03R on every cell):
>
> | bucket | 2022 | 2023 | 2024 | 2025 |
> |---|---|---|---|---|
> | momentum | −0.07R | −0.08R | −0.16R | −0.11R |
> | A-grade | −0.16R | −0.16R | −0.19R | −0.21R |
> | other | −0.17R | −0.20R | −0.19R | −0.17R |
> | ALL | −0.15R | −0.18R | −0.19R | −0.16R |
>
> **Net-negative in every bucket, every year.** Gross (before the 1-pip cost) the rule
> set is ≈ breakeven (−0.02…−0.06R). Momentum exit mix: TP 16–19% / SL 47–49% /
> time-exit 32–37%; time-exit drift ≈ +0.1R — the 2R target is simply not reached often
> enough before the engulf extreme is taken out (barrier odds 17:49 at 2:1 payoff).
> Slippage is negligible (TP fills +0.01…+0.06R beyond target, SL fills −0.01…−0.03R).
> All 16 momentum year-quarter cells ≤ +0.01R.
>
> **What survives:** the detection layer and its verification (Sections 1–2, 3.3);
> the 4-bar excursion study (raw MFE/MAE geometry, no TP logic); the RET retrace-entry
> outcome data; and the v1.12 tick-exact trade simulator itself. What does **not**
> survive: the momentum-reject entry edge, the "dual geometry" narrative, and the
> proposed `RejectionTrade.mq5` as specified (close entry + engulf-extreme SL + 2R
> target). See DEC-034 for disposition.
>
> **Update (v1.13, same day):** following the corrected verdict the owner directed
> removal of plain engulfing bars (no level sweep) — the EA is now **reject-only**:
> no ENGULF journal lines, no thin arrows, no diamonds, C3/RET armed on rejects only
> (DEC-035). Archived v1.08–v1.12 journals remain the record for the removed streams.

---

## 1. What the prototype measures

**Detection stream** (unchanged since v1.07, replay-verified bit-exact):

- **ENGULF** — an M15 bar whose body engulfs the prior bar's body (direction-corrected).
- **REJECT** — an engulfing bar that also **swept** a swing level (fractal N=3 pivots,
  newest-of-equals tie-break) and closed back — the spring/bar-fakey event.
- **BROKEN** — a swing level closed through by a later body.
- **PIVOT** — newly confirmed swing high/low (3-bar confirmation lag).

**Annotation layers** (all journal-only, integer-point math — no ULP knife-edges):

- `mom:` block on every ENGULF/REJECT — `clv` (close location in range, %),
  `br` (body/range %), `exp` (body vs avg body of prior 20 bars, tenths of an x),
  `depth` on REJECT lines (deepest sweep beyond the level, % of bar range).
- `C3` — the next bar's cont/hold/rev verdict vs the engulf body (ICT C3 test)
  + MFE/MAE over 4 bars, % of the engulf range.
- `RET` (v1.09) — after every engulf close, a **virtual limit at 50% of the body**
  (the retrace entry), valid 8 bars, stop at the engulf extreme; on fill the next
  4 bars are measured **in % of risk** (R-multiples), or `nofill` if untouched.

**Chart markers** (v1.11): green/red arrows = bull/bear engulf (wide = swept);
orange diamond = momentum engulf ≥2× avg body at its retrace level (non-rejects);
**REJECT arrows carry the bucket color** — gold = A-grade (weak close clv<50 +
small bar exp<2×), magenta = momentum reject (exp≥3×), else direction color.
Inputs: `InpMomExpTenths=20`, `InpWeakClosePct=50`, `InpBigExpTenths=30`,
`InpRetraceBars=8`, `InpRetracePct=50`, `InpAvgBodyBars=20`, `InpOutcomeBars=4`.

Versions 1.08–1.11 were annotate-only: the detection stream is byte-identical
across them (local EURUSD diff = INIT line only); the verifier
(`verify_symbol.py`) replays every tag from OHLC alone.

### 1.1 Detection algorithm (detailed)

Everything below runs on **closed M15 bars only** (no intra-bar logic), all
comparisons on integer points (`MathRound(price / _Point)`) to avoid ULP
knife-edges. Code reference: `RejectionProto.mq5` — `ProcessBar()` →
`EvaluateBar()` (detection) + book maintenance.

**Input defaults:** `InpMinRangePoints=10` (noise gate), `InpSwingBars=3`
(fractal width), `InpUseSwingLevels=true`, `InpUseDayExtreme=true`,
`InpAvgBodyBars=20`, `InpMomExpTenths=20`, `InpBigExpTenths=30`,
`InpWeakClosePct=50`.

**Per-bar pipeline — strict order** (`ProcessBar`, bar `B` just closed,
previous bar `P`): pending-window bookkeeping first (`TRD` time exits, `RET`
retrace windows, `C3` outcome windows — bar `B` is a *new* bar for older
pendings), then:

```
1. EvaluateBar(B)          detection — uses level books AS OF P's CLOSE
2. UpdateDayExtremes(B)    fold B into running day H/L (AFTER evaluation,
                           so the bar that sets a day extreme cannot sweep it)
3. MarkBrokenByClose(B)    body-close through a swing level marks it BROKEN
4. IfPivotAdd(s − N)       the bar N=InpSwingBars back is confirmed/refuted
                           as a fractal pivot by B's close (N-bar lag)
```

The no-hindsight rule: the evaluated bar never sweeps or breaks with levels
that its own close creates — references are frozen at the previous close.

**Step 1a — sweep test (full specification).** A *sweep* is a failed breakout:
the bar's wick trades through a live level and the close retreats to the
original side. Which levels are live, and the exact predicate:

*Level source 1 — swing levels.* Every confirmed N=3 fractal pivot (step 4)
enters a chronological book (`swingHigh` / `swingLow`; each entry = {pivot
time, pivot price, broken flag}). Because confirmation lags the pivot bar by
N bars, a level is sweepable only from the bar after its confirming bar
onward. Books grow unboundedly but broken entries are skipped; on flat
tops/bottoms the newest touch holds the reference (tie-break in step 4).

*Level source 2 — running day extremes.* Server-day high/low, ratcheted
AFTER each bar's evaluation (step 2) and reset at server midnight. Same-day
only: a bar may sweep the day extremes only if the day stamp matches its own
(`dayStamp == DateOf(B.time)`) — and until the first bar of a new day has
closed and been folded in, there is no day-extreme reference at all. Day
extremes never "break"; they only reset.

*The predicate* — per live level `lvl`, strict comparisons on both sides:

```
swept high lvl:  B.high > lvl  AND  B.close < lvl
swept low  lvl:  B.low  < lvl  AND  B.close > lvl
day extremes:    same pattern vs the running dayHigh / dayLow
```

Three boundary consequences of the strict comparisons:

- a wick exactly *touching* `lvl` (`B.high == lvl`) does **not** sweep —
  price must trade strictly beyond the level;
- a close exactly *at* `lvl` does **not** sweep — the close must be back
  strictly inside (this is what makes a sweep a *failed* breakout);
- sweep and BROKEN are disjoint on the same bar: a sweep needs the close on
  the near side (`close < lvl` for a high), a break needs it beyond
  (`close > lvl`), so one bar can never do both to the same level — and
  `close == lvl` does neither.

*Depth and multiplicity* — every swept level is recorded (level kind, price,
pivot time; comma-joined into the journal's `swept:` list) and counted:

```
depth(high) = round(100 × (B.high − lvl) / B.range)     % of bar range
depth(low)  = round(100 × (lvl − B.low)  / B.range)
nSwept      = count of levels swept by this bar
maxDepth    = deepest single sweep (journaled as depth= on REJECT lines)
```

A single bar may sweep any number of levels — a swing high and the day high
simultaneously, or several swings at once; each counts separately and
`maxDepth` keeps the deepest.

*Ordering guarantees (no hindsight).* The evaluated bar is tested against the
books exactly as its PREVIOUS close left them: the bar that creates a new day
extreme cannot sweep it (ratchet happens after evaluation, step 2); the bar
whose close confirms a pivot cannot sweep that pivot (confirmation is step 4);
and the bar's own body-close break is applied only after its evaluation
(step 3). Every sweep is therefore knowable at the sweeping bar's close, and
the whole `swept:` stream is replayable from OHLC alone — which is what the
verifier checks.

**Step 1b — engulfing test** vs `P` (skipped if `B.range < InpMinRangePoints`
— the noise gate; sweeps are still evaluated for too-small bars):

```
pair gate   bull engulf: P closed bear  AND  B closed bull
            bear engulf: P closed bull  AND  B closed bear
            (no pair → no engulf)

D1 body engulf:     B.body > P.body  AND  B's body COVERS P's body
                    bull: B.open ≤ P.close AND B.close ≥ P.open
                    bear: B.open ≥ P.close AND B.close ≤ P.open
D2 outside bar:     B.range > P.range AND B.high ≥ P.high AND B.low ≤ P.low
D3 decisive:        D1 AND B.body ≥ 2/3 × B.range
                    integer form: bodyPts × 3 ≥ rangePts × 2  (exact at the
                    boundary — the D3 knife-edge lesson)

eng = +1 (bull) / −1 (bear) if ANY of D1/D2/D3 holds; defs = bitmask
(defs & 1 = D1, & 2 = D2, & 4 = D3, journaled as "defs=1,3" etc.)
```

**Step 1c — REJECT conjunction and instrumentation:**

```
REJECT  :=  eng ≠ 0  AND  nSwept ≥ 1        (engulfing bar + level sweep)
```

Since v1.13 this is the only journaled signal (plain engulfs with no sweep
are counted in session totals only). On every REJECT the tick-exact virtual
trade (`TRD`, v1.12) opens: entry at `B.close`, SL = engulf extreme (bar low
bull / bar high bear), TP = `InpTradeRRTenths/10` R (2.0R), risk < 30 points
(3 pips) skipped; resolved from actual tester ticks (pessimistic SL-first
intrabar), exits tagged SL/TP/TIME at 16 bars.

**Step 1d — bucket classification** on every REJECT — the momentum / A-grade /
other decision is defined in detail in §1.2 below (expansion vs 20-bar average
body, direction-corrected close location value, strict if/else-if evaluated
momentum-first).

**Step 3 — BROKEN:** a swing level stops being a sweep reference when a later
bar's **body** closes through it (wick-through does not break; that is a
sweep). Day extremes are never "broken" — they reset at server midnight.

**Step 4 — pivot confirmation (N=3 fractal, 7-bar window):** candidate bar
`p` (three bars back) becomes a swing high iff no NEWER flank bar has
`high ≥ h` (strict) and no OLDER flank bar has `high > h` (equality allowed)
— mirrored for lows. The asymmetric tie-break (MQL5 CHoCH convention) means
on a flat top / bottom (EQH/EQL) the **newest** touch keeps the swing
reference instead of the pair cancelling out. Confirmed pivots join the book
as live sweep references (with a 3-bar confirmation lag — the level exists
for sweeping only from the bar that confirms it).

### 1.2 Reject-bucket classification (detailed definition)

Since v1.13 every journaled signal bar is a **REJECT** (engulfing bar that swept
a level); plain engulfings are gone. Each REJECT is classified into **exactly one
of three buckets** — mutually exclusive and exhaustive. The classification lives
in `RejectionProto.mq5` (`bkt` field: 0/1/2) and rides the REJECT journal line
tag and the v1.12 `TRD` virtual-trade lines (`bkt=`).

**Inputs (four of the seven feed the classifier):**

| input | default | role |
|---|---|---|
| `InpAvgBodyBars` | 20 | lookback for the average-body baseline |
| `InpMomExpTenths` | 20 | A-grade upper bound: exp < N/10 × avg body |
| `InpBigExpTenths` | 30 | momentum lower bound: exp ≥ N/10 × avg body |
| `InpWeakClosePct` | 50 | A-grade close-location bound |

**The two measurements (all integer-point math, no floats):**

- **exp** (expansion) — signal-bar body vs the mean body of the prior
  `InpAvgBodyBars` completed bars (the signal bar itself is excluded).
  Computed as `exp10 = round(10 × bodyPts × cnt / sumBody)` in tenths, so
  `exp=3.2x` means the bar's body is 3.2× the 20-bar average. Every x.5 edge is
  settled by half-up rounding on integers (the D3 knife-edge lesson).
- **clv** (close location value) — how strongly the bar closed **in its own
  direction**, % of range: bull bar `100×(close−low)/range`, bear bar
  `100×(high−close)/range`. High clv = close near the direction-favorable
  extreme (strong); low clv = the bar gave back more than half its move
  (weak close). A-grade's "weak close" means `clv < InpWeakClosePct`.

**The three buckets:**

| bkt | name | chart color | condition | intent |
|---|---|---|---|---|
| 1 | **momentum reject** | magenta | `exp ≥ 3.0×` | big-displacement bar — the ICT-displacement / Bulkowski-expansion family |
| 0 | **A-grade reject** | gold | `exp < 2.0×` **and** `clv < 50%` | small, soft-closing bar — the slow-grind reversal family that scored best on the 4-bar excursion study |
| 2 | **other** | direction color (green/red) | everything else | catch-all |

Classification details that matter when reading results:

- **The 2×–3× gap is "other"**: a bar with `exp` between 2.0× and 3.0× — even
  with a weak close — is neither A-grade (requires exp < 2×) nor momentum
  (requires exp ≥ 3×). The two named buckets cannot overlap; bucket assignment
  is a strict if/else-if chain evaluated momentum-first.
- **Fallback to "other"**: when no average body is computable (`InpAvgBodyBars=0`,
  or the lookback has zero total body — dead/flat market), `haveExp` is false and
  the bar lands in bucket 2 regardless of clv.
- **Journal form**: the REJECT line appends ` + momentum reject` or
  ` + A-grade (weak close + small bar)`; untagged REJECTs are bucket 2. The TRD
  line carries the numeric `bkt=0|1|2`, which is how every per-bucket result in
  this document was aggregated.
- **Plain engulfings were never bucketed** — the bucket logic is reject-only by
  construction (pre-v1.13 arch journals have bucket tags on rejects only).

---

## 2. The trade simulation

To convert the annotations into gain/loss, trades were replayed offline from the
journals' own BAR OHLC lines (`pnl_sim.py`, `pnl_byyear.py`, `pnl_fullyear.py`):

| rule | value |
|---|---|
| entry | at the signal bar's **close** (chase) — or the v1.09 retrace limit (50% body) variant |
| stop | the **engulf extreme** (bar low bull / high bear) |
| target | **+2R fixed** (grid 1R/1.5R/2R/time-only was checked; 2R reported) |
| time exit | close of bar 16 if neither hit |
| intrabar | **pessimistic** — stop checked before target on every bar; gap-through-stop fills at open |
| costs | 1 pip round-trip deducted per trade (≈10 points on 3/5-digit symbols) |
| filter | signals with stop distance < 3 pips skipped (untradeable) |
| buckets | exactly the chart colors: A-grade / momentum / other (rejects only), plus plain engulfings |

Every journal was produced by remote headless Strategy Tester runs
(IC Markets demo, Model=0) and **verified by the OHLC replay checker before its
trades were counted**.

> **Pre-audit results from this offline sim were VOID** (bear-TP bug — see the
correction banner). Post-audit, the corrected offline sim agrees with the in-EA
tick-exact `TRD` simulator (Model=4) within ~0.03R on every cell and serves as
the independent cross-check; the numbers in Section 3 are the tick-exact ones.

---

## 3. Results

All figures below are the **corrected Model=4 tick-exact** results (in-EA `TRD`
simulator, 72,326 trades, 18 pairs × 2022–2025, net of the 1-pip round trip; the
corrected offline sim agrees within ~0.03R on every cell). The pre-audit tables
(+0.76R pooled momentum, 16/16 positive cells, 71/72 symbols-positive) were
artifacts of the two analysis bugs documented in the correction banner above and
have been deleted from this log.

### 3.1 Corrected per-bucket results

| bucket | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|
| momentum | −0.07R | −0.08R | −0.16R | −0.11R |
| A-grade | −0.16R | −0.16R | −0.19R | −0.21R |
| other | −0.17R | −0.20R | −0.19R | −0.17R |
| ALL | −0.15R | −0.18R | −0.19R | −0.16R |

**Net-negative in every bucket, every year.** Gross (before the 1-pip cost) the
rule set is ≈ breakeven (−0.02…−0.06R). All 16 momentum year-quarter cells
≤ +0.01R. Exit mix (momentum): TP 16–19% / SL 47–49% / time-exit 32–37%;
time-exit drift ≈ +0.1R — the 2R target is simply not reached often enough before
the engulf extreme is taken out (barrier odds 17:49 at 2:1 payoff). Slippage is
negligible (TP fills +0.01…+0.06R beyond target, SL fills −0.01…−0.03R).

### 3.2 The controls, re-scored after the audit

- **Entry timing (lesson survives, numbers void).** A signal exists only at its
  bar's close; any sim that fills at the signal bar's *open* measures the signal
  bar's own move, not a tradeable edge. The original +1.64R/91% open-entry
  control was produced by the buggy sim and is void — treat open-entry
  comparisons as a fantasy baseline, nothing more.
- **Excursion vs barrier (excursion half survives).** The 4-bar excursion study
  (C3/RET MFE/MAE, no TP logic) survives the audit: momentum bars are the *worst*
  family and A-grade the best. The claimed "ranking reversal" under 2R barrier
  touches came from the buggy sim and is void — under corrected accounting every
  family is net-negative, momentum is the least-bad (−0.07…−0.16R), and A-grade
  sits at the bottom in 2025 (−0.21R). The surviving lesson: bar-shape quality on
  *excursions* does not transfer to P&L under a fixed tight-stop/tight-target
  geometry.
- **Sweep-confluence comparison (void).** The "rejects ≈ non-rejects" claim was
  computed from the same buggy sim and is not re-derivable from the reject-only
  v1.13 stream; the archived v1.08–v1.12 journals preserve the underlying data.

### 3.3 Data integrity (survives the audit)

Every journal's detection stream was replay-verified from OHLC before use:
**Q1 campaigns 54/54, full-year 48/72 bit-exact; the 24 full-year exceptions are
1–4-line diffs each** — year-boundary window spill (2025 runs open on 2024-12-27
backfill bars), display-precision `dayLow` rounding, or isolated single-line
C3/RET diffs. Structural battery passes on all 72; none of the diffs touch the
REJECT stream materially.

---

## 4. Conclusion (corrected)

1. **The owner question is answered NO.** An engulfing bar with momentum — with
   or without a level sweep — is *not* a good entry under the tested geometry
   (close entry, SL = engulf extreme, TP = 2R, 16-bar flat exit): net-negative
   in every bucket, every year, on 2022–2025 × 18 pairs; gross of costs it is
   ≈ breakeven and the 1-pip round trip sinks it. Do not build
   `RejectionTrade.mq5` on these rules.
2. **The 2R fixed target is the binding constraint**, not the entry: barrier
   odds 17:49 at 2:1 payoff, TP reached only 16–19% of the time before the
   engulf extreme is taken out. Exit-side variants (partials, time-based,
   wider/looser targets) are the natural next lever — re-scoreable from the
   existing TRD/RET data without new tester runs, each requiring a fresh
   a-priori spec before any positive claim.
3. **Excursion quality does not transfer to barrier P&L.** A-grade rejects win
   the 4-bar excursion study yet sit at the bottom under barrier accounting;
   momentum is the least-bad. Bar-shape grading must match the exit style being
   scored — a geometry lesson that survives the audit.
4. **The verification discipline worked.** The tick-exact in-EA simulator plus
   per-trade reconciliation is what exposed the offline-sim bug; annotate-first
   and verify-first held. Any future positive claim from this research line
   must be tick-exact before it is believed.

## 5. Caveats (updated post-audit)

- Corrected results are **Model=4, tick-resolved from real ticks** — the earlier
  "Model=0 fills" caveat is resolved; no fill-model doubt remains.
- Costs are a flat 1 pip round trip; true fast-market stop slippage could be
  worse, but with gross ≈ breakeven no plausible cost model turns the rule
  positive — the verdict is cost-robust.
- The 2R/16-bar exit was fixed *a priori* from the Q1-2024 study, so 2023–2025
  remain genuinely out-of-sample *for the exit rule*; the corrected verdict
  holds on every year including the a-priori year.
- Findings cover 18 FX majors/crosses; untested on gold, indices, or exotics.

## 6. Artifacts

- **Corrected-campaign artifacts (authoritative):** `E:\tmp\rproto_m4\` — 72
  Model=4 journals, `REPORT.md`, verify tables, corrected aggregator
  (`trd_analysis2.py`), fixed full-year sim (`pnl_fullyear.py`)
- Pre-audit artifacts (void sim outputs, kept for the audit record):
  `E:\tmp\rproto_backtest\` — Q1 journals (`remote\`), full-year journals
  (`remote_full\`, 505 MB), original buggy sims (`pnl_sim.py`, `pnl_byyear.py`),
  campaign `report.txt` / `verify_table.csv` / `mismatch_details.txt`
- Verifier: `E:\tmp\rproto_backtest\verify_symbol.py` (version-aware, v1.13 gate)
- EA: `RejectionProto.mq5` v1.13 (reject-only; `TRD` simulator since v1.12);
  archived v1.08–v1.12 journals preserve the removed non-reject streams

## 7. Disposition (DEC-034)

The tested rule set is a clean negative — `RejectionTrade.mq5` as specified must
not be built. Owner options per DEC-034:

- **(a)** Close the RejectionProto thread as a negative result (v1.13
  reject-only remains the shipped annotation tool).
- **(b)** Exit-side variants only — the 2R fixed target is the binding
  constraint; partials/time-based exits re-scoreable from existing TRD/RET data
  without new tester runs, each requiring a fresh a-priori spec first.
- **(c)** Entry-side variants (retrace limit entries, already instrumented via
  RET) — full campaign required before any positive claim.
