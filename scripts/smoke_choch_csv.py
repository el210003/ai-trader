"""Smoke test for CHoCH CSV journal & 4-variant edge simulator (no network needed)."""
import csv
import os
import sys
import tempfile
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config
from app.data.store import Store
from app.notifications import telegram as tg_mod
from app.notifications.choch_journal import (
    CSV_COLUMNS,
    resolve_choch_outcomes,
    summarize_choch_journal,
)

tmp = tempfile.mkdtemp()
db_path = os.path.join(tmp, "choch_test.db")
csv_path = os.path.join(tmp, "choch_alerts.csv")
store = Store(db_path)

cfg = load_config()
cfg["storage"]["path"] = db_path
cfg["telegram"].update({
    "enabled": True,
    "bot_token": "123456:TESTTOKEN",
    "chat_id": "-1001234567890",
    "events": ["choch", "bos"],
    "timeframes": ["M15"],
    "max_event_age_bars": 2,
    "quiet_hours": None,
    "csv_journal": {
        "enabled": True,
        "path": csv_path,
        "events": ["choch"],
        "horizon_bars": 24,
        "sl_atr": 1.0,
        "tp_atr": 2.0,
        "ob_valid_bars": 24,
    },
})

# Build 300 historical M15 candles ending at base_time
base_time = 1700000000
rows = []
price = 1.0840
for i in range(300):
    t = base_time - (299 - i) * 900
    # Make bar 296 a bearish candle (OB origin before bullish impulse)
    if i == 296:
        o, h, l, c = 1.0846, 1.0848, 1.0840, 1.0841
    elif i == 299:
        # Bullish CHoCH breakout candle closing at 1.0860 above swing high 1.0852
        o, h, l, c = 1.0845, 1.0862, 1.0844, 1.0860
    else:
        o = price
        h = o + 0.0004
        l = o - 0.0004
        c = o + 0.0001
        price = c
    rows.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 100.0})

df_hist = pd.DataFrame(rows)
store.upsert_candles(df_hist, "EURUSD", "M15")

bos_ev = {
    "type": "bos",
    "direction": "bearish",
    "level": 1.0830,
    "level_time": base_time - 7200,
    "level_index": 285,
    "index": 295,
    "time": base_time - 4 * 900,
}
choch_ev = {
    "type": "choch",
    "direction": "bullish",
    "level": 1.0852,
    "level_time": base_time - 3600,
    "level_index": 290,
    "index": 299,
    "time": base_time,
}

# Seed initial pair state with BOS only
store.save_analysis("EURUSD", "M15", {
    "symbol": "EURUSD",
    "tf": "M15",
    "generated_at": base_time - 900,
    "meta": {"bars": 299},
    "smc": {"trend": "bearish", "last_close": 1.0845, "atr": 0.0010, "events": [bos_ev]},
})

with patch.object(tg_mod, "send_message", return_value=(True, "ok")):
    # Pass 1: seeds BOS silently -> 0 CSV rows
    n1 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15")])
    assert n1 == 0
    assert not os.path.exists(csv_path)

    # Pass 2: new bullish CHoCH arrives -> logged to CSV once
    store.save_analysis("EURUSD", "M15", {
        "symbol": "EURUSD",
        "tf": "M15",
        "generated_at": base_time,
        "meta": {"bars": 300},
        "smc": {
            "trend": "bullish",
            "last_close": 1.0860,
            "atr": 0.0010,
            "events": [bos_ev, choch_ev],
            "sweeps": [{"side": "sellside", "kind": "equal_lows", "level": 1.0835, "index": 297}],
            "liquidity_pools": [{"side": "buyside", "price": 1.0885, "touches": 2, "last_index": 280}],
            "swings": [],
            "dealing_range": {"top": 1.0862, "bottom": 1.0835, "equilibrium": 1.08485, "position": 0.926, "zone": "premium"},
            "order_blocks": {
                "all": [{
                    "type": "bullish_ob", "direction": "bullish",
                    "top": 1.0848, "bottom": 1.0840,
                    "origin_index": 296, "origin_time": base_time - 3 * 900,
                    "event_type": "choch", "event_index": 299, "event_time": base_time,
                    "left_at": 299, "mitigated_index": None, "mitigated": False,
                }],
                "fresh": [{
                    "type": "bullish_ob", "direction": "bullish",
                    "top": 1.0848, "bottom": 1.0840,
                    "origin_index": 296, "origin_time": base_time - 3 * 900,
                    "event_type": "choch", "event_index": 299, "event_time": base_time,
                    "left_at": 299, "mitigated_index": None, "mitigated": False,
                }],
            },
            "fvgs": {"all": [], "fresh": []},
        },
        "htf": {"H1": {"trend": "bullish"}, "H4": {"trend": "bullish"}},
        "setups": [{"direction": "long", "verdict": "BUY", "ml_prob": 0.612}],
    })

    n2 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15")])
    assert n2 == 1, n2
    assert os.path.isfile(csv_path)

    # Pass 3: duplicate call -> no duplicate CSV row
    n3 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15")])
    assert n3 == 0

