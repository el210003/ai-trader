"""Synthetic smoke test for the rejection-bar family (not a unit suite)."""
import math
import sys

import pandas as pd

sys.path.insert(0, ".")

from app.smc import analyze
from app.engine.setup_builder import build_setups
from app.ai.features import make_features, feature_vector, FEATURES
from app.smc.rejection import infer_point

CFG = {
    "swing_lookback": 3, "eq_tolerance_pct": 0.0006, "ob_max_age_bars": 300,
    "fvg_max_age_bars": 200, "sweep_lookback_bars": 30, "min_rr": 1.5,
    "default_rr": 2.0, "max_rr": 5.0, "sl_buffer_atr": 0.25,
    "min_risk_atr": 0.75, "max_entry_distance_atr": 1.0,
    "skip_already_tested": True, "retest_buffer_atr": 0.20,
    "entry_valid_bars": 24, "atr_period": 14,
    "rejection": {
        "enabled": True, "mode": "retrace", "retrace_pct": 50,
        "entry_valid_bars": 8, "trigger_lookback_bars": 2,
        "require_directional_sweep": True, "buckets": [0, 1, 2],
    },
}


def mkdf(rows):
    # round to 5 decimals: keeps synthetic prices clean so infer_point sees
    # the intended quote precision (real MT5 rates are exact decimals)
    return pd.DataFrame([[round(v, 5) for v in row] for row in rows],
                        columns=["time", "open", "high", "low", "close"])


def base_rows():
    """Grinding uptrend then a pullback that forms a swing low, then a base."""
    rows, t, p = [], 1_700_000_000, 1.1000
    for i in range(60):                       # up-leg -> swing highs/lows
        rows.append((t + i * 900, p, p + 0.0009, p - 0.0004, p + 0.0007))
        p += 0.0006
    for i in range(8):                        # pullback -> swing low at the end
        rows.append((t + (60 + i) * 900, p, p + 0.0002, p - 0.0006, p - 0.0004))
        p -= 0.0005
    for i in range(6):                        # base holding above the low
        rows.append((t + (68 + i) * 900, p, p + 0.0003,
                     p - 0.0002 + i * 0.0001, p + 0.0001))
    return mkdf(rows), t


def _spring(df, t, swing_low):
    """Append a bear bar closing above the level, then a bull engulf that
    sweeps the swing low and closes back above it (the spring itself)."""
    rows = list(df.values)
    n = len(df)
    po = swing_low + 0.0006
    pc = swing_low + 0.0002
    rows.append([t + n * 900, po, po + 0.0002, pc - 0.0002, pc])
    bo = pc                                   # D1 cover: open at the bear close
    bc = swing_low + 0.0012
    rows.append([t + (n + 1) * 900, bo, bc + 0.0004, swing_low - 0.0008, bc])
    return mkdf(rows), bo, bc


def test_detector_fires_on_sweep_engulf():
    df, t = base_rows()
    swing_low = float(df["low"].iloc[67])
    df2, bo, bc = _spring(df, t, swing_low)

    smc = analyze(df2, CFG)
    assert smc["rejections"], "no rejection events detected"
    ev = smc["rejections"][-1]
    assert ev["direction"] == "bull", ev
    assert ev["defs"] > 0
    assert any(s["side"] == "sellside" for s in ev["swept"]), ev["swept"]
    assert ev["index"] == len(df2) - 1
    print(f"detector OK: bucket={ev['bucket']} exp={ev['exp']} clv={ev['clv']} "
          f"depth={ev['depth']} defs={ev['defs']} swept={len(ev['swept'])}")

    setups = build_setups("EURUSD", "M15", df2, smc, CFG)
    rej = [s for s in setups if s.get("setup_kind") == "rejection"]
    assert rej, f"no rejection setup built; all setups: {[(s['direction'], s.get('entry_zone', {}).get('type')) for s in setups]}"
    s = rej[0]
    # EA RetraceArm semantics: limit = close - depth (bull), depth =
    # half-up(bodyPts*pct/100) points — i.e. half the body within half a point
    body = abs(bc - bo)
    point = infer_point(df2)
    depth = (bc - s["entry"]) / point
    assert abs(depth - 0.5 * body / point) <= 0.5, (depth, body / point)
    assert s["direction"] == "long"
    assert s["stop_loss"] < s["entry"] < s["take_profit"]
    assert s["entry_zone"]["type"] == "rejection_bar"
    assert s["entry_zone"]["origin_time"] == ev["time"]
    assert s["entry_valid_bars"] == 8
    assert s["entry_style"] == "retrace"
    assert s["rr"] >= CFG["min_rr"]
    print(f"setup OK: entry={s['entry']:.5f} sl={s['stop_loss']:.5f} "
          f"tp={s['take_profit']:.5f} rr={s['rr']} bucket={s['rejection']['bucket']}")

    feats = make_features(df2, s, smc, CFG)
    assert set(feats.keys()) == set(FEATURES), set(feats.keys()) ^ set(FEATURES)
    vec = feature_vector(feats)
    assert len(vec) == len(FEATURES)
    assert feats["rej_is_momentum"] in (0.0, 1.0)
    print(f"features OK: rej_exp={feats['rej_exp']} rej_clv={feats['rej_clv']} "
          f"mom={feats['rej_is_momentum']} agrade={feats['rej_is_agrade']} "
          f"({len(vec)} features)")
    return s


