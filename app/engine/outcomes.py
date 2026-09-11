"""Live outcome resolution for journaled trade setups.

Every journaled setup (setups_history) is resolved against the actual
entry-timeframe candles that formed AFTER it, using the SAME conservative
rules as the ML labeler (app/engine/labeler.py):

  - limit order fills when price touches `entry`
  - SL wins same-bar ties (conservative)
  - WIN  -> r_multiple = the setup's planned RR
  - LOSS -> r_multiple = -1.0
  - EXPIRED_UNFILLED: entry never touched within the horizon
  - EXPIRED_OPEN:     filled but neither TP nor SL hit within the horizon
                      (MFE/MAE report how far it travelled)
  - VOID:             unresolvable data (levels invalid, candles pruned)

Only unresolved rows are processed (idempotent); resolution runs after each
analysis cycle, on demand via POST /api/outcomes/resolve, and via the
`outcomes` CLI command.
"""
import time
from collections import defaultdict

import numpy as np

WIN, LOSS = "WIN", "LOSS"
EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
EXPIRED_OPEN = "EXPIRED_OPEN"
VOID = "VOID"


def _resolve_one(df, r: dict, horizon: int):
    """Resolve one pending setup row against candles. Returns outcome dict or
    None when there is not enough future data yet (retry next cycle)."""
    entry, sl, tp = r["entry"], r["stop_loss"], r["take_profit"]
    direction = (r["direction"] or "").lower()
    if entry is None or sl is None or tp is None or direction not in ("long", "short"):
        return {**_base(r), "result": VOID, "resolved_at": int(time.time())}
    risk = abs(entry - sl)
    if risk <= 0:
        return {**_base(r), "result": VOID, "resolved_at": int(time.time())}

    times = df["time"].values
    start = int(np.searchsorted(times, r["formed_at"], side="right"))
    n = len(df)
    if start >= n:
        return None                      # no future bars yet
    if start == 0 and times[0] > r["formed_at"]:
        return {**_base(r), "result": VOID, "resolved_at": int(time.time())}

    filled = False
    fill_time = None
    mfe = mae = 0.0
    end = min(n, start + max(1, int(horizon)))
    for k in range(start, end):
        hi = float(df["high"].iat[k])
        lo = float(df["low"].iat[k])
        if direction == "long":
            if not filled:
                if lo <= entry:
                    filled, fill_time = True, int(times[k])
                else:
                    continue
            # conservative: SL wins same-bar ties (mirrors the labeler)
            if lo <= sl:
                mae = max(mae, (entry - lo) / risk)
                hit = _post_loss_tp(df, k, end, direction, tp)
                return _outcome(r, LOSS, filled, fill_time, -1.0, mfe, mae,
                                k - start, times[k], post_loss_tp_hit=hit)
            if hi >= tp:
                mfe = max(mfe, (hi - entry) / risk)
                return _outcome(r, WIN, filled, fill_time, float(r["rr"] or 0.0),
                                mfe, mae, k - start, times[k])
            mfe = max(mfe, (hi - entry) / risk)
            mae = max(mae, (entry - lo) / risk)
        else:
            if not filled:
                if hi >= entry:
                    filled, fill_time = True, int(times[k])
                else:
                    continue
            if hi >= sl:
                mae = max(mae, (hi - entry) / risk)
                hit = _post_loss_tp(df, k, end, direction, tp)
                return _outcome(r, LOSS, filled, fill_time, -1.0, mfe, mae,
                                k - start, times[k], post_loss_tp_hit=hit)
            if lo <= tp:
                mfe = max(mfe, (entry - lo) / risk)
                return _outcome(r, WIN, filled, fill_time, float(r["rr"] or 0.0),
                                mfe, mae, k - start, times[k])
            mfe = max(mfe, (entry - lo) / risk)
            mae = max(mae, (hi - entry) / risk)

    if not filled:
        if end < start + horizon:
            return None          # horizon not fully observed yet — wait for bars
        return _outcome(r, EXPIRED_UNFILLED, False, None, None, 0.0, mae, horizon, None)
    if end < start + horizon:
        return None              # still could hit TP/SL within the horizon
    return _outcome(r, EXPIRED_OPEN, True, fill_time, None, mfe, mae, horizon, None)


def _post_loss_tp(df, loss_k: int, end: int, direction: str, tp: float) -> int:
    """Tier-0 diagnostic: after being stopped out, did price reach what would
    have been TP within the remaining horizon? (SL-placement quality measure.)"""
    for k in range(loss_k + 1, end):
        if direction == "long":
            if float(df["high"].iat[k]) >= tp:
                return 1
        else:
            if float(df["low"].iat[k]) <= tp:
                return 1
    return 0


def _base(r: dict) -> dict:
    return {"setup_id": r["id"], "symbol": r["symbol"], "tf": r["tf"],
            "direction": r["direction"]}


def _outcome(r: dict, result, filled, fill_time, r_mult, mfe, mae, bars, t,
             post_loss_tp_hit=None):
    return {**_base(r), "result": result, "filled": 1 if filled else 0,
            "fill_time": fill_time, "r_multiple": r_mult,
            "mfe_r": round(float(mfe), 3), "mae_r": round(float(mae), 3),
            "bars_to_outcome": int(bars),
            "post_loss_tp_hit": post_loss_tp_hit,
            "resolved_at": int(t) if t else int(time.time())}


