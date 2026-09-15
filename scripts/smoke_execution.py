"""Smoke test for the execution engine with a stubbed MT5 module (no terminal needed)."""
import os, sys, tempfile, time, types, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AI_TRADER_TEST", "1")

from app.config import load_config
from app.data.store import Store
from app.execution.executor import ExecutionEngine, trade_identity

tmp = tempfile.mkdtemp()
cfg = load_config()
cfg["storage"]["path"] = os.path.join(tmp, "test.db")
cfg["execution"].update({"enabled": True, "dry_run": True, "min_score": 70,
                         "min_ml_prob": 0.5, "fresh_seconds": 120})

store = Store(cfg["storage"]["path"])

# --- fake analysis with a qualifying BUY setup
setup = {
    "symbol": "EURUSD", "tf": "M15", "direction": "long", "verdict": "BUY",
    "final_score": 82.0, "ml_prob": 0.66, "rr": 2.1,
    "entry": 1.0840, "stop_loss": 1.0820, "take_profit": 1.0882,
    "entry_zone": {"type": "order_block", "top": 1.0845, "bottom": 1.0830, "origin_time": 1700000000},
    "confluences": ["market structure aligned", "fresh order block"],
    "formed_at": int(time.time()),
}
analysis = {"symbol": "EURUSD", "tf": "M15", "generated_at": int(time.time()),
            "setups": [setup], "smc": {}, "meta": {}}
store.save_analysis("EURUSD", "M15", analysis)
# context-only TF must be ignored
store.save_analysis("EURUSD", "H1", {"symbol": "EURUSD", "tf": "H1",
                                     "generated_at": int(time.time()), "setups": [dict(setup, tf="H1")]})

# --- stub mt5 module
class Info:
    digits = 5; point = 0.00001; spread = 12; trade_mode = 4; filling_mode = 1
    volume_min = 0.01; volume_max = 100.0; volume_step = 0.01
    trade_tick_value = 1.0; trade_tick_size = 0.00001; trade_contract_size = 100000.0
    currency_profit = "USD"; visible = True
class Tick:
    ask = 1.0841; bid = 1.0839; time = int(time.time())
class Acc:
    login = 123; server = "test"; currency = "USD"; balance = 10000.0
    equity = 10000.0; margin_free = 10000.0; leverage = 100; trade_allowed = True
fake_mt5 = types.SimpleNamespace(
    symbol_info=lambda s: Info(), symbol_select=lambda s, v: True,
    symbol_info_tick=lambda s: Tick(), account_info=lambda: Acc(),
    positions_get=lambda symbol=None: [], orders_get=lambda symbol=None: [],
    order_calc_margin=lambda *a: 50.0,
    order_send=lambda req: types.SimpleNamespace(retcode=10009, comment="ok",
                                                 order=777, deal=888, volume=req["volume"], price=req["price"]),
    shutdown=lambda: None,
)

engine = ExecutionEngine(cfg, store, allowed_symbols={"EURUSD"}, verbose=False)
engine._connect = lambda: fake_mt5          # stub the connection

# 1) dry-run pass -> one dry_run trade
s1 = engine.scan_once()
assert s1["considered"] == 1, s1
assert len(s1["actions"]) == 1 and s1["actions"][0]["status"] == "dry_run", s1
t = store.load_trades(limit=5)
assert len(t) == 1 and t[0]["status"] == "dry_run" and t[0]["lot"] is not None
print("pass 1 (dry run):", s1["actions"][0]["symbol"], "lot", s1["actions"][0]["lot"],
      "identity ok:", t[0]["identity"] == trade_identity("EURUSD", "M15", "long", setup))

# 2) dedup: same setup identity -> skipped
s2 = engine.scan_once()
assert len(s2["actions"]) == 0, s2
assert s2["skipped"].get("setup already executed") == 1, s2
print("pass 2 (dedup):", s2["skipped"])

# 3) new identity but inside cooldown -> skipped
setup2 = dict(setup); setup2["entry_zone"] = dict(setup["entry_zone"], origin_time=1700000100)
store.save_analysis("EURUSD", "M15", {**analysis, "setups": [setup2]})
s3 = engine.scan_once()
assert s3["skipped"].get("cooldown (240 min)") == 1, s3
print("pass 3 (cooldown):", s3["skipped"])

# 4) low score setup -> skipped by gate
setup3 = dict(setup, final_score=55.0, entry_zone=dict(setup["entry_zone"], origin_time=1700000200))
store.save_analysis("EURUSD", "M15", {**analysis, "setups": [setup3]})
s4 = engine.scan_once()
assert any("score" in k for k in s4["skipped"]), s4
print("pass 4 (score gate):", s4["skipped"])

# 5) live mode (dry_run off) -> filled
store2 = Store(os.path.join(tmp, "t2.db"))
cfg2 = dict(cfg); cfg2["storage"]["path"] = os.path.join(tmp, "t2.db")
cfg2["execution"] = dict(cfg["execution"], dry_run=False, cooldown_minutes=0)
store2.save_analysis("EURUSD", "M15", analysis)
engine2 = ExecutionEngine(cfg2, store2, allowed_symbols={"EURUSD"}, verbose=False)
engine2._connect = lambda: fake_mt5
s5 = engine2.scan_once()
assert s5["actions"] and s5["actions"][0]["status"] == "filled", s5
a = s5["actions"][0]
assert a["ticket"] == 777 and a["price"] == 1.0841, a
rows = store2.load_trades()
assert rows[0]["status"] == "filled" and rows[0]["ticket"] == 777
print("pass 5 (live fill):", a["status"], "ticket", a["ticket"], "price", a["price"], "lot", a["lot"])

# 6) limit entry type -> placed
cfg3 = dict(cfg2); cfg3["execution"] = dict(cfg2["execution"], entry_type="limit", cooldown_minutes=0)
store3 = Store(os.path.join(tmp, "t3.db"))
store3.save_analysis("EURUSD", "M15", analysis)
engine3 = ExecutionEngine(cfg3, store3, allowed_symbols={"EURUSD"}, verbose=False)
engine3._connect = lambda: fake_mt5
s6 = engine3.scan_once()
assert s6["actions"][0]["status"] == "placed", s6
print("pass 6 (limit):", s6["actions"][0]["status"], "at", s6["actions"][0]["price"])

# 7) verdict/direction mismatch gate (valid short geometry, but verdict says BUY)
setup4 = dict(setup, direction="short", verdict="BUY",
              stop_loss=1.0860, take_profit=1.0798,
              entry_zone=dict(setup["entry_zone"], origin_time=1700000300))
store2.save_analysis("EURUSD", "M15", {**analysis, "setups": [setup4]})
s7 = engine2.scan_once()
assert any("mismatch" in k for k in s7["skipped"]), s7
print("pass 7 (mismatch gate):", s7["skipped"])

# 8) short setup end-to-end (geometry tp < entry < sl)
setup5 = dict(setup, direction="short", verdict="SELL", final_score=78.0,
              entry=1.0840, stop_loss=1.0860, take_profit=1.0798,
              entry_zone=dict(setup["entry_zone"], origin_time=1700000400))
store2.save_analysis("EURUSD", "M15", {**analysis, "setups": [setup5]})
s8 = engine2.scan_once()
assert s8["actions"] and s8["actions"][0]["status"] == "filled", s8
assert s8["actions"][0]["price"] == 1.0839   # short fills at bid
print("pass 8 (short live):", s8["actions"][0]["status"], "price", s8["actions"][0]["price"])

print("\nALL SMOKE TESTS PASSED")