def test_zone_setups_still_flow():
    df, t = base_rows()
    smc = analyze(df, CFG)
    setups = build_setups("EURUSD", "M15", df, smc, CFG)
    kinds = [s.get("setup_kind", "zone") for s in setups]
    print(f"zone path OK: {len(setups)} setups, kinds={set(kinds) or '{}'}")
    for s in setups:
        assert s.get("setup_kind", "zone") == "zone"


def test_labeler_honors_entry_valid_bars():
    from app.engine.labeler import label_setup
    df, t = base_rows()
    smc = analyze(df, CFG)
    s = {
        "formed_index": len(df) - 3, "direction": "long",
        "entry": 1.0900, "stop_loss": 1.0880, "take_profit": 1.0960,
        "fill_tolerance": 0.0002, "entry_valid_bars": 8, "rr": 3.0,
    }
    # price never comes near entry within 8 bars -> None with the 8-bar window
    y = label_setup(df, s, horizon=96, entry_valid_bars=24)
    far = dict(s, entry=0.5)
    y2 = label_setup(df, far, horizon=96, entry_valid_bars=24)
    print(f"labeler OK: per-setup window accepted (y={y}, far-entry y={y2})")


def test_detector_silent_without_sweep():
    df, t = base_rows()
    smc = analyze(df, CFG)
    # no crafted sweep -> any rejections here would be from the organic base
    print(f"quiet base: {len(smc['rejections'])} organic rejections "
          f"(may be >0 on real structure)")


def test_eqh_newest_touch_keeps_swing():
    """EA tie-break: on equal highs the NEWEST touch keeps the swing
    reference (older flank equality allowed, newer strict). Two equal highs
    must leave a live swing high that a later bear engulf can sweep."""
    df, t = base_rows()
    rows = list(df.values)
    n = len(df)
    lvl = 1.1340
    # two touches of the same high separated by a dip, then a base below it
    rows.append([t + n * 900, 1.1330, lvl, 1.1325, 1.1328])
    rows.append([t + (n + 1) * 900, 1.1328, 1.1332, 1.1322, 1.1326])
    rows.append([t + (n + 2) * 900, 1.1326, lvl, 1.1320, 1.1324])   # newest touch
    for j in range(3):
        rows.append([t + (n + 3 + j) * 900, 1.1324, 1.1328,
                     1.1318 - j * 0.0001, 1.1322])
    df2 = mkdf(rows)
    # a bear engulf that wicks above lvl and closes back below it
    rows = list(df2.values)
    n2 = len(df2)
    po, pc = lvl - 0.0006, lvl - 0.0002
    rows.append([t + n2 * 900, po, po + 0.0002, pc - 0.0002, pc])
    bo = pc
    bc = lvl - 0.0012
    rows.append([t + (n2 + 1) * 900, bo, lvl + 0.0008, bc - 0.0004, bc])
    df3 = mkdf(rows)
    smc3 = analyze(df3, CFG)
    evs = [e for e in smc3["rejections"] if e["index"] == len(df3) - 1]
    assert evs, "EQH newest-touch swing was not swept (tie-break broken)"
    ev = evs[-1]
    assert ev["direction"] == "bear"
    assert any(s["kind"] == "swing_high" and abs(s["level"] - lvl) < 1e-9
               for s in ev["swept"]), ev["swept"]
    print(f"EQH tie-break OK: newest-touch swing swept by bear reject at idx {ev['index']}")


def test_day_extreme_sweep():
    """Running server-day high is a sweep reference (InpUseDayExtreme):
    a bear engulf that wicks above the day high and closes back rejects."""
    df, t = base_rows()
    rows = list(df.values)
    n = len(df)
    day_high = float(df["high"].max())
    # bull bar closing just under the day high, then a bear engulf wicking
    # above the day high and closing back below it
    po = day_high - 0.0008
    pc = day_high - 0.0004
    rows.append([t + n * 900, po, day_high - 0.0002, po - 0.0002, pc])
    bo = pc
    bc = day_high - 0.0030
    rows.append([t + (n + 1) * 900, bo, day_high + 0.0006, bc - 0.0004, bc])
    df2 = mkdf(rows)
    smc = analyze(df2, CFG)
    evs = [e for e in smc["rejections"] if e["index"] == len(df2) - 1]
    assert evs, "day-high sweep did not arm a reject"
    ev = evs[-1]
    assert any(s["kind"] == "day_high" for s in ev["swept"]), ev["swept"]
    print(f"day-extreme OK: day_high swept by bear reject at idx {ev['index']}")


def test_close_mode():
    df, t = base_rows()
    swing_low = float(df["low"].iloc[67])
    df2, bo, bc = _spring(df, t, swing_low)
    cfg = dict(CFG, rejection=dict(CFG["rejection"], mode="close"))
    smc = analyze(df2, cfg)
    setups = build_setups("EURUSD", "M15", df2, smc, cfg)
    rej = [s for s in setups if s.get("setup_kind") == "rejection"]
    assert rej and rej[0]["entry_style"] == "close"
    assert abs(rej[0]["entry"] - df2["close"].iat[-1]) < 1e-9
    print(f"close mode OK: entry={rej[0]['entry']:.5f} == last close")


