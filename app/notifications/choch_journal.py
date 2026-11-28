"""Append-only CSV journal & 4-variant forward simulator for CHoCH alerts."""
import csv
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..data.mt5_client import TF_SECONDS
from ..data.store import Store
from ..engine.setup_builder import _target_liquidity
from ..logsetup import debug
from ..smc.order_blocks import find_order_blocks
from ..smc.structure import detect_structure, find_swings

_CSV_LOCK = threading.RLock()

CSV_COLUMNS: List[str] = [
    # 1. Identity & timing
    "event_key",
    "logged_at_utc",
    "alert_delay_sec",
    "symbol",
    "tf",
    "event_type",
    "direction",
    "side",
    # 2. Bar timestamps & session context
    "event_bar_ts",
    "bar_close_ts",
    "bar_close_utc",
    "utc_hour",
    "session",
    # 3. Market & confluence snapshot at alert time
    "entry_price",
    "broken_level",
    "swing_age_bars",
    "atr",
    "break_dist_atr",
    "invalidation_price",
    "struct_sl_dist_atr",
    "nearest_pool_price",
    "nearest_pool_dist_atr",
    "range_position",
    "range_zone",
    "has_recent_sweep",
    "last_sweep_kind",
    "last_sweep_bars_ago",
    "fresh_ob_count",
    "fresh_fvg_count",
    "htf_h1_trend",
    "htf_h4_trend",
    "htf_trend_aligned",
    "csm_status",
    "setup_verdict",
    "setup_ml_prob",
    # 4. Multi-horizon forward returns & excursions
    "outcome_status",
    "bars_observed",
    "ret_1b_atr",
    "ret_3b_atr",
    "ret_6b_atr",
    "ret_12b_atr",
    "ret_24b_atr",
    "mfe_atr",
    "mae_atr",
    "bars_to_mfe",
    "bars_to_mae",
    # 5. Variant 1: Baseline Signal (1.0 ATR SL / 2.0 ATR TP at CHoCH Close)
    "v1_entry",
    "v1_sl",
    "v1_tp",
    "v1_rr",
    "v1_result",
    "v1_r",
    "v1_bars",
    "v1_mfe_r",
    "v1_mae_r",
    # 6. Variant 2: Structural Market (CHoCH Close, Swing SL, Liquidity TP)
    "v2_entry",
    "v2_sl",
    "v2_tp",
    "v2_rr",
    "v2_result",
    "v2_r",
    "v2_bars",
    "v2_mfe_r",
    "v2_mae_r",
    # 7. Variant 3: OB Pullback Limit (Limit at CHoCH OB Edge, OB SL, Liquidity TP)
    "v3_has_ob",
    "v3_entry",
    "v3_sl",
    "v3_tp",
    "v3_rr",
    "v3_filled",
    "v3_fill_bars",
    "v3_result",
    "v3_r",
    "v3_bars",
    "v3_mfe_r",
    "v3_mae_r",
    # 8. Variant 4: Structure Trail (CHoCH Close, Swing SL, Exit on Opposing CHoCH)
    "v4_entry",
    "v4_sl",
    "v4_exit_price",
    "v4_exit_reason",
    "v4_result",
    "v4_r",
    "v4_bars",
    "v4_mfe_r",
    "v4_mae_r",
    # 9. Manual review
    "user_taken",
    "user_notes",
]


def _journal_cfg(cfg: dict) -> dict:
    tg = cfg.get("telegram") or {}
    return tg.get("csv_journal") or {}


def resolve_csv_path(cfg: dict) -> Path:
    jcfg = _journal_cfg(cfg)
    raw = str(jcfg.get("path") or "data/choch_alerts.csv").strip()
    p = Path(raw)
    if p.is_absolute():
        return p
    root = cfg.get("_root")
    base = Path(root) if root else Path(__file__).resolve().parent.parent.parent
    return (base / p).resolve()


def canonical_event_key(symbol: str, tf: str, ev: dict) -> str:
    return (
        f"{symbol}|{tf}|{str(ev.get('type') or '').lower()}|{str(ev.get('direction') or '').lower()}|"
        f"{int(ev.get('time') or 0)}|{int(ev.get('level_time') or 0)}"
    )


