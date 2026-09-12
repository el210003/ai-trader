# Checkpoint Record

Each checkpoint is a named Git tag plus this record of what the project's
state is, what's known-good, and what's still open. Use a tag to return to a
known-good state:  `git checkout <tag>` (or `git switch -c <branch> <tag>`).

---

## Checkpoint `v0.1.0` — portable & measurably honest

**Tag:** `v0.1.0` · **HEAD:** `a529e0f`
**Recorded:** after the "make a checkpoint" request.

### What this checkpoint represents
A fully portable, self-contained project that runs on a fresh machine, with
the measurement loop made honest. The core feature set is complete and
verified end-to-end.

### Known-good (verified in this session)
- **Portable checkout** — `setup.bat` bootstraps (venv + deps + verify);
  committed launchers (`serve/ingest/analyze/train/outcomes/insights/check_env`)
  auto-pick the venv. Fresh clone reaches a running dashboard in 3 commands.
- **`app/data` package committed** — the bare `data/` gitignore rule (which
  silently un-tracked `app/data` and broke clones with
  `No module named 'app.data'`) is fixed to `/data/`; 34 tracked .py == 34
  on-disk .py; all modules import.
- **Setup generation** — SMC (structure/sweeps/premium-discount/OB/FVG) →
  setups with retest-buffer + entry-validity + present-time gates; MTF
  confluence (H1/H4) projected for M15 entries; CSM28 strip.
- **ML** — 16 features, HistGradientBoosting (sklearn default; xgboost-CUDA
  optional), **isotonic probability calibration** (out-of-fold), dead features
  pruned; auto-retrain on feature-set AND builder-version change.
- **Measurement** — outcomes resolved (WIN/LOSS/EXPIRED, fill tolerance,
  entry-validity), **identity-based journal dedup** (no duplicate inflation),
  Perf tab (by verdict/score/calibration/HTF/symbol/session), insight engine +
  feature-tercile report, tier-3 live-outcome training blend (guarded).
- **Ops** — parallel bar-close analysis (process pool), parallel training
  replay, watcher watchdogs/heartbeats/auto-reconnect, process logging to
  `logs/ai-trader.log`.

### Key config defaults (config.yaml)
`mtf.entry_tf: M15` (M15 only setups; H1/H4 context) · `smc.retest_buffer_atr:
0.20` · `smc.entry_valid_bars: 24` · `ai.ml.backend: sklearn` ·
`ai.ml.parallel: true` · `ai.ml.live_outcomes.min_samples: 200` ·
`dashboard.parallel_analysis: true` · `dashboard.debug: true`.

### Data state at checkpoint
- Live DB migrated: journal 2,131 → **396 real setups** (backup
  `setups_history_backup_1789151541`).
- Deployed model is **19-feature + uncalibrated** → `model_needs_retrain()` is
  TRUE on deploy; `serve.bat` restart triggers a one-time auto-retrain into
  the **16-feature + isotonic-calibrated** model.

### Open / follow-ups
1. Restart `serve.bat` once so `ensure_model_current` retrains the deployed
   model into the calibrated 16-feature version.
2. After ~2–4 weeks of resolved outcomes: re-draw the calibration curve,
   re-run the feature-tercile report, decide `hybrid.ml_weight`/`llm_weight`.
3. Optional: `smc.market_entry` variant (off by default) if more fills wanted.
4. Optional: shadow testing (paper A/B for builder changes) — deferred.