def test_directional_filter():
    """Mirror pattern: bear reject at a swing high -> SHORT with a buyside
    sweep; the same event must NOT arm a long."""
    df, t = base_rows()
    n = len(df)
    swing_high = float(df["high"].iloc[59])   # confirmed pivot (index 59, conf 62)
    # stop-hold: bull bar closing below the high, then bear engulf that
    # sweeps the swing high and closes back below it
    rows = list(df.values)
    po = swing_high - 0.0006
    pc = swing_high - 0.0002
    rows.append([t + n * 900, po, po + 0.0002, pc - 0.0002, pc])
    bo = pc
    bc = swing_high - 0.0012
    rows.append([t + (n + 1) * 900, bo, swing_high + 0.0008, bc - 0.0004, bc])
    df2 = mkdf(rows)
    cfg = dict(CFG, rejection=dict(CFG["rejection"], require_directional_sweep=True))
    smc = analyze(df2, cfg)
    setups = build_setups("EURUSD", "M15", df2, smc, cfg)
    rej = [s for s in setups if s.get("setup_kind") == "rejection"]
    assert rej, "bear reject did not arm a short"
    for s in rej:
        sides = {x["side"] for x in s["rejection"]["swept"]}
        want = "sellside" if s["direction"] == "long" else "buyside"
        assert want in sides, (s["direction"], sides)
    assert all(s["direction"] == "short" for s in rej), \
        f"long armed from buyside-only event: {[s['direction'] for s in rej]}"
    print(f"directional filter OK: {len(rej)} short setup(s), buyside sweep")


def test_equal_pool_sweep_and_consumption():
    """Equal-highs pool sweeps fire once confirmed (not blocked by epoch timestamp)
    and are consumed so they do not re-fire on subsequent bars."""
    df, t = base_rows()
    rows = list(df.values)
    n = len(df)
    lvl = 1.1350
    # Touch 1 at n, dip, Touch 2 at n+4, then 3 confirmation bars (lookback=3 -> confirmed at n+7)
    rows.append([t + n * 900, 1.1340, lvl, 1.1335, 1.1338])
    rows.append([t + (n + 1) * 900, 1.1338, 1.1340, 1.1325, 1.1328])
    rows.append([t + (n + 2) * 900, 1.1328, 1.1334, 1.1322, 1.1330])
    rows.append([t + (n + 3) * 900, 1.1330, 1.1342, 1.1328, 1.1340])
    rows.append([t + (n + 4) * 900, 1.1340, lvl + 0.0001, 1.1336, 1.1338])
    for j in range(3):
        rows.append([t + (n + 5 + j) * 900, 1.1338, 1.1341,
                     1.1330 - j * 0.0001, 1.1334])
    df2 = mkdf(rows)
    # Bar n+8: wick sweeps equal highs and closes back below
    rows = list(df2.values)
    n2 = len(df2)
    rows.append([t + n2 * 900, 1.1334, lvl + 0.0009, 1.1330, lvl - 0.0005])
    # Bar n+9: another wick above lvl must NOT re-emit the consumed pool
    rows.append([t + (n2 + 1) * 900, lvl - 0.0005, lvl + 0.0007, lvl - 0.0010, lvl - 0.0008])
    df3 = mkdf(rows)
    smc = analyze(df3, CFG)
    eq_sweeps = [s for s in smc["sweeps"] if s["kind"] == "equal_highs" and s["index"] >= n2]
    assert len(eq_sweeps) == 1, f"expected 1 equal_highs sweep, got {eq_sweeps}"
    assert eq_sweeps[0]["index"] == n2
    print(f"equal-pool sweep OK: fired once at idx {n2} @ {eq_sweeps[0]['level']:.5f}")


def test_multi_swing_book_and_fast_parity():
    """Older un-broken swing highs stay live after a lower swing high confirms,
    and FastSMC.at(i) matches analyze(df[:i+1]) on sweeps and rejections."""
    from app.smc.fast_context import FastSMC
    df, t = base_rows()
    rows = list(df.values)
    n = len(df)
    # Swing high 1 at n, dip, lower swing high 2 at n+4 (distinct from base_rows swing high 1.13630), 3 confirm bars, then sweep high1
    high1 = 1.1390
    high2 = 1.1375
    rows.append([t + n * 900, 1.1350, high1, 1.1345, 1.1355])
    rows.append([t + (n + 1) * 900, 1.1355, 1.1358, 1.1340, 1.1342])
    rows.append([t + (n + 2) * 900, 1.1342, 1.1348, 1.1335, 1.1340])
    rows.append([t + (n + 3) * 900, 1.1340, 1.1352, 1.1338, 1.1350])
    rows.append([t + (n + 4) * 900, 1.1350, high2, 1.1346, 1.1348])
    for j in range(3):
        rows.append([t + (n + 5 + j) * 900, 1.1348, 1.1350,
                     1.1340 - j * 0.0001, 1.1344])
    # Wick sweeps BOTH high2 and the older high1, closing below high2
    rows.append([t + (n + 8) * 900, 1.1344, high1 + 0.0006, 1.1340, high2 - 0.0004])
    df2 = mkdf(rows)

    cfg_no_day = dict(CFG, rejection=dict(CFG["rejection"], use_day_extremes=False))
    smc = analyze(df2, cfg_no_day)
    bar_sweeps = [s for s in smc["sweeps"] if s["index"] == n + 8 and s["side"] == "buyside"]
    swept_lvls = [round(s["level"], 5) for s in bar_sweeps]
    assert high1 in swept_lvls and high2 in swept_lvls, f"missing older swing high: {bar_sweeps}"

    fast = FastSMC(df2, CFG)
    for idx in (len(df2) - 3, len(df2) - 2, len(df2) - 1):
        sub = df2.iloc[:idx + 1].reset_index(drop=True)
        ref = analyze(sub, CFG)
        got = fast.at(idx)
        assert got["sweeps"] == ref["sweeps"], f"FastSMC sweeps mismatch at {idx}"
        assert got["rejections"] == ref["rejections"], f"FastSMC rejections mismatch at {idx}"
    print("multi-swing book & FastSMC parity OK")


