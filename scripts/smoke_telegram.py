"""Smoke test for Telegram CHoCH & BOS structure alerts (no network needed)."""
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config
from app.data.store import Store
from app.notifications import telegram as tg_mod

# 1. Config defaults & env fallback (test against DEFAULTS via nonexistent config path)
cfg_default = load_config("__nonexistent_config_for_defaults__.yaml")
assert cfg_default["telegram"]["enabled"] is False, cfg_default["telegram"]
assert cfg_default["telegram"]["events"] == ["choch", "bos"]

with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "999:ENVTOK", "TELEGRAM_CHAT_ID": "-100777"}):
    tok, cid = tg_mod.resolve_credentials(cfg_default)
    assert (tok, cid) == ("999:ENVTOK", "-100777"), (tok, cid)

# 2. First-run seeding & deduplication
tmp = tempfile.mkdtemp()
db_path = os.path.join(tmp, "tg_test.db")
store = Store(db_path)

cfg = load_config()
cfg["storage"]["path"] = db_path
cfg["telegram"].update({
    "enabled": True,
    "bot_token": "123456:TESTTOKEN",
    "chat_id": "-1001234567890",
    "events": ["choch", "bos"],
    "timeframes": [],  # should fall back to mtf.entry_tf (M15)
    "max_event_age_bars": 2,
    "quiet_hours": None,
    "csv_journal": {"enabled": False},
})

base_time = 1700000000
ev1 = {
    "type": "bos",
    "direction": "bullish",
    "level": 1.0850,
    "level_time": base_time - 3600,
    "level_index": 280,
    "index": 298,
    "time": base_time,
}

store.save_analysis("EURUSD", "M15", {
    "symbol": "EURUSD",
    "tf": "M15",
    "generated_at": base_time,
    "meta": {"bars": 300},
    "smc": {"trend": "bullish", "last_close": 1.0855, "events": [ev1]},
})
# Non-entry TF (H1) should be ignored when telegram.timeframes is empty
store.save_analysis("EURUSD", "H1", {
    "symbol": "EURUSD",
    "tf": "H1",
    "generated_at": base_time,
    "meta": {"bars": 300},
    "smc": {"trend": "bullish", "last_close": 1.0855, "events": [ev1]},
})

sent_calls = []

def fake_send(bot_token, chat_id, text, timeout=10):
    sent_calls.append((bot_token, chat_id, text))
    return True, "ok"

with patch.object(tg_mod, "send_message", side_effect=fake_send):
    # Pass 1: initial run seeds existing events without sending
    n1 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15"), ("EURUSD", "H1")])
    assert n1 == 0 and len(sent_calls) == 0, (n1, sent_calls)
    assert store.has_telegram_seed("EURUSD", "M15")

    # Pass 2: unchanged analysis sends 0
    n2 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15")])
    assert n2 == 0 and len(sent_calls) == 0

    # Pass 3: new CHoCH event arrives -> 1 alert sent
    ev2 = {
        "type": "choch",
        "direction": "bearish",
        "level": 1.0830,
        "level_time": base_time,
        "level_index": 298,
        "index": 299,
        "time": base_time + 900,
    }
    store.save_analysis("EURUSD", "M15", {
        "symbol": "EURUSD",
        "tf": "M15",
        "generated_at": base_time + 900,
        "meta": {"bars": 301},
        "smc": {"trend": "bearish", "last_close": 1.0825, "events": [ev1, ev2]},
    })
    n3 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15"), ("EURUSD", "H1")])
    assert n3 == 1 and len(sent_calls) == 1, (n3, sent_calls)
    assert "CHoCH" in sent_calls[0][2] and "BEARISH" in sent_calls[0][2], sent_calls[0][2]

    # Pass 4: duplicate check -> 0 sent
    n4 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15")])
    assert n4 == 0 and len(sent_calls) == 1

    # Pass 5: event filter (choch only suppresses bos) + quiet hours
    ev3 = {
        "type": "bos",
        "direction": "bearish",
        "level": 1.0810,
        "level_time": base_time + 900,
        "level_index": 299,
        "index": 300,
        "time": base_time + 1800,
    }
    store.save_analysis("EURUSD", "M15", {
        "symbol": "EURUSD",
        "tf": "M15",
        "generated_at": base_time + 1800,
        "meta": {"bars": 302},
        "smc": {"trend": "bearish", "last_close": 1.0805, "events": [ev1, ev2, ev3]},
    })
    cfg_choch_only = {**cfg, "telegram": {**cfg["telegram"], "events": ["choch"]}}
    n5 = tg_mod.notify_structure_events(store, cfg_choch_only, [("EURUSD", "M15")])
    assert n5 == 0

    # Quiet hours 00-24 mutes all hours
    cfg_quiet = {**cfg, "telegram": {**cfg["telegram"], "quiet_hours": "00-24"}}
    n6 = tg_mod.notify_structure_events(store, cfg_quiet, [("EURUSD", "M15")])
    assert n6 == 0

    # Unmuted with [choch, bos] -> sends ev3
    n7 = tg_mod.notify_structure_events(store, cfg, [("EURUSD", "M15")])
    assert n7 == 1 and len(sent_calls) == 2

    # Pass 6: Multi-bot fan-out & per-bot routing overrides (telegram.bots)
    cfg_multi = {
        **cfg,
        "telegram": {
            **cfg["telegram"],
            "bots": [
                {
                    "name": "htf-choch-only",
                    "bot_token": "222222:HTFTOKEN",
                    "chat_id": "-1002222222",
                    "events": ["choch"],
                    "timeframes": ["H1"],
                    "symbols": ["EURUSD"],
                }
            ],
        },
    }
    # Initial pass seeds the new bot on H1 without sending
    m1 = tg_mod.notify_structure_events(store, cfg_multi, [("EURUSD", "M15"), ("EURUSD", "H1")])
    assert m1 == 0 and len(sent_calls) == 2
    assert store.has_telegram_seed("EURUSD", "H1", target_prefix="tg:222222:-1002222222")

    # New H1 CHoCH arrives: primary (M15-only) skips it, htf-choch-only sends it
    ev_h1_choch = {
        "type": "choch",
        "direction": "bullish",
        "level": 1.0900,
        "level_time": base_time + 1800,
        "level_index": 299,
        "index": 300,
        "time": base_time + 3600,
    }
    store.save_analysis("EURUSD", "H1", {
        "symbol": "EURUSD",
        "tf": "H1",
        "generated_at": base_time + 3600,
        "meta": {"bars": 301},
        "smc": {"trend": "bullish", "last_close": 1.0910, "events": [ev1, ev_h1_choch]},
    })
    m2 = tg_mod.notify_structure_events(store, cfg_multi, [("EURUSD", "M15"), ("EURUSD", "H1")])
    assert m2 == 1 and len(sent_calls) == 3, (m2, sent_calls)
    assert sent_calls[-1][0] == "222222:HTFTOKEN" and sent_calls[-1][1] == "-1002222222"

store.close()
print("ALL TELEGRAM SMOKE TESTS PASSED")