def resolve_pending(store, cfg: dict, verbose: bool = False) -> int:
    """Resolve all pending journaled setups. Returns the number resolved."""
    horizon = int(cfg.get("ai", {}).get("ml", {}).get("label_horizon_bars", 96))
    lookback = int(cfg.get("outcomes", {}).get("lookback_days", 30))
    pending = store.pending_setups(lookback)
    if not pending:
        return 0
    by_pair = defaultdict(list)
    for r in pending:
        by_pair[(r["symbol"], r["tf"])].append(r)
    resolved = 0
    for (symbol, tf), rows in by_pair.items():
        df = store.load_candles(symbol, tf)
        if df is None or len(df) < 10:
            continue
        for r in rows:
            try:
                out = _resolve_one(df, r, horizon)
            except Exception:
                continue
            if out is not None:
                store.save_outcome(out)
                resolved += 1
    if verbose and resolved:
        print(f"  [outcomes] resolved {resolved} setup(s)")
    return resolved


# ---------------------------------------------------------------- aggregates
def _win_rate(rs):
    return round(sum(1 for r in rs if r["result"] == WIN) / len(rs), 3) if rs else None


def _expectancy(rs):
    vals = [r["r_multiple"] for r in rs if r["r_multiple"] is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _bucket(rs):
    resolved = [r for r in rs if r["result"] in (WIN, LOSS)]
    return {"n": len(rs), "resolved": len(resolved),
            "wins": sum(1 for r in resolved if r["result"] == WIN),
            "win_rate": _win_rate(resolved), "expectancy_r": _expectancy(resolved)}


def aggregate(rows: list) -> dict:
    """Aggregate resolved outcome rows into the Performance-tab views."""
    for r in rows:
        if r.get("payload"):
            try:
                import json
                p = json.loads(r["payload"])
                r["htf_metrics"] = p.get("htf_metrics") or {}
            except Exception:
                r["htf_metrics"] = {}
        else:
            r["htf_metrics"] = {}

    tradable = [r for r in rows if (r.get("verdict") or "").upper() in ("BUY", "SELL")]
    waiting = [r for r in rows if (r.get("verdict") or "").upper() == "WAIT"]
    avoid = [r for r in rows if (r.get("verdict") or "").upper() == "AVOID"]

    def by(f):
        groups = defaultdict(list)
        for r in rows:
            groups[f(r)].append(r)
        return {k: _bucket(v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}

    # ML calibration: predicted probability bucket vs realized win rate
    cal = defaultdict(lambda: {"pred": [], "n": 0, "wins": 0})
    for r in rows:
        mp = r.get("ml_prob")
        if mp is None:
            continue
        if r["result"] in (WIN, LOSS):
            lo = round(float(mp) * 10) / 10
            key = f"{lo:.1f}–{lo+0.1:.1f}"
            cal[key]["pred"].append(float(mp))
            cal[key]["n"] += 1
            cal[key]["wins"] += 1 if r["result"] == WIN else 0
    calibration = {k: {"predicted": round(sum(v["pred"]) / len(v["pred"]), 3),
                       "n": v["n"],
                       "realized": round(v["wins"] / v["n"], 3) if v["n"] else None}
                   for k, v in sorted(cal.items())}

    def htf_key(r):
        ta = (r.get("htf_metrics") or {}).get("trend_align")
        if ta is None:
            return "no HTF ctx"
        if ta >= 0.99:
            return "aligned"
        if ta <= 0.01:
            return "opposed"
        return "mixed"

    def session_key(r):
        h = time.gmtime(r["formed_at"]).tm_hour
        if h < 7:
            return "Asia 00–07"
        if h < 12:
            return "London 07–12"
        if h < 16:
            return "LDN/NY 12–16"
        if h < 20:
            return "NY 16–20"
        return "Late 20–24"

    resolved = [r for r in rows if r["result"] in (WIN, LOSS)]
    wins = [r for r in resolved if r["result"] == WIN]
    return {
        "overall": {
            **_bucket(rows),
            "fill_rate": (round(sum(1 for r in rows if r["filled"]) / len(rows), 3)
                          if rows else None),
            "avg_win_r": (round(sum(r["r_multiple"] for r in wins) / len(wins), 3)
                          if wins else None),
            "avg_mfe_r": (round(sum(r["mfe_r"] for r in rows if r["filled"]) /
                                max(1, sum(1 for r in rows if r["filled"])), 3)
                          if rows else None),
            "avg_mae_r": (round(sum(r["mae_r"] for r in rows if r["filled"]) /
                                max(1, sum(1 for r in rows if r["filled"])), 3)
                          if rows else None),
        },
        "by_verdict": {"BUY/SELL": _bucket(tradable), "WAIT": _bucket(waiting),
                       "AVOID": _bucket(avoid)},
        "by_score_band": by(lambda r: ("<60" if (r.get("score") or 0) < 60 else
                                       "60–70" if r.get("score") < 70 else
                                       "70–80" if r.get("score") < 80 else "80+")),
        "by_htf_alignment": by(htf_key),
        "by_symbol": by(lambda r: r["symbol"]),
        "by_direction": by(lambda r: r["direction"] or "?"),
        "by_session": by(session_key),
        "calibration": calibration,
    }