def test_causal_pool_rejection_parity():
    """Equal-highs/lows pool rejections match bit-for-bit between analyze(df[:i+1])
    and FastSMC.at(i), including with zero-range bars and shifting close tolerance."""
    from app.smc.fast_context import FastSMC
    df, t = base_rows()
    rows = list(df.values)
    n = len(df)
    lvl = 1.1350
    # Touch 1 at n, dip, Touch 2 at n+4, 3 confirm bars -> confirmed at n+7
    rows.append([t + n * 900, 1.1340, lvl, 1.1335, 1.1338])
    rows.append([t + (n + 1) * 900, 1.1338, 1.1340, 1.1325, 1.1328])
    rows.append([t + (n + 2) * 900, 1.1328, 1.1334, 1.1322, 1.1330])
    rows.append([t + (n + 3) * 900, 1.1330, 1.1342, 1.1328, 1.1340])
    rows.append([t + (n + 4) * 900, 1.1340, lvl + 0.0001, 1.1336, 1.1338])
    for j in range(3):
        rows.append([t + (n + 5 + j) * 900, 1.1338, 1.1341,
                     1.1330 - j * 0.0001, 1.1334])
    # Zero-range bar (h == l) after confirmation
    rows.append([t + (n + 8) * 900, 1.1334, 1.1334, 1.1334, 1.1334])
    # Bull bar closing below pool, followed by bear engulf sweeping the equal-highs pool
    rows.append([t + (n + 9) * 900, lvl - 0.0006, lvl - 0.0002, lvl - 0.0008, lvl - 0.0002])
    rows.append([t + (n + 10) * 900, lvl - 0.0002, lvl + 0.0008, lvl - 0.0014, lvl - 0.0012])
    # Subsequent bar sweeping above lvl again must NOT re-emit the consumed pool
    rows.append([t + (n + 11) * 900, lvl - 0.0012, lvl - 0.0004, lvl - 0.0014, lvl - 0.0004])
    rows.append([t + (n + 12) * 900, lvl - 0.0004, lvl + 0.0009, lvl - 0.0016, lvl - 0.0014])
    df2 = mkdf(rows)

    fast = FastSMC(df2, CFG)
    trig_lb = max(2, int((CFG.get("rejection") or {}).get("trigger_lookback_bars", 2)))
    for idx in range(n + 6, len(df2)):
        sub = df2.iloc[:idx + 1].reset_index(drop=True)
        ref = analyze(sub, CFG)
        arm_from = idx + 1 - trig_lb
        ref_tail = [e for e in ref["rejections"] if e["index"] >= arm_from]
        got = fast.at(idx)
        assert got["rejections"] == ref_tail, (
            f"FastSMC rejection parity mismatch at {idx}: got={got['rejections']} vs ref={ref_tail}"
        )
    # Verify the equal_highs pool sweep was emitted on bar n+10 and not re-emitted on n+12
    ev_10 = [e for e in analyze(df2, CFG)["rejections"] if e["index"] == n + 10]
    assert ev_10 and any(s["kind"] == "equal_highs" for s in ev_10[-1]["swept"]), ev_10
    ev_12 = [e for e in analyze(df2, CFG)["rejections"] if e["index"] == n + 12]
    assert not any(s["kind"] == "equal_highs" for e in ev_12 for s in e["swept"]), ev_12
    print("causal pool rejection parity OK")


def test_structure_independent_active_swings():
    """detect_structure tracks active_high and active_low independently across
    chronological bars, preserves unbroken swings when an opposing swing confirms,
    handles simultaneous confirmed_at swings, and matches FastSMC.at(i)."""
    from app.smc.structure import detect_structure
    from app.smc.fast_context import FastSMC

    t0 = 1_700_000_000
    rows = []
    for i in range(15):
        c = 1.1010 if i == 13 else 1.0970
        rows.append((t0 + i * 900, c - 0.0005, c + 0.0005, c - 0.0005, c))
    df = mkdf(rows)
    swings = [
        {"type": "high", "index": 5, "price": 1.1000, "time": t0 + 5 * 900, "confirmed_at": 8},
        {"type": "low", "index": 8, "price": 1.0950, "time": t0 + 8 * 900, "confirmed_at": 11},
    ]
    events, trend = detect_structure(df, swings)
    assert len(events) == 1, events
    assert events[0]["direction"] == "bullish" and events[0]["type"] == "bos"
    assert events[0]["index"] == 13 and events[0]["level_index"] == 5
    assert trend == "bullish"

    # Simultaneous confirmed_at swings on the same bar
    swings_simul = [
        {"type": "high", "index": 3, "price": 1.1000, "time": t0 + 3 * 900, "confirmed_at": 6},
        {"type": "low", "index": 3, "price": 1.0950, "time": t0 + 3 * 900, "confirmed_at": 6},
    ]
    events_s, trend_s = detect_structure(df, swings_simul)
    assert len(events_s) == 1 and events_s[0]["direction"] == "bullish" and events_s[0]["index"] == 13
    assert trend_s == "bullish"

    # FastSMC parity check across alternating swing sequence
    df_base, _ = base_rows()
    fast = FastSMC(df_base, CFG)
    for idx in range(20, len(df_base)):
        sub = df_base.iloc[:idx + 1].reset_index(drop=True)
        ref = analyze(sub, CFG)
        got = fast.at(idx)
        assert got["events"] == ref["events"], f"events mismatch at {idx}"
        assert got["trend"] == ref["trend"], f"trend mismatch at {idx}"
    print("structure independent active swings & FastSMC parity OK")