def _iso_utc(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def _session_name(utc_hour: int) -> str:
    if utc_hour < 7:
        return "Asia"
    if utc_hour < 12:
        return "London"
    if utc_hour < 16:
        return "LDN_NY_overlap"
    if utc_hour < 20:
        return "NY"
    return "Late_NY"


def _fmt_price(val: Optional[float]) -> str:
    if val is None:
        return ""
    return f"{float(val):.6f}".rstrip("0").rstrip(".")


def _fmt_num(val: Optional[float], digits: int = 3) -> str:
    if val is None:
        return ""
    return f"{float(val):.{digits}f}"


def _read_rows(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.is_file():
        return []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            rows.append({col: (r.get(col) if r.get(col) is not None else "") for col in CSV_COLUMNS})
        return rows


def _write_rows_atomic(csv_path: Path, rows: List[Dict[str, str]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({col: r.get(col, "") for col in CSV_COLUMNS})
    os.replace(tmp_path, csv_path)


def _find_event_ob(store: Store, symbol: str, tf: str, smc: dict, ev: dict, direction: str) -> Optional[dict]:
    ev_idx = ev.get("index")
    ev_time = int(ev.get("time") or 0)
    all_obs = (smc.get("order_blocks") or {}).get("all") or []
    for ob in all_obs:
        if ob.get("direction") != direction:
            continue
        if ev_idx is not None and ob.get("event_index") == int(ev_idx):
            return ob
        if ev_time and int(ob.get("event_time") or 0) == ev_time:
            return ob

    # Fallback if event fell outside the trimmed OB list: locate directly from candles
    df = store.load_candles(symbol, tf)
    if df is None or len(df) < 5 or not ev_time:
        return None
    matches = df.index[df["time"] == ev_time].tolist()
    if not matches:
        return None
    idx = int(matches[-1])
    syn_ev = dict(ev, index=idx, direction=direction)
    found = find_order_blocks(df, [syn_ev])
    return found[-1] if found else None


def record_choch_event(
    store: Store,
    cfg: dict,
    symbol: str,
    tf: str,
    ev: dict,
    analysis: dict,
) -> bool:
    """Record a single unseen structure event (default: CHoCH) into the CSV journal.
    Idempotent on canonical event_key so multi-bot setups log each event once.
    """
    jcfg = _journal_cfg(cfg)
    if not jcfg.get("enabled", True):
        return False

    etype = str(ev.get("type") or "").strip().lower()
    allowed_events = {str(e).strip().lower() for e in (jcfg.get("events") or ["choch"]) if str(e).strip()}
    if etype not in allowed_events:
        return False

    direction = str(ev.get("direction") or "").strip().lower()
    if direction not in ("bullish", "bearish"):
        return False

    key = canonical_event_key(symbol, tf, ev)
    csv_path = resolve_csv_path(cfg)

    with _CSV_LOCK:
        existing = _read_rows(csv_path)
        if any(r.get("event_key") == key for r in existing):
            return False

        smc = (analysis or {}).get("smc") or {}
        htf = (analysis or {}).get("htf") or {}
        setups = (analysis or {}).get("setups") or []
        meta = (analysis or {}).get("meta") or {}
        smc_cfg = cfg.get("smc") or {}

        n_bars = int(meta.get("bars") or 0)
        ev_idx = ev.get("index")
        ev_bar_ts = int(ev.get("time") or 0)
        tf_sec = int(TF_SECONDS.get(str(tf).strip().upper(), 0))
        bar_close_ts = ev_bar_ts + tf_sec if (ev_bar_ts and tf_sec) else ev_bar_ts
        now_ts = int(time.time())
        delay_sec = max(0, now_ts - bar_close_ts) if bar_close_ts else 0
        utc_hour = time.gmtime(bar_close_ts or now_ts).tm_hour

        # Exact close of the CHoCH bar (even when age_bars > 0)
        entry_price = float(smc.get("last_close") or 0.0)
        if ev_bar_ts and n_bars and isinstance(ev_idx, int) and ev_idx != (n_bars - 1):
            df_c = store.load_candles(symbol, tf)
            if df_c is not None and len(df_c) > 0:
                m = df_c[df_c["time"] == ev_bar_ts]
                if len(m) > 0:
                    entry_price = float(m["close"].iat[-1])

        broken_level = float(ev.get("level") or 0.0)
        lvl_idx = ev.get("level_index")
        swing_age = (int(ev_idx) - int(lvl_idx)) if (isinstance(ev_idx, int) and isinstance(lvl_idx, int)) else ""

        atr_v = float(smc.get("atr") or 0.0)
        atr_safe = atr_v if atr_v > 0 else 1e-9
        break_dist_atr = (abs(entry_price - broken_level) / atr_safe) if (atr_v > 0 and broken_level > 0) else None

        rng = smc.get("dealing_range") or {}
        dr_top = rng.get("top")
        dr_bot = rng.get("bottom")
        invalidation_price = float(dr_bot) if (direction == "bullish" and dr_bot is not None) else (
            float(dr_top) if (direction == "bearish" and dr_top is not None) else None
        )
        range_pos = rng.get("position")
        range_zone = str(rng.get("zone") or "")

        # Sweeps in confirming direction within sweep_lookback_bars
        sweep_lb = int(smc_cfg.get("sweep_lookback_bars", 30))
        want_sweep_side = "sellside" if direction == "bullish" else "buyside"
        ref_idx = int(ev_idx) if isinstance(ev_idx, int) else (n_bars - 1)
        recent_sweeps = [
            s for s in (smc.get("sweeps") or [])
            if s.get("side") == want_sweep_side
            and isinstance(s.get("index"), int)
            and 0 <= (ref_idx - s["index"]) <= sweep_lb
        ]
        has_recent_sweep = 1 if recent_sweeps else 0
        last_sweep = recent_sweeps[-1] if recent_sweeps else None
        last_sweep_kind = str(last_sweep.get("kind") or "") if last_sweep else ""
        last_sweep_bars_ago = (ref_idx - int(last_sweep["index"])) if last_sweep else ""

        # Active aligned OBs & FVGs
        fresh_obs = [o for o in ((smc.get("order_blocks") or {}).get("fresh") or []) if o.get("direction") == direction]
        fresh_fvgs = [g for g in ((smc.get("fvgs") or {}).get("fresh") or []) if g.get("direction") == direction]

        # HTF trends
        h1_trend = str((htf.get("H1") or {}).get("trend") or "")
        h4_trend = str((htf.get("H4") or {}).get("trend") or "")
        htf_trends = [t for t in (h1_trend, h4_trend) if t]
        if not htf_trends:
            htf_aligned = "none"
        elif all(t == direction for t in htf_trends):
            htf_aligned = "aligned"
        elif all(t != direction for t in htf_trends):
            htf_aligned = "opposed"
        else:
            htf_aligned = "mixed"

        # CSM alignment
        csm_status = "neutral"
        try:
            csm = store.load_csm()
            csm_dir = ((csm.get("aligned") or {}).get(symbol)) if csm else None
            want_trade_dir = "long" if direction == "bullish" else "short"
            if csm_dir == want_trade_dir:
                csm_status = "aligned"
            elif csm_dir in ("long", "short"):
                csm_status = "opposed"
        except Exception:
            pass

        # Matching-direction setup from the same analysis pass
        want_trade_dir = "long" if direction == "bullish" else "short"
        matching_setups = [s for s in setups if s.get("direction") == want_trade_dir]
        best_setup = matching_setups[0] if matching_setups else None
        setup_verdict = str(best_setup.get("verdict") or "NONE") if best_setup else "NONE"
        setup_ml_prob = best_setup.get("ml_prob") if best_setup else None

        # ---- Compute Entry / SL / TP for all 4 simulation variants ----
        sl_atr = float(jcfg.get("sl_atr", 1.0))
        tp_atr = float(jcfg.get("tp_atr", 2.0))
        buffer_atr = float(smc_cfg.get("sl_buffer_atr", 0.40))
        min_risk = float(smc_cfg.get("min_risk_atr", 0.75)) * atr_v
        min_rr = float(smc_cfg.get("min_rr", 1.5))
        default_rr = float(smc_cfg.get("default_rr", 2.0))
        max_rr = float(smc_cfg.get("max_rr", 3.2))

        htf_pools: List[float] = []
        pool_side = "buyside" if direction == "bullish" else "sellside"
        for c in htf.values():
            if isinstance(c, dict):
                htf_pools.extend(float(p) for p in (c.get(pool_side) or []))

        smc_for_tp = {
            "liquidity_pools": smc.get("liquidity_pools") or [],
            "swings": smc.get("swings") or [],
        }

        # Variant 1: Baseline Signal (1.0 ATR SL / 2.0 ATR TP at CHoCH Close)
        v1_entry = entry_price
        if atr_v > 0:
            v1_risk = max(sl_atr * atr_v, min_risk)
            if direction == "bullish":
                v1_sl = v1_entry - v1_risk
                v1_tp = v1_entry + tp_atr * atr_v
            else:
                v1_sl = v1_entry + v1_risk
                v1_tp = v1_entry - tp_atr * atr_v
            v1_rr = abs(v1_tp - v1_entry) / v1_risk
            v1_status = "PENDING"
        else:
            v1_sl = v1_tp = v1_rr = None
            v1_status = "VOID"

        # Variant 2: Structural Market & Variant 4: Structure Trail
        v2_entry = entry_price
        if atr_v > 0:
            if direction == "bullish":
                cands = [invalidation_price] if invalidation_price is not None else [v2_entry - min_risk]
                sl_base = min(cands)
                v2_sl = sl_base - buffer_atr * atr_v
                v2_risk = max(v2_entry - v2_sl, min_risk)
                v2_sl = v2_entry - v2_risk
                v2_tp, v2_rr = _target_liquidity(
                    "long", v2_entry, v2_entry, v2_risk, smc_for_tp, min_rr, default_rr, extra_pools=htf_pools
                )
                if v2_rr > max_rr:
                    v2_tp = v2_entry + max_rr * v2_risk
                    v2_rr = max_rr
            else:
                cands = [invalidation_price] if invalidation_price is not None else [v2_entry + min_risk]
                sl_base = max(cands)
                v2_sl = sl_base + buffer_atr * atr_v
                v2_risk = max(v2_sl - v2_entry, min_risk)
                v2_sl = v2_entry + v2_risk
                v2_tp, v2_rr = _target_liquidity(
                    "short", v2_entry, v2_entry, v2_risk, smc_for_tp, min_rr, default_rr, extra_pools=htf_pools
                )
                if v2_rr > max_rr:
                    v2_tp = v2_entry - max_rr * v2_risk
                    v2_rr = max_rr
            struct_sl_dist_atr = v2_risk / atr_safe
            nearest_pool_price = v2_tp
            nearest_pool_dist_atr = abs(v2_tp - v2_entry) / atr_safe
            v2_status = "PENDING"
            v4_status = "PENDING"
        else:
            v2_sl = v2_tp = v2_rr = None
            struct_sl_dist_atr = nearest_pool_price = nearest_pool_dist_atr = None
            v2_status = "VOID"
            v4_status = "VOID"

        # Variant 3: OB Pullback Limit (Limit at CHoCH OB edge, OB SL, Liquidity TP)
        choch_ob = _find_event_ob(store, symbol, tf, smc, ev, direction) if atr_v > 0 else None
        if choch_ob is not None and atr_v > 0:
            v3_has_ob = 1
            if direction == "bullish":
                v3_entry = min(float(choch_ob["top"]), entry_price)
                v3_sl = float(choch_ob["bottom"]) - buffer_atr * atr_v
                v3_risk = max(v3_entry - v3_sl, min_risk)
                v3_sl = v3_entry - v3_risk
                v3_tp, v3_rr = _target_liquidity(
                    "long", v3_entry, v3_entry, v3_risk, smc_for_tp, min_rr, default_rr, extra_pools=htf_pools
                )
                if v3_rr > max_rr:
                    v3_tp = v3_entry + max_rr * v3_risk
                    v3_rr = max_rr
            else:
                v3_entry = max(float(choch_ob["bottom"]), entry_price)
                v3_sl = float(choch_ob["top"]) + buffer_atr * atr_v
                v3_risk = max(v3_sl - v3_entry, min_risk)
                v3_sl = v3_entry + v3_risk
                v3_tp, v3_rr = _target_liquidity(
                    "short", v3_entry, v3_entry, v3_risk, smc_for_tp, min_rr, default_rr, extra_pools=htf_pools
                )
                if v3_rr > max_rr:
                    v3_tp = v3_entry - max_rr * v3_risk
                    v3_rr = max_rr
            v3_status = "PENDING"
        else:
            v3_has_ob = 0
            v3_entry = v3_sl = v3_tp = v3_rr = None
            v3_status = "NO_OB" if atr_v > 0 else "VOID"

        row = {
            "event_key": key,
            "logged_at_utc": _iso_utc(now_ts),
            "alert_delay_sec": str(delay_sec),
            "symbol": symbol,
            "tf": str(tf).strip().upper(),
            "event_type": etype,
            "direction": direction,
            "side": "BUY" if direction == "bullish" else "SELL",
            "event_bar_ts": str(ev_bar_ts),
            "bar_close_ts": str(bar_close_ts),
            "bar_close_utc": _iso_utc(bar_close_ts) if bar_close_ts else "",
            "utc_hour": str(utc_hour),
            "session": _session_name(utc_hour),
            "entry_price": _fmt_price(entry_price),
            "broken_level": _fmt_price(broken_level),
            "swing_age_bars": str(swing_age),
            "atr": _fmt_price(atr_v),
            "break_dist_atr": _fmt_num(break_dist_atr, 3),
            "invalidation_price": _fmt_price(invalidation_price),
            "struct_sl_dist_atr": _fmt_num(struct_sl_dist_atr, 3),
            "nearest_pool_price": _fmt_price(nearest_pool_price),
            "nearest_pool_dist_atr": _fmt_num(nearest_pool_dist_atr, 3),
            "range_position": _fmt_num(range_pos, 3) if range_pos is not None else "",
            "range_zone": range_zone,
            "has_recent_sweep": str(has_recent_sweep),
            "last_sweep_kind": last_sweep_kind,
            "last_sweep_bars_ago": str(last_sweep_bars_ago),
            "fresh_ob_count": str(len(fresh_obs)),
            "fresh_fvg_count": str(len(fresh_fvgs)),
            "htf_h1_trend": h1_trend,
            "htf_h4_trend": h4_trend,
            "htf_trend_aligned": htf_aligned,
            "csm_status": csm_status,
            "setup_verdict": setup_verdict,
            "setup_ml_prob": _fmt_num(setup_ml_prob, 3) if setup_ml_prob is not None else "",
            "outcome_status": "PENDING" if atr_v > 0 else "VOID",
            "bars_observed": "0",
            "ret_1b_atr": "",
            "ret_3b_atr": "",
            "ret_6b_atr": "",
            "ret_12b_atr": "",
            "ret_24b_atr": "",
            "mfe_atr": "",
            "mae_atr": "",
            "bars_to_mfe": "",
            "bars_to_mae": "",
            "v1_entry": _fmt_price(v1_entry),
            "v1_sl": _fmt_price(v1_sl),
            "v1_tp": _fmt_price(v1_tp),
            "v1_rr": _fmt_num(v1_rr, 2),
            "v1_result": v1_status,
            "v1_r": "",
            "v1_bars": "",
            "v1_mfe_r": "",
            "v1_mae_r": "",
            "v2_entry": _fmt_price(v2_entry),
            "v2_sl": _fmt_price(v2_sl),
            "v2_tp": _fmt_price(v2_tp),
            "v2_rr": _fmt_num(v2_rr, 2),
            "v2_result": v2_status,
            "v2_r": "",
            "v2_bars": "",
            "v2_mfe_r": "",
            "v2_mae_r": "",
            "v3_has_ob": str(v3_has_ob),
            "v3_entry": _fmt_price(v3_entry),
            "v3_sl": _fmt_price(v3_sl),
            "v3_tp": _fmt_price(v3_tp),
            "v3_rr": _fmt_num(v3_rr, 2),
            "v3_filled": "0",
            "v3_fill_bars": "",
            "v3_result": v3_status,
            "v3_r": "0.000" if v3_status == "NO_OB" else "",
            "v3_bars": "",
            "v3_mfe_r": "",
            "v3_mae_r": "",
            "v4_entry": _fmt_price(v2_entry),
            "v4_sl": _fmt_price(v2_sl),
            "v4_exit_price": "",
            "v4_exit_reason": "PENDING" if v4_status == "PENDING" else "VOID",
            "v4_result": v4_status,
            "v4_r": "",
            "v4_bars": "",
            "v4_mfe_r": "",
            "v4_mae_r": "",
            "user_taken": "",
            "user_notes": "",
        }

        existing.append(row)
        _write_rows_atomic(csv_path, existing)
        debug(f"[choch-csv] logged {symbol} {tf} {direction} CHoCH -> {csv_path.name}")
        return True


def _sim_bracket(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    rr: float,
    horizon_done: bool,
    is_limit: bool = False,
    valid_bars: int = 24,
) -> dict:
    """Simulate a bracket trade over forward bars [0..m-1].
    Conservative tie-break: SL checked before TP on every bar.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return {"result": "VOID", "filled": 0, "fill_bars": "", "r": "", "bars": "", "mfe_r": "", "mae_r": ""}

    m = len(closes)
    filled = not is_limit
    fill_idx = 0 if not is_limit else None
    mfe_r = 0.0
    mae_r = 0.0

    for k in range(m):
        hi = float(highs[k])
        lo = float(lows[k])
        if not filled:
            if k >= valid_bars:
                return {
                    "result": "EXPIRED_UNFILLED",
                    "filled": 0,
                    "fill_bars": "",
                    "r": "0.000",
                    "bars": str(k),
                    "mfe_r": "0.000",
                    "mae_r": "0.000",
                }
            if direction == "bullish" and lo <= entry:
                filled = True
                fill_idx = k
            elif direction == "bearish" and hi >= entry:
                filled = True
                fill_idx = k
            else:
                continue

        bars_in_trade = k - fill_idx + 1
        if direction == "bullish":
            if lo <= sl:
                mae_r = max(mae_r, (entry - lo) / risk)
                return {
                    "result": "LOSS",
                    "filled": 1,
                    "fill_bars": str(fill_idx + 1) if is_limit else "0",
                    "r": "-1.000",
                    "bars": str(bars_in_trade),
                    "mfe_r": _fmt_num(mfe_r, 3),
                    "mae_r": _fmt_num(mae_r, 3),
                }
            if hi >= tp:
                mfe_r = max(mfe_r, (hi - entry) / risk)
                return {
                    "result": "WIN",
                    "filled": 1,
                    "fill_bars": str(fill_idx + 1) if is_limit else "0",
                    "r": _fmt_num(rr, 3),
                    "bars": str(bars_in_trade),
                    "mfe_r": _fmt_num(mfe_r, 3),
                    "mae_r": _fmt_num(mae_r, 3),
                }
            mfe_r = max(mfe_r, (hi - entry) / risk)
            mae_r = max(mae_r, (entry - lo) / risk)
        else:
            if hi >= sl:
                mae_r = max(mae_r, (hi - entry) / risk)
                return {
                    "result": "LOSS",
                    "filled": 1,
                    "fill_bars": str(fill_idx + 1) if is_limit else "0",
                    "r": "-1.000",
                    "bars": str(bars_in_trade),
                    "mfe_r": _fmt_num(mfe_r, 3),
                    "mae_r": _fmt_num(mae_r, 3),
                }
            if lo <= tp:
                mfe_r = max(mfe_r, (entry - lo) / risk)
                return {
                    "result": "WIN",
                    "filled": 1,
                    "fill_bars": str(fill_idx + 1) if is_limit else "0",
                    "r": _fmt_num(rr, 3),
                    "bars": str(bars_in_trade),
                    "mfe_r": _fmt_num(mfe_r, 3),
                    "mae_r": _fmt_num(mae_r, 3),
                }
            mfe_r = max(mfe_r, (entry - lo) / risk)
            mae_r = max(mae_r, (hi - entry) / risk)

    if not filled:
        if horizon_done or m >= valid_bars:
            return {
                "result": "EXPIRED_UNFILLED",
                "filled": 0,
                "fill_bars": "",
                "r": "0.000",
                "bars": str(min(m, valid_bars)),
                "mfe_r": "0.000",
                "mae_r": "0.000",
            }
        return {"result": "PENDING", "filled": 0, "fill_bars": "", "r": "", "bars": "", "mfe_r": "", "mae_r": ""}

    bars_in_trade = m - fill_idx
    if horizon_done:
        last_c = float(closes[-1])
        dir_sign = 1.0 if direction == "bullish" else -1.0
        mtm_r = dir_sign * (last_c - entry) / risk
        return {
            "result": "EXPIRED_OPEN",
            "filled": 1,
            "fill_bars": str(fill_idx + 1) if is_limit else "0",
            "r": _fmt_num(mtm_r, 3),
            "bars": str(bars_in_trade),
            "mfe_r": _fmt_num(mfe_r, 3),
            "mae_r": _fmt_num(mae_r, 3),
        }

    return {
        "result": "PENDING",
        "filled": 1,
        "fill_bars": str(fill_idx + 1) if is_limit else "0",
        "r": "",
        "bars": str(bars_in_trade),
        "mfe_r": _fmt_num(mfe_r, 3),
        "mae_r": _fmt_num(mae_r, 3),
    }


def _sim_structure_trail(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    direction: str,
    entry: float,
    sl: float,
    opposing_choch_rel_idx: Optional[int],
    horizon_done: bool,
) -> dict:
    """Simulate Variant 4: Market entry at CHoCH close, structural SL, exit on
    first opposing CHoCH candle close (or time-stop at horizon_bars).
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return {
            "exit_price": "", "exit_reason": "VOID", "result": "VOID",
            "r": "", "bars": "", "mfe_r": "", "mae_r": "",
        }

    m = len(closes)
    dir_sign = 1.0 if direction == "bullish" else -1.0
    mfe_r = 0.0
    mae_r = 0.0

    for k in range(m):
        hi = float(highs[k])
        lo = float(lows[k])
        cl = float(closes[k])
        if direction == "bullish":
            if lo <= sl:
                mae_r = max(mae_r, (entry - lo) / risk)
                return {
                    "exit_price": _fmt_price(sl),
                    "exit_reason": "SL",
                    "result": "LOSS",
                    "r": "-1.000",
                    "bars": str(k + 1),
                    "mfe_r": _fmt_num(mfe_r, 3),
                    "mae_r": _fmt_num(mae_r, 3),
                }
            mfe_r = max(mfe_r, (hi - entry) / risk)
            mae_r = max(mae_r, (entry - lo) / risk)
        else:
            if hi >= sl:
                mae_r = max(mae_r, (hi - entry) / risk)
                return {
                    "exit_price": _fmt_price(sl),
                    "exit_reason": "SL",
                    "result": "LOSS",
                    "r": "-1.000",
                    "bars": str(k + 1),
                    "mfe_r": _fmt_num(mfe_r, 3),
                    "mae_r": _fmt_num(mae_r, 3),
                }
            mfe_r = max(mfe_r, (entry - lo) / risk)
            mae_r = max(mae_r, (hi - entry) / risk)

        if opposing_choch_rel_idx is not None and k == opposing_choch_rel_idx:
            realized_r = dir_sign * (cl - entry) / risk
            res = "WIN" if realized_r > 0 else ("LOSS" if realized_r < 0 else "BREAKEVEN")
            return {
                "exit_price": _fmt_price(cl),
                "exit_reason": "OPPOSING_CHOCH",
                "result": res,
                "r": _fmt_num(realized_r, 3),
                "bars": str(k + 1),
                "mfe_r": _fmt_num(mfe_r, 3),
                "mae_r": _fmt_num(mae_r, 3),
            }

    if horizon_done and m > 0:
        cl = float(closes[-1])
        realized_r = dir_sign * (cl - entry) / risk
        res = "WIN" if realized_r > 0 else ("LOSS" if realized_r < 0 else "BREAKEVEN")
        return {
            "exit_price": _fmt_price(cl),
            "exit_reason": "TIME_EXIT",
            "result": res,
            "r": _fmt_num(realized_r, 3),
            "bars": str(m),
            "mfe_r": _fmt_num(mfe_r, 3),
            "mae_r": _fmt_num(mae_r, 3),
        }

    return {
        "exit_price": "",
        "exit_reason": "PENDING",
        "result": "PENDING",
        "r": "",
        "bars": str(m),
        "mfe_r": _fmt_num(mfe_r, 3),
        "mae_r": _fmt_num(mae_r, 3),
    }


def resolve_choch_outcomes(store: Store, cfg: dict, verbose: bool = False) -> int:
    """Backfill forward returns, excursions, and all 4 variant results for
    PENDING rows in data/choch_alerts.csv. Preserves user_taken & user_notes.
    Returns number of rows that transitioned to RESOLVED/VOID.
    """
    jcfg = _journal_cfg(cfg)
    if not jcfg.get("enabled", True):
        return 0

    csv_path = resolve_csv_path(cfg)
    horizon = max(1, int(jcfg.get("horizon_bars", 96)))
    ob_valid = max(1, int(jcfg.get("ob_valid_bars", (cfg.get("smc") or {}).get("entry_valid_bars", 24))))
    lookback = int((cfg.get("smc") or {}).get("swing_lookback", 3))

    with _CSV_LOCK:
        rows = _read_rows(csv_path)
        if not rows:
            return 0

        pending_idxs = [i for i, r in enumerate(rows) if r.get("outcome_status") == "PENDING"]
        if not pending_idxs:
            return 0

        by_pair: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for idx in pending_idxs:
            r = rows[idx]
            by_pair[(r["symbol"], r["tf"])].append(idx)

        newly_resolved = 0
        dirty = False

        for (symbol, tf), idxs in by_pair.items():
            df = store.load_candles(symbol, tf)
            if df is None or len(df) < 10:
                continue
            df = df.reset_index(drop=True)
            times = df["time"].values
            highs_all = df["high"].values
            lows_all = df["low"].values
            closes_all = df["close"].values

            # Causal structure events across the loaded history (for Variant 4 opposing CHoCH exit)
            swings = find_swings(df, lookback)
            struct_events, _ = detect_structure(df, swings)
            choch_events = [e for e in struct_events if e.get("type") == "choch"]

            for idx in idxs:
                r = rows[idx]
                try:
                    ev_ts = int(r.get("event_bar_ts") or 0)
                    atr_v = float(r.get("atr") or 0.0)
                    entry = float(r.get("entry_price") or 0.0)
                except ValueError:
                    r["outcome_status"] = "VOID"
                    dirty = True
                    newly_resolved += 1
                    continue

                if ev_ts <= 0 or atr_v <= 0 or entry <= 0:
                    r["outcome_status"] = "VOID"
                    dirty = True
                    newly_resolved += 1
                    continue

                start = int(np.searchsorted(times, ev_ts, side="right"))
                if start == 0 and int(times[0]) > ev_ts:
                    r["outcome_status"] = "VOID"
                    dirty = True
                    newly_resolved += 1
                    continue
                if start >= len(df):
                    continue  # no future closed bars yet

                end = min(len(df), start + horizon)
                m = end - start
                if m <= 0:
                    continue

                horizon_done = m >= horizon
                direction = r.get("direction", "bullish")
                dir_sign = 1.0 if direction == "bullish" else -1.0

                f_highs = highs_all[start:end]
                f_lows = lows_all[start:end]
                f_closes = closes_all[start:end]

                r["bars_observed"] = str(m)
                for b in (1, 3, 6, 12, 24):
                    col = f"ret_{b}b_atr"
                    if m >= b:
                        ret_atr = dir_sign * (float(f_closes[b - 1]) - entry) / atr_v
                        r[col] = _fmt_num(ret_atr, 3)

                # Excursions in ATR
                if direction == "bullish":
                    fav_arr = (f_highs - entry) / atr_v
                    adv_arr = (entry - f_lows) / atr_v
                else:
                    fav_arr = (entry - f_lows) / atr_v
                    adv_arr = (f_highs - entry) / atr_v
                mfe_idx = int(np.argmax(fav_arr))
                mae_idx = int(np.argmax(adv_arr))
                r["mfe_atr"] = _fmt_num(max(0.0, float(fav_arr[mfe_idx])), 3)
                r["mae_atr"] = _fmt_num(max(0.0, float(adv_arr[mae_idx])), 3)
                r["bars_to_mfe"] = str(mfe_idx + 1)
                r["bars_to_mae"] = str(mae_idx + 1)

                # Variant 1: Baseline ATR
                if r.get("v1_sl") and r.get("v1_tp"):
                    v1 = _sim_bracket(
                        f_highs, f_lows, f_closes, direction,
                        float(r["v1_entry"]), float(r["v1_sl"]), float(r["v1_tp"]),
                        float(r.get("v1_rr") or 2.0), horizon_done, is_limit=False,
                    )
                    r["v1_result"] = v1["result"]
                    r["v1_r"] = v1["r"]
                    r["v1_bars"] = v1["bars"]
                    r["v1_mfe_r"] = v1["mfe_r"]
                    r["v1_mae_r"] = v1["mae_r"]

                # Variant 2: Structural Market
                if r.get("v2_sl") and r.get("v2_tp"):
                    v2 = _sim_bracket(
                        f_highs, f_lows, f_closes, direction,
                        float(r["v2_entry"]), float(r["v2_sl"]), float(r["v2_tp"]),
                        float(r.get("v2_rr") or 2.0), horizon_done, is_limit=False,
                    )
                    r["v2_result"] = v2["result"]
                    r["v2_r"] = v2["r"]
                    r["v2_bars"] = v2["bars"]
                    r["v2_mfe_r"] = v2["mfe_r"]
                    r["v2_mae_r"] = v2["mae_r"]

                # Variant 3: OB Pullback Limit
                if r.get("v3_has_ob") == "1" and r.get("v3_entry") and r.get("v3_sl") and r.get("v3_tp"):
                    v3 = _sim_bracket(
                        f_highs, f_lows, f_closes, direction,
                        float(r["v3_entry"]), float(r["v3_sl"]), float(r["v3_tp"]),
                        float(r.get("v3_rr") or 2.0), horizon_done,
                        is_limit=True, valid_bars=ob_valid,
                    )
                    r["v3_filled"] = str(v3["filled"])
                    r["v3_fill_bars"] = v3["fill_bars"]
                    r["v3_result"] = v3["result"]
                    r["v3_r"] = v3["r"]
                    r["v3_bars"] = v3["bars"]
                    r["v3_mfe_r"] = v3["mfe_r"]
                    r["v3_mae_r"] = v3["mae_r"]

                # Variant 4: Structure Trail (exit on opposing CHoCH close)
                if r.get("v4_sl"):
                    opp_dir = "bearish" if direction == "bullish" else "bullish"
                    opp_rel = None
                    for ce in choch_events:
                        c_idx = int(ce["index"])
                        if start <= c_idx < end and ce.get("direction") == opp_dir:
                            opp_rel = c_idx - start
                            break
                    v4 = _sim_structure_trail(
                        f_highs, f_lows, f_closes, direction,
                        float(r["v4_entry"]), float(r["v4_sl"]),
                        opp_rel, horizon_done,
                    )
                    r["v4_exit_price"] = v4["exit_price"]
                    r["v4_exit_reason"] = v4["exit_reason"]
                    r["v4_result"] = v4["result"]
                    r["v4_r"] = v4["r"]
                    r["v4_bars"] = v4["bars"]
                    r["v4_mfe_r"] = v4["mfe_r"]
                    r["v4_mae_r"] = v4["mae_r"]

                if horizon_done:
                    r["outcome_status"] = "RESOLVED"
                    newly_resolved += 1
                dirty = True

        if dirty:
            _write_rows_atomic(csv_path, rows)

    if verbose and newly_resolved:
        print(f"  [choch-csv] resolved {newly_resolved} CHoCH row(s) in {csv_path}")
    return newly_resolved


def summarize_choch_journal(cfg: dict, symbol: str = None, tf: str = None) -> dict:
    """Aggregate end-result statistics across all 4 variants from data/choch_alerts.csv."""
    csv_path = resolve_csv_path(cfg)
    with _CSV_LOCK:
        rows = _read_rows(csv_path)

    if symbol:
        sym_u = symbol.strip().upper()
        rows = [r for r in rows if r.get("symbol", "").upper() == sym_u]
    if tf:
        tf_u = tf.strip().upper()
        rows = [r for r in rows if r.get("tf", "").upper() == tf_u]

    def _variant_stats(prefix: str, is_limit: bool = False) -> dict:
        filled_rows = []
        unfilled = 0
        pending = 0
        for r in rows:
            res = (r.get(f"{prefix}_result") or "").upper()
            if res in ("", "PENDING"):
                pending += 1
                continue
            if res in ("EXPIRED_UNFILLED", "NO_OB", "VOID"):
                unfilled += 1
                continue
            if is_limit and r.get(f"{prefix}_filled") != "1":
                unfilled += 1
                continue
            filled_rows.append(r)

        wins = sum(1 for r in filled_rows if (r.get(f"{prefix}_result") or "").upper() == "WIN")
        losses = sum(1 for r in filled_rows if (r.get(f"{prefix}_result") or "").upper() == "LOSS")
        open_or_be = len(filled_rows) - wins - losses
        wl = wins + losses
        r_vals = []
        for r in filled_rows:
            try:
                if r.get(f"{prefix}_r") != "":
                    r_vals.append(float(r[f"{prefix}_r"]))
            except ValueError:
                pass
        gross_win = sum(x for x in r_vals if x > 0)
        gross_loss = abs(sum(x for x in r_vals if x < 0))
        pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)

        mfe_vals = [float(r[f"{prefix}_mfe_r"]) for r in filled_rows if r.get(f"{prefix}_mfe_r") != ""]
        mae_vals = [float(r[f"{prefix}_mae_r"]) for r in filled_rows if r.get(f"{prefix}_mae_r") != ""]
        bar_vals = [int(r[f"{prefix}_bars"]) for r in filled_rows if r.get(f"{prefix}_bars") != ""]
        eligible = len(filled_rows) + unfilled

        return {
            "signals": len(rows),
            "filled": len(filled_rows),
            "unfilled": unfilled,
            "pending": pending,
            "fill_rate": round(len(filled_rows) / eligible, 3) if eligible else None,
            "wins": wins,
            "losses": losses,
            "other": open_or_be,
            "win_rate": round(wins / wl, 3) if wl else None,
            "expectancy_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
            "total_r": round(sum(r_vals), 2) if r_vals else 0.0,
            "profit_factor": pf,
            "avg_mfe_r": round(sum(mfe_vals) / len(mfe_vals), 2) if mfe_vals else None,
            "avg_mae_r": round(sum(mae_vals) / len(mae_vals), 2) if mae_vals else None,
            "avg_bars": round(sum(bar_vals) / len(bar_vals), 1) if bar_vals else None,
        }

    def _slice_by(field: str) -> dict:
        groups: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            k = r.get(field) or "(blank)"
            groups[k].append(r)
        out = {}
        for k, grp in sorted(groups.items()):
            def _mean_col(col: str) -> Optional[float]:
                vals = []
                for x in grp:
                    if x.get(col) != "":
                        try:
                            vals.append(float(x[col]))
                        except ValueError:
                            pass
                return round(sum(vals) / len(vals), 3) if vals else None

            out[k] = {
                "n": len(grp),
                "v1_exp_r": _mean_col("v1_r"),
                "v2_exp_r": _mean_col("v2_r"),
                "v3_exp_r": _mean_col("v3_r"),
                "v4_exp_r": _mean_col("v4_r"),
                "ret_6b_atr": _mean_col("ret_6b_atr"),
                "mfe_atr": _mean_col("mfe_atr"),
                "mae_atr": _mean_col("mae_atr"),
            }
        return out

    return {
        "csv_path": str(csv_path),
        "total": len(rows),
        "resolved": sum(1 for r in rows if r.get("outcome_status") == "RESOLVED"),
        "pending": sum(1 for r in rows if r.get("outcome_status") == "PENDING"),
        "variants": {
            "v1_baseline_atr": _variant_stats("v1", is_limit=False),
            "v2_structural_mkt": _variant_stats("v2", is_limit=False),
            "v3_ob_pullback_lmt": _variant_stats("v3", is_limit=True),
            "v4_structure_trail": _variant_stats("v4", is_limit=False),
        },
        "by_direction": _slice_by("direction"),
        "by_sweep": _slice_by("has_recent_sweep"),
        "by_htf": _slice_by("htf_trend_aligned"),
        "by_session": _slice_by("session"),
    }