with open(csv_path, "r", encoding="utf-8", newline="") as f:
    reader = list(csv.DictReader(f))
    assert len(reader) == 1, len(reader)
    assert list(reader[0].keys()) == CSV_COLUMNS
    row = reader[0]
    assert row["outcome_status"] == "PENDING"
    assert row["v3_has_ob"] == "1"
    assert row["has_recent_sweep"] == "1"
    assert row["htf_trend_aligned"] == "aligned"
    assert row["setup_verdict"] == "BUY"

# Add manual notes and 24 future bars:
# Bar +1 pulls back into OB (low=1.0846 <= 1.0848, above SL), then rallies past 1.0890 TP!
for r in reader:
    r["user_taken"] = "1"
    r["user_notes"] = "manual note test"
with open(csv_path, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
    w.writeheader()
    w.writerows(reader)

future = []
for k in range(24):
    t = base_time + (k + 1) * 900
    if k == 0:
        # Pullback fills Variant 3 OB limit at 1.0848 without hitting SL (1.0836)
        o, h, l, c = 1.0860, 1.0861, 1.0846, 1.0852
    elif k == 3:
        # Rally hits V1 TP (1.0880), V2 TP (1.0885), V3 TP (1.0885)
        o, h, l, c = 1.0865, 1.0892, 1.0864, 1.0890
    else:
        o, h, l, c = 1.0890, 1.0895, 1.0885, 1.0892
    future.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": 120.0})

store.upsert_candles(pd.DataFrame(future), "EURUSD", "M15")
resolved_n = resolve_choch_outcomes(store, cfg, verbose=True)
assert resolved_n == 1, resolved_n

with open(csv_path, "r", encoding="utf-8", newline="") as f:
    final_rows = list(csv.DictReader(f))
    fr = final_rows[0]
    assert fr["outcome_status"] == "RESOLVED", fr["outcome_status"]
    assert fr["bars_observed"] == "24"
    # Bar +1 dipped to 1.0846:
    # - V1 (1.0 ATR SL=1.0850) -> LOSS (-1.0R)
    # - V2 (wide structural SL=1.0831, fallback 2R TP=1.0918) -> EXPIRED_OPEN (+1.103R)
    # - V3 (OB limit=1.0848, tight SL=1.0836, liquidity pool TP=1.0885 @ 3.08R) -> WIN (+3.08R)
    # - V4 (structure trail to bar 24 close=1.0892) -> WIN (+1.103R)
    assert fr["v1_result"] == "LOSS", fr["v1_result"]
    assert fr["v2_result"] == "EXPIRED_OPEN" and float(fr["v2_r"]) > 1.0, (fr["v2_result"], fr["v2_r"])
    assert fr["v3_filled"] == "1" and fr["v3_result"] == "WIN", (fr["v3_filled"], fr["v3_result"])
    assert fr["v4_result"] == "WIN" and float(fr["v4_r"]) > 1.0, (fr["v4_result"], fr["v4_r"])
    assert fr["user_taken"] == "1" and fr["user_notes"] == "manual note test"

summary = summarize_choch_journal(cfg)
assert summary["resolved"] == 1
assert summary["variants"]["v1_baseline_atr"]["losses"] == 1
assert summary["variants"]["v2_structural_mkt"]["other"] == 1
assert summary["variants"]["v3_ob_pullback_lmt"]["wins"] == 1
assert summary["variants"]["v4_structure_trail"]["wins"] == 1

store.close()
print("ALL CHOCH CSV JOURNAL SMOKE TESTS PASSED")