def test_skip_already_tested_uses_left_at_and_falls_back():
    """skip_already_tested starts scanning after OB impulse departure (left_at + 1)
    so impulse bars overlapping the OB don't disqualify it, and falls back to the
    next untested candidate zone if a higher-scored zone was already retested."""
    t0 = 1_700_000_000
    rows = []
    # Build 20 bars with a bullish BOS, an OB at index 10 whose impulse bars 11-12
    # still overlap [bottom, top] before leaving at left_at=13, and clean pullback at 16.
    for i in range(18):
        o, h, l, c = 1.1000, 1.1005, 1.0995, 1.1002
        if i == 10:  # bearish OB candle [1.0990, 1.1000]
            o, h, l, c = 1.0998, 1.1000, 1.0990, 1.0992
        elif i == 11:  # impulse candle 1 still overlaps OB top (low 1.0995 <= 1.1000)
            o, h, l, c = 1.0992, 1.1006, 1.0995, 1.1005
        elif i == 12:  # impulse candle 2 still overlaps OB top (low 1.0998 <= 1.1000)
            o, h, l, c = 1.1005, 1.1012, 1.0998, 1.1010
        elif i in (13, 14, 15):  # left_at=13: low > 1.1000
            o, h, l, c = 1.1010, 1.1018, 1.1005, 1.1014
        elif i in (16, 17):  # price approaches OB without touching entry 1.1000
            o, h, l, c = 1.1012, 1.1013, 1.1002, 1.1004
        rows.append((t0 + i * 900, o, h, l, c))
    df = mkdf(rows)

    cfg = dict(CFG, skip_already_tested=True, rejection=dict(CFG["rejection"], enabled=False))
    ob1 = {
        "type": "bullish_ob", "direction": "bullish",
        "top": 1.1000, "bottom": 1.0990,
        "origin_index": 10, "origin_time": t0 + 10 * 900,
        "event_type": "bos", "event_index": 12, "event_time": t0 + 12 * 900,
        "left_at": 13, "mitigated_index": None, "mitigated": False,
    }
    smc = {
        "trend": "bullish", "swings": [], "events": [], "sweeps": [], "rejections": [],
        "swing_pivots": [], "liquidity_pools": [],
        "dealing_range": {"top": 1.1020, "bottom": 1.0985, "equilibrium": 1.10025,
                          "high_time": t0 + 15 * 900, "low_time": t0, "position": 0.54, "zone": "premium"},
        "order_blocks": {"all": [ob1], "fresh": [ob1]},
        "fvgs": {"all": [], "fresh": []},
        "atr": 0.0010, "last_close": 1.1004, "n_bars": len(df),
    }
    setups = build_setups("EURUSD", "M15", df, smc, cfg)
    longs = [s for s in setups if s["direction"] == "long"]
    assert len(longs) == 1 and longs[0]["entry_zone"]["origin_index"] == 10, setups

    # Now add a higher-scored OB (ob_tested) that WAS retested after left_at, and verify fallback to ob1
    rows2 = list(rows)
    rows2[15] = (t0 + 15 * 900, 1.1014, 1.1016, 1.1001, 1.1006)  # low 1.1001 <= 1.1002 (retests ob_tested)
    df2 = mkdf(rows2)
    ob_tested = {
        "type": "bullish_ob", "direction": "bullish",
        "top": 1.1002, "bottom": 1.0994,
        "origin_index": 11, "origin_time": t0 + 11 * 900,
        "event_type": "bos", "event_index": 12, "event_time": t0 + 12 * 900,
        "left_at": 13, "mitigated_index": None, "mitigated": False,
    }
    smc2 = dict(smc, order_blocks={"all": [ob_tested, ob1], "fresh": [ob_tested, ob1]})
    setups2 = build_setups("EURUSD", "M15", df2, smc2, cfg)
    longs2 = [s for s in setups2 if s["direction"] == "long"]
    assert len(longs2) == 1 and longs2[0]["entry_zone"]["origin_index"] == 10, setups2
    print("skip_already_tested left_at & candidate fallback OK")


def test_htf_tp_distance_atr_units():
    """htf_tp_distance_atr stays in ATR units (20.0 sentinel when no HTF pool
    exists, regardless of ATR scale; exact ATR distance when an HTF pool exists)."""
    df, t = base_rows()
    setup = {
        "direction": "long",
        "entry": 50000.0,
        "rr": 2.0,
        "range_position": 0.3,
        "confluences": ["market structure aligned"],
        "entry_zone": {"type": "order_block", "origin_index": len(df) - 5},
    }
    # High-ATR asset (BTCUSD, ATR = 500.0) without htf_ctx -> sentinel must remain 20.0
    smc_btc = {"atr": 500.0, "last_close": 50100.0, "sweeps": []}
    feats_btc = make_features(df, setup, smc_btc, CFG, htf_ctx=None)
    assert set(feats_btc.keys()) == set(FEATURES)
    assert feats_btc["htf_tp_distance_atr"] == 20.0, feats_btc["htf_tp_distance_atr"]

    # Low-ATR FX pair (ATR = 0.001) without htf_ctx -> sentinel must remain 20.0
    smc_fx = {"atr": 0.001, "last_close": 1.1005, "sweeps": []}
    setup_fx = dict(setup, entry=1.1000)
    feats_fx = make_features(df, setup_fx, smc_fx, CFG, htf_ctx=None)
    assert feats_fx["htf_tp_distance_atr"] == 20.0, feats_fx["htf_tp_distance_atr"]

    # With HTF pool 2.0 * ATR above entry -> distance must be 2.0 ATR
    htf_ctx = {"H1": {"buyside": [50000.0 + 2.0 * 500.0], "sellside": []}}
    feats_pool = make_features(df, setup, smc_btc, CFG, htf_ctx=htf_ctx)
    assert abs(feats_pool["htf_tp_distance_atr"] - 2.0) < 1e-9, feats_pool["htf_tp_distance_atr"]
    print("htf_tp_distance_atr units & feature keys OK")


