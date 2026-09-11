# Setup-Journal Design Review & Fix Proposal

**Status:** PROPOSED — for approval
**Reviewed against:** live DB (2,123 journal rows, 1,290 resolved outcomes)
**Symptom reported:** journal fills with setups that "just wait" and never seem
to close; stats/insights look unreliable.

## Verified facts (from data)

| Metric | Value | Meaning |
|---|---|---|
| Journal rows | 2,123 | but only **390 distinct** setups (sym+tf+dir+entry) |
| Avg resolution copies | 3.3× | worst: USDCHF long logged **49×** across 49 bars |
| Resolved rows | 1,290 | from ~390 real decisions → 3.3× inflation |
| Fill rate (resolved) | **87%** | limit retests DO get filled — not the problem |
| Never-filled (expired) | 168 | ~13% genuinely never touch entry |
| Bindings: BUY+SELL 83 · WAIT 1,846 · AVOID 194 | | engine rarely confirms a trade |
| Horizon | 96 M15 bars = 24h | a setup can't resolve for a day |

## Root cause #1 — journal dedup is identity-broken

The dedup key is `(symbol, tf, formed_at, direction)`, but `formed_at` = the
last closed bar's time and changes **every M15 bar**. The same persistent setup
(same zone → same entry, same SL/TP) is re-journaled as a "new" trade each bar
for as long as it stays valid.

**Damage:** every downstream consumer is inflated by ~3.3×:
- journal counts, Perf-tab win rate / expectancy / by-verdict / calibration
- insight engine ("Losing regime: USDJPY −0.408R" etc.)
- **tier-3 live-outcome training blend** (training on N copies of the same few
  setups → serious overfit)

## Root cause #2 — perception "it just waits"

- Recurring setups re-log every bar → the journal shows the same trade stacking
  up as many `OPEN` rows with identical levels.
- Each row only resolves after its **own** 24h window (and duplicates each get
  their own), so fresh + duplicate rows pile up as `OPEN` (~841) — it looks like
  nothing ever closes even though 87% fill and most resolve.
- True never-fills (~168) are the legitimate "waiting for a retest" setups.

## Proposed fix (ordered)

### 1. Identity-based journaling (the core fix)
Dedup on the **real setup identity**: `(symbol, tf, direction,
entry_zone.origin_time, entry)`.
- A persistent setup is journaled **once**; subsequent cycles **UPDATE** the
  same row (verdict, score, ml_prob, formed_at → latest) instead of inserting.
- This collapses the doubling; journal + stats + insights + training become
  honest automatically.

### 2. Dedup migration of existing data
One-time script to collapse the 2,123 rows → ~390, keeping the earliest
formation and the first resolved outcome per identity. Optionally back up the
raw table first.

### 3. Make the resolver close stale setups cleanly
- Track each setup's **first** formed_at; a row whose horizon elapsed relative
  to its first occurrence resolves once (EXPIRED_OPEN / EXPIRED_UNFILLED) and
  stops re-appearing.
- Prevent the same open setup from being duplicated as a fresh pending row.

### 4. (Follow-up) actionable-now clarity
Cards already show "X ATR to entry". Optionally add a **market-vs-limit** tag:
- "limit pending (retest)" when entry is below price and ATR distance > 0 — the
  normal wait state
- "actionable now" when within a tight ATR band
No change to logic — purely communicates what the journal row means.

## Expected effect
- Journal: 2,123 → ~390 rows, each a real open/closed decision.
- Perf-tab / insights / tier-3 training: computed on real samples, not copies.
- No more "it just waits forever" — open count reflects genuinely-unresolved
  setups (those within their 24h window), not duplicate stacking.

## Effort
- Fix 1: small (append_setups / update logic + journal row update).
- Fix 2: small script (idempotent, backs up first).
- Fix 3: small (resolver consent on first formed_at + no duplicate pending).
- Fix 4: small UI.

Recommended: implement **1 + 2 + 3 now**, keep 4 as a quick polish.
