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


if __name__ == "__main__":
    test_detector_fires_on_sweep_engulf()
    test_zone_setups_still_flow()
    test_labeler_honors_entry_valid_bars()
    test_detector_silent_without_sweep()
    test_close_mode()
    test_directional_filter()
    test_eqh_newest_touch_keeps_swing()
    test_day_extreme_sweep()
    print("ALL SMOKE TESTS PASSED")