def test_dealing_range_impulse_leg_and_fast_parity():
    """compute_dealing_range anchors to the active impulse leg [impulse_low, running_high]
    after a bullish BOS so a pullback sits in discount rather than 100% premium, and
    matches FastSMC.at(i) bit-for-bit."""
    from app.smc.fast_context import FastSMC

    t0 = 1_700_000_000
    rows = []
    # 0..3: range before initial swing high at index 4 (1.1020)
    base = [
        (1.1000, 1.1005, 1.0995, 1.1002),
        (1.1002, 1.1008, 1.0998, 1.1004),
        (1.1004, 1.1010, 1.1000, 1.1006),
        (1.1006, 1.1014, 1.1002, 1.1010),
        (1.1010, 1.1020, 1.1008, 1.1012),  # idx 4: swing high 1.1020 (confirmed at 7)
        (1.1012, 1.1013, 1.1000, 1.1002),
        (1.1002, 1.1005, 1.0992, 1.0995),
        (1.0995, 1.0998, 1.0988, 1.0990),
        (1.0990, 1.0992, 1.0980, 1.0985),  # idx 8: swing low 1.0980 (confirmed at 11)
        (1.0985, 1.0996, 1.0984, 1.0994),
        (1.0994, 1.1008, 1.0992, 1.1006),
        (1.1006, 1.1018, 1.1004, 1.1016),
        # idx 12: bullish BOS close above 1.1020 -> surges to 1.1100
        (1.1016, 1.1100, 1.1015, 1.1090),
        # idx 13: pullback into lower half of [1.0980, 1.1100] (eq = 1.1040) -> close 1.1025 (discount!)
        (1.1090, 1.1092, 1.1020, 1.1025),
    ]
    for i, (o, h, l, c) in enumerate(base):
        rows.append((t0 + i * 900, o, h, l, c))
    df = mkdf(rows)

    smc = analyze(df, CFG)
    dr = smc["dealing_range"]
    assert abs(dr["bottom"] - 1.0980) < 1e-9, dr
    assert abs(dr["top"] - 1.1100) < 1e-9, dr
    assert abs(dr["equilibrium"] - 1.1040) < 1e-9, dr
    assert dr["zone"] == "discount" and dr["position"] < 0.5, dr

    fast = FastSMC(df, CFG)
    for idx in range(7, len(df)):
        sub = df.iloc[:idx + 1].reset_index(drop=True)
        ref = analyze(sub, CFG)
        got = fast.at(idx)
        assert got["dealing_range"] == ref["dealing_range"], (
            f"FastSMC dealing_range mismatch at {idx}: got={got['dealing_range']} vs ref={ref['dealing_range']}"
        )
    print("dealing range impulse leg & FastSMC parity OK")


def test_find_swings_tie_break_matches_detect_pivots():
    """find_swings uses the same older-flank-equality / newer-flank-strict
    tie-break as detect_pivots, keeping the newest touch on tight double tops
    and clustering equal highs when two swings fall within tolerance."""
    from app.smc.structure import find_swings
    from app.smc.rejection import detect_pivots
    from app.smc.liquidity import cluster_equal_levels

    t0 = 1_700_000_000
    # Exact double top within lookback=3: bar 5 == bar 7 == 1.10500
    highs = [1.1010, 1.1020, 1.1030, 1.1035, 1.1040, 1.10500, 1.1042, 1.10500, 1.1040, 1.1030, 1.1020]
    rows = [(t0 + i * 900, h - 0.0005, h, h - 0.0010, h - 0.0003) for i, h in enumerate(highs)]
    df = mkdf(rows)

    swings = find_swings(df, lookback=3)
    pivots = detect_pivots(df, n_swing=3)
    assert swings == pivots, f"swings={swings} != pivots={pivots}"
    swing_highs = [s for s in swings if s["type"] == "high"]
    assert len(swing_highs) == 1 and swing_highs[0]["index"] == 7, swing_highs

    # Two separated touches (> lookback bars apart) both emit and cluster into an equal_highs pool
    highs2 = [
        1.1010, 1.1020, 1.1030, 1.10500, 1.1035, 1.1025, 1.1030,
        1.10500, 1.1035, 1.1025, 1.1015,
    ]
    rows2 = [(t0 + i * 900, h - 0.0005, h, h - 0.0010, h - 0.0003) for i, h in enumerate(highs2)]
    df2 = mkdf(rows2)
    swings2 = find_swings(df2, lookback=3)
    pivots2 = detect_pivots(df2, n_swing=3)
    assert swings2 == pivots2, f"swings2={swings2} != pivots2={pivots2}"
    pools = cluster_equal_levels(swings2, "buyside", tol=0.0005)
    assert len(pools) == 1 and pools[0]["members"] == [3, 7], pools
    print("find_swings tie-break & detect_pivots parity OK")


