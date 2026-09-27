# Rejection-bar entry family (RejectionProto port)

The rejection-bar detector from the `RejectionProto` research line
(`docs/rejectionproto-research.md`) is now ported into ai-trader as a **setup
family**: an engulfing bar that swept a live level and closed back — the
spring/fakey event — arms a tradeable setup with its own entry/SL/TP geometry.

This document is the **a-priori spec** (per the research's discipline: a fresh
spec is written down before any positive claim is measured — DEC-034).

---

## What the research forbids (and what we do instead)

The corrected tick-exact verdict (72,326 trades, 18 pairs × 2022–2025) killed
one specific geometry:

| rejected rule | ai-trader replacement |
|---|---|
| entry at the signal bar's **close** (chase) | retrace **limit at 50% of the bar body** (the RET variant), valid 8 bars |
| SL at the engulf extreme | same idea, but + `sl_buffer_atr` buffer and a `min_risk_atr` volatility floor |
| **fixed +2R target** (the binding constraint: TP hit only 16–19%) | **nearest opposing liquidity pool / swing level** with `min_rr` sanity — the same TP logic zone setups use |
| 16-bar flat time exit | not ported; outcomes resolve WIN/LOSS/EXPIRED over the standard label horizon |

`mode: close` (chase at bar close) remains available in config for A/B
comparison, but the research predicts it loses under tight fixed targets; it
is off by default (`mode: retrace`).

## Detection (ported line-faithful from RejectionProto.mq5, §1.1–1.2)

- **Engulfing test** vs the prior bar — D1 body engulf (direction-corrected
  cover), D2 outside bar, D3 decisive. D3 uses **integer-point math**
  (`bodyPts*3 >= rangePts*2`, half-up rounding) exactly as the EA — float
  comparison mislands knife-edge bars (the documented D3 lesson).
- **Sweep test** — wick pierces a live level, close retreats, against three
  books: every confirmed (unbroken) M15 swing high/low, equal-highs/lows
  pools (ai-trader's clustered liquidity, additive), and the **running
  server-day high/low** (`use_day_extremes`, the EA's `InpUseDayExtreme`) —
  day extremes reset at server midnight and are never body-broken.
- **REJECT** = engulfing ∧ swept ≥ 1 level. No-hindsight rule preserved
  (ProcessBar order): evaluate → fold day extremes → body-close breaks →
  add pivots confirmed by this bar.
- **Pivot book** uses the EA's tie-break verbatim (`IfPivotAdd`): equality
  allowed against the OLDER flank, strict against the NEWER — on a flat
  top/bottom (EQH/EQL) the **newest touch keeps the swing reference**.
- **Bucket classification** — `exp10` = half-up tenths from **integer-point**
  bodies vs the 20-bar average (signal bar excluded); `clvPct` = integer
  close-location percent; `haveExp = exp10 > 0` (an expansion rounding to 0
  is "no baseline"); momentum tested first with the `big_exp_tenths > 0`
  gate; A-grade = `exp10 < mom_exp_tenths && clvPct < weak_close_pct`.
- **Retrace entry** follows the EA's `RetraceArm` exactly: a limit
  `retrace_pct` deep into the body **from the close side**, depth rounded
  half-up to a placeable point price (`depthPts = (bodyPts*pct+50)/100`).

The one remaining deviation: the noise gate is ATR-relative (`min_range_atr`,
transfers across symbols) instead of the EA's fixed `InpMinRangePoints`.

## From detection to setup (`smc.rejection` config)

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | emit rejection setups alongside zone setups |
| `mode` | `retrace` | `retrace` = limit inside the bar body, `close` = chase at bar close |
| `retrace_pct` | `50` | entry depth into the bar body, % (50 = RET) |
| `entry_valid_bars` | `8` | RET fill window (per-setup: the labeler + outcome resolver honor it over the global 24) |
| `trigger_lookback_bars` | `2` | how recent the rejection bar may be and still arm a setup |
| `require_directional_sweep` | `true` | longs need a sellside sweep, shorts a buyside sweep (set `false` for the prototype's any-sweep rule) |
| `buckets` | `[0, 1, 2]` | which buckets are tradeable |
| `sl_buffer_atr` / `min_risk_atr` | `0.25` / `0.30` | stop buffer beyond the bar extreme / volatility floor (rejection stops are tighter than zone stops' 0.75) |
| `max_retrace_atr` | `1.0` | skip when the retrace entry sits farther than this from the bar close |
| `use_day_extremes` | `true` | sweep against the running server-day H/L (EA `InpUseDayExtreme`) |
| `avg_body_bars`, `mom_exp_tenths`, `big_exp_tenths`, `weak_close_pct`, `min_range_atr` | `20/20/30/50/0.05` | detector parameters (§1.2) |

`min_rr` / `default_rr` / `max_rr` / `retest_buffer_atr` / `skip_already_tested`
are shared with the zone family (top-level `smc` keys).

Setup payload additions (visible in the journal, Perf tab, and trades):

```json
"setup_kind": "rejection",
"entry_style": "retrace",
"rejection": {"bucket": 1, "exp": 3.2, "clv": 91, "br": 91, "depth": 5,
              "defs": 5, "mode": "retrace", "swept": [...]}
```

## ML integration

Four new features ride every setup (neutral priors for zone setups):
`rej_exp`, `rej_clv`, `rej_is_momentum`, `rej_is_agrade`. Adding features
changes the feature set → the deployed model is flagged stale and auto-retrains
on the next `serve.bat` restart (the documented auto-retrain contract).

## Execution

No new engine gates. Rejection setups flow through the same pipeline
(ML → LLM → hybrid verdict → Trade tab). Per-setup `entry_style` overrides the
engine's `entry_type`: `retrace` setups are sent as **limit orders at the
retrace level** (falling back to market if price is already at/past the level),
`close` setups use the engine default.

## Measurement plan (honest-accounting rules)

1. Run the engine in **DRY-RUN** first (Trade tab toggle) — the journal and
   outcome resolver treat dry-run and live identically.
2. Compare families in the Perf tab: `entry_zone.type == "rejection_bar"`
   setups vs `order_block`/`fvg` setups, and rejection setups by **bucket**.
3. Watch the RET-specific failure mode the prototype measured: unfilled
   retrace entries (`EXPIRED_UNFILLED`) are the cost of the 50% limit — the
   fill rate vs the close-chase baseline is itself a result.
4. Caveat: outcome resolution is bar-close based and pessimistic (SL wins
   same-bar ties, touch = fill) — good enough for family comparison, **not**
   tick-exact. Per the research's discipline, any positive claim worth acting
   on must be confirmed tick-exact (Strategy Tester Model=4) before it is
   believed.

## Research linkage

- `docs/rejectionproto-research.md` — corrected verdict (DEC-034), detection
  spec (§1.1), bucket spec (§1.2), corrected per-bucket results (§3.1).
- What transfers: the detection layer (replay-verified), the bucket taxonomy,
  the RET instrument, and the lesson that the fixed-2R target was the binding
  constraint. What does not: the momentum-reject close-chase edge (proven
  negative) and any pre-audit positive number from that document.