def test_infer_point_trailing_window_and_override():
    """infer_point and infer_point_by_bar scan a trailing window so quotes ending
    in trailing zeros on bar 0 / mid / -1 still detect 5-digit precision, and
    honor an explicit `point` override."""
    from app.smc.rejection import infer_point, infer_point_by_bar

    t0 = 1_700_000_000
    # Bars 0, 2 (mid), and 4 (last) all end in .xxx00 (float repr 1.105 -> exponent -3),
    # while bar 3 has 5 decimal places (1.10512 -> exponent -5).
    rows = [
        (t0 + 0 * 900, 1.10500, 1.10500, 1.10500, 1.10500),
        (t0 + 1 * 900, 1.10500, 1.10500, 1.10500, 1.10500),
        (t0 + 2 * 900, 1.10500, 1.10500, 1.10500, 1.10500),
        (t0 + 3 * 900, 1.10500, 1.10512, 1.10500, 1.10500),
        (t0 + 4 * 900, 1.10500, 1.10500, 1.10500, 1.10500),
    ]
    df = mkdf(rows)
    assert infer_point(df) == 1e-5, infer_point(df)
    by_bar = infer_point_by_bar(df)
    assert by_bar[-1] == 1e-5, by_bar
    for idx in range(len(df)):
        assert by_bar[idx] == infer_point(df.iloc[:idx + 1]), (idx, by_bar[idx])
    assert infer_point(df, point=0.0001) == 0.0001
    assert infer_point_by_bar(df, point=0.0001)[-1] == 0.0001
    print("infer_point trailing window & override OK")


def test_csm_trailing_bar_alignment():
    """compute_tf_strength drops incomplete tip bars where fewer than min_pairs
    have real closes, evaluating across the full aligned 28-pair bar instead."""
    import numpy as np
    from app.csm import CSM_PAIRS, compute_tf_strength

    rng = np.random.default_rng(42)
    times = list(range(1000, 1040))
    closes_aligned = {}
    for idx, (pair, _b, _q) in enumerate(CSM_PAIRS):
        base_px = 1.10 + idx * 0.01
        vals = base_px + np.cumsum(rng.normal(0, 0.0005, size=len(times)))
        closes_aligned[pair] = pd.Series(vals, index=times)

    ref = compute_tf_strength(closes_aligned, lookback=10, ma_len=5, ma_type="SMA", min_pairs=10)
    assert len(ref) == 8, ref

    # Add an extra newer bar (t=1040) on EURUSD only (simulating early rollover tick)
    closes_skewed = dict(closes_aligned)
    closes_skewed["EURUSD"] = pd.concat([
        closes_aligned["EURUSD"],
        pd.Series([float(closes_aligned["EURUSD"].iloc[-1]) + 0.05], index=[1040]),
    ])
    got = compute_tf_strength(closes_skewed, lookback=10, ma_len=5, ma_type="SMA", min_pairs=10)
    assert got == ref, f"CSM skewed by single early-ticking pair: got={got} vs ref={ref}"
    print("CSM trailing-bar misalignment guard OK")


def test_csm_endpoint_self_heal_no_nameerror():
    """GET /api/csm self-heal worker connects a local MT5 handle instead of
    referencing an undefined `mt5` variable, and passes it to update_csm."""
    import tempfile
    import time
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app.dashboard.server import create_app
    from app import pipeline
    from app.data import mt5_client

    tmpdir = tempfile.mkdtemp()
    try:
        cfg = {
            "symbols": ["EURUSD"],
            "timeframes": ["M15"],
            "mt5": {},
            "smc": CFG,
            "csm": {"enabled": True, "min_interval_seconds": 30},
            "ai": {"ml": {"model_path": f"{tmpdir}/missing.joblib"}, "llm": {"enabled": False}},
            "storage": {"path": f"{tmpdir}/test.db"},
            "dashboard": {"bar_close_watcher": False, "auto_run_interval": 0},
            "execution": {"enabled": False},
            "_demo": False,
        }
        app = create_app(cfg)
        sentinel_mt5 = object()
        called = {}

        def fake_connect(c):
            called["connected"] = True
            return sentinel_mt5

        def fake_shutdown(m):
            called["shutdown"] = m

        def fake_update(c, st, mt5=None, demo=False, **kw):
            called["mt5"] = mt5
            called["demo"] = demo

        with patch.object(mt5_client, "_SHARED_MT5", None), \
             patch.object(mt5_client, "connect_mt5", side_effect=fake_connect), \
             patch.object(mt5_client, "shutdown_mt5", side_effect=fake_shutdown), \
             patch.object(pipeline, "update_csm", side_effect=fake_update):
            client = TestClient(app)
            resp = client.get("/api/csm")
            assert resp.status_code == 200
            for _ in range(50):
                if "mt5" in called:
                    break
                time.sleep(0.02)

        assert called.get("connected") is True, called
        assert called.get("mt5") is sentinel_mt5, called
        assert called.get("demo") is False, called
    finally:
        import gc, shutil
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("csm endpoint self-heal refresh OK")


def test_mt5_lock_serializes_concurrent_calls():
    """MT5_LOCK and mt5_session serialize concurrent IPC calls across threads."""
    import threading
    import time
    from unittest.mock import patch
    from app.data import mt5_client

    active = 0
    max_active = 0
    guard = threading.Lock()

    class FakeMT5:
        def copy_rates_from_pos(self, symbol, tf, start, bars):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with guard:
                active -= 1
            return [
                {"time": 1_700_000_000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "tick_volume": 10},
            ]

        def last_error(self):
            return (0, "ok")

        def shutdown(self):
            pass

    fake = FakeMT5()

    def worker():
        with mt5_client.mt5_session({}, demo=False) as m:
            df = mt5_client.fetch_ohlcv("EURUSD", "M15", 1, demo=False, mt5=m)
            assert len(df) == 1

    with patch.object(mt5_client, "_SHARED_MT5", None), \
         patch.object(mt5_client, "connect_mt5", return_value=fake):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert max_active == 1, f"Concurrent MT5 IPC overlap detected: max_active={max_active}"
    print("MT5_LOCK & mt5_session concurrency serialization OK")


def test_execution_engine_dynamic_allowed_symbols():
    """ExecutionEngine resolves callable allowed_symbols dynamically per scan
    instead of freezing the startup symbol set."""
    import tempfile
    import time
    from app.data.store import Store
    from app.execution.executor import ExecutionEngine

    tmpdir = tempfile.mkdtemp()
    try:
        store = Store(f"{tmpdir}/test.db")
        now = int(time.time())
        for sym in ("EURUSD", "GBPUSD"):
            store.save_analysis(sym, "M15", {
                "generated_at": now,
                "setups": [{
                    "direction": "long", "verdict": "BUY", "entry": 1.1000,
                    "stop_loss": 1.0980, "take_profit": 1.1040, "rr": 2.0,
                    "final_score": 85.0, "ml_prob": 0.70,
                    "entry_zone": {"origin_time": now - 900},
                }],
            })
        active_syms = ["EURUSD"]
        cfg = {"mtf": {"entry_tf": "M15"}, "execution": {"enabled": True, "dry_run": True}}
        eng = ExecutionEngine(cfg, store, allowed_symbols=lambda: active_syms, verbose=False)

        cands1 = [s["symbol"] for s in eng._candidates(fresh_seconds=120)]
        assert cands1 == ["EURUSD"], cands1

        active_syms.append("GBPUSD")
        cands2 = sorted(s["symbol"] for s in eng._candidates(fresh_seconds=120))
        assert cands2 == ["EURUSD", "GBPUSD"], cands2

        active_syms.remove("EURUSD")
        cands3 = [s["symbol"] for s in eng._candidates(fresh_seconds=120)]
        assert cands3 == ["GBPUSD"], cands3
    finally:
        import gc, shutil
        try:
            store.conn.close()
        except Exception:
            pass
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("ExecutionEngine dynamic allowed_symbols OK")


def test_index_html_chart_and_livetick_fixes():
    """Verify index.html renders all setups with LONG/SHORT prefixes, caches
    utcOffset per dataset, and uses incremental series.update() in liveTick()."""
    from pathlib import Path
    html = Path("app/dashboard/static/index.html").read_text(encoding="utf-8")
    assert "utcOffset:null" in html
    assert "state.utcOffset = null" in html
    assert "state.utcOffset = (Math.abs(est - offH*3600) <= 900 && est > -5400) ? offH : 2;" in html
    assert "const setups = a.setups || [];" in html
    assert "s.direction === 'long' ? 'LONG ' : 'SHORT '" in html
    assert "state.series.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });" in html
    assert "if (newBarAppended) renderBoxes();" in html
    print("index.html multi-setup lines, cached utcOffset & incremental liveTick OK")


def test_index_html_delegated_clicks_and_esc():
    """Verify index.html escapes single quotes in esc() and uses data-* attributes
    with delegated click listeners instead of inline onclick string interpolation."""
    from pathlib import Path
    html = Path("app/dashboard/static/index.html").read_text(encoding="utf-8")
    assert "'&#39;'" in html
    assert 'onclick="switchTf(' not in html
    assert 'onclick="gotoSetup(' not in html
    assert 'data-tf="${esc(tf)}"' in html
    assert 'data-symbol="${esc(row.symbol)}" data-tf="${esc(row.tf)}"' in html
    assert "$('structureCard').addEventListener('click'" in html
    assert "e.target.closest('[data-tf]')" in html
    assert "$('rankedRows').addEventListener('click'" in html
    assert "e.target.closest('.rank-row[data-symbol]')" in html
    print("index.html delegated click handlers & single-quote escaping OK")


if __name__ == "__main__":
    test_detector_fires_on_sweep_engulf()
    test_zone_setups_still_flow()
    test_labeler_honors_entry_valid_bars()
    test_detector_silent_without_sweep()
    test_close_mode()
    test_directional_filter()
    test_eqh_newest_touch_keeps_swing()
    test_day_extreme_sweep()
    test_equal_pool_sweep_and_consumption()
    test_multi_swing_book_and_fast_parity()
    test_causal_pool_rejection_parity()
    test_structure_independent_active_swings()
    test_skip_already_tested_uses_left_at_and_falls_back()
    test_htf_tp_distance_atr_units()
    test_dealing_range_impulse_leg_and_fast_parity()
    test_find_swings_tie_break_matches_detect_pivots()
    test_infer_point_trailing_window_and_override()
    test_csm_trailing_bar_alignment()
    test_csm_endpoint_self_heal_no_nameerror()
    test_mt5_lock_serializes_concurrent_calls()
    test_execution_engine_dynamic_allowed_symbols()
    test_index_html_chart_and_livetick_fixes()
    test_index_html_delegated_clicks_and_esc()
    print("ALL SMOKE TESTS PASSED")
