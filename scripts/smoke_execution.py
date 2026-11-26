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
cfg["_root"] = tmp   # isolate runtime overrides (data/execution_overrides.json)
cfg["execution"].update({"enabled": True, "dry_run": True, "min_score": 70,
                         "min_ml_prob": 0.5, "fresh_seconds": 120,
                         "trading_hours": None})

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

# 5) live mode (dry_run off, explicit market) -> filled
store2 = Store(os.path.join(tmp, "t2.db"))
cfg2 = dict(cfg); cfg2["storage"]["path"] = os.path.join(tmp, "t2.db")
cfg2["execution"] = dict(cfg["execution"], entry_type="market", dry_run=False, cooldown_minutes=0)
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

# 9) runtime-adjustable parameters: gates + order identity (persisted)
cfg9 = dict(cfg); cfg9["execution"] = dict(cfg["execution"], enabled=False)
eng9 = ExecutionEngine(cfg9, store2, allowed_symbols={"EURUSD"}, verbose=False)
g9 = eng9.set_gates(min_score=55.0, min_ml_prob=0.6)
assert g9 == {"min_score": 55.0, "min_ml_prob": 0.6}, g9
ident = eng9.set_identity(magic=424242, comment="test-bot")
assert ident == {"magic": 424242, "comment": "test-bot"}, ident
assert eng9.magic == 424242
try:
    eng9.set_identity(magic=0)
    raise AssertionError("magic=0 was accepted")
except ValueError:
    pass
ident2 = eng9.set_identity(comment="x" * 50)
assert ident2["comment"] == "x" * 31, ident2          # MT5 comment display limit
ov = json.load(open(os.path.join(tmp, "data", "execution_overrides.json")))
assert ov["min_score"] == 55.0 and ov["magic"] == 424242 \
       and ov["comment"] == "x" * 31, ov
eng9b = ExecutionEngine(cfg9, store2, allowed_symbols={"EURUSD"}, verbose=False)
assert eng9b.magic == 424242, eng9b.magic
assert eng9b.gates() == {"min_score": 55.0, "min_ml_prob": 0.6}, eng9b.gates()
print("pass 9 (runtime params):", g9, ident)

# 10) Phase A verification: A1 journal persistence, A2 age decoupling, A3 routing, A4 drift guard, A5 gate defaults
def_cfg = load_config()
assert def_cfg["smc"]["retest_buffer_atr"] == 0.0
assert def_cfg["smc"]["ob_setup_max_age_bars"] == 150
assert def_cfg["smc"]["fvg_setup_max_age_bars"] == 48
assert def_cfg["smc"]["entry_valid_bars"] == 24
assert def_cfg["execution"]["entry_type"] == "limit"
assert def_cfg["execution"]["min_score"] == 44
assert def_cfg["execution"]["min_ml_prob"] == 0.41

clean_root = os.path.join(tmp, "clean_root")
def_eng = ExecutionEngine({"storage": {"path": os.path.join(tmp, "t_def.db")}, "_root": clean_root},
                          store2, verbose=False)
assert def_eng.gates() == {"min_score": 44.0, "min_ml_prob": 0.41}, def_eng.gates()
assert def_eng.status()["min_score"] == 44.0 and def_eng.status()["min_ml_prob"] == 0.41
assert def_eng.status()["entry_type"] == "limit"

# A1: Store.append_setups persists setup_kind, entry_style, entry_valid_bars, rejection
store_a = Store(os.path.join(tmp, "ta.db"))
rej_setup = {
    "symbol": "EURUSD", "tf": "M15", "direction": "long", "verdict": "BUY",
    "setup_kind": "rejection", "entry_style": "retrace", "entry_valid_bars": 8,
    "final_score": 80.0, "ml_prob": 0.65, "rr": 2.0,
    "entry": 1.0840, "stop_loss": 1.0820, "take_profit": 1.0880,
    "fill_tolerance": 0.0,
    "entry_zone": {"type": "rejection_bar", "top": 1.0845, "bottom": 1.0835, "origin_time": 1700000500},
    "rejection": {"bucket": 1, "exp": 3.2, "clv": 85.0, "depth": 20.0, "defs": 2, "mode": "retrace", "swept": []},
    "formed_at": int(time.time()),
}
zone_setup = dict(setup, setup_kind="zone", entry_style="limit", entry_valid_bars=24,
                  fill_tolerance=0.0,
                  entry_zone=dict(setup["entry_zone"], origin_time=1700000600))
store_a.append_setups("EURUSD", "M15", [rej_setup, zone_setup], int(time.time()))
hist = store_a.load_setups_history("EURUSD", "M15", limit=5)
by_kind = {json.loads(r["payload"])["setup_kind"]: json.loads(r["payload"]) for r in hist}
assert by_kind["rejection"]["entry_style"] == "retrace" and by_kind["rejection"]["entry_valid_bars"] == 8
assert by_kind["rejection"]["rejection"]["bucket"] == 1
assert by_kind["zone"]["entry_style"] == "limit" and by_kind["zone"]["entry_valid_bars"] == 24

# Verify outcomes.py honors per-family entry_valid_bars (8 for rejection vs 24 for zone)
import pandas as pd
from app.engine.outcomes import resolve_pending
t0_bar = int(rej_setup["formed_at"])
bar_rows = []
for bi in range(40):
    bt = t0_bar + bi * 900
    if bi == 12:
        bar_rows.append((bt, 1.0850, 1.0890, 1.0838, 1.0885, 100.0))
    else:
        bar_rows.append((bt, 1.0855, 1.0865, 1.0850, 1.0858, 100.0))
store_a.upsert_candles(pd.DataFrame(bar_rows, columns=["time", "open", "high", "low", "close", "volume"]), "EURUSD", "M15")
assert resolve_pending(store_a, def_cfg) == 2
out_rows = {json.loads(r["payload"])["setup_kind"]: r for r in store_a.load_setups_history("EURUSD", "M15", limit=5)}
assert out_rows["rejection"]["result"] == "EXPIRED_UNFILLED" and out_rows["rejection"]["bars_to_outcome"] == 8, out_rows["rejection"]
assert out_rows["zone"]["result"] == "WIN" and out_rows["zone"]["filled"] == 1, out_rows["zone"]

# A3: Dry-run routing for zone (limit), rejection retrace (limit), rejection close (market)
now_ts = int(time.time())
close_setup = dict(rej_setup, entry_style="close", entry=1.0841,
                   entry_zone=dict(rej_setup["entry_zone"], origin_time=1700000700))
store_a.save_analysis("EURUSD", "M15", {
    "symbol": "EURUSD", "tf": "M15", "generated_at": now_ts,
    "setups": [zone_setup, rej_setup, close_setup],
})
cfg_a = dict(def_cfg)
cfg_a["storage"] = {"path": os.path.join(tmp, "ta.db")}
cfg_a["_root"] = clean_root
cfg_a["execution"] = dict(def_cfg["execution"], enabled=True, dry_run=True,
                          cooldown_minutes=0, max_open_positions=10, max_per_symbol=10,
                          max_trades_per_day=10, trading_hours=None)
eng_a = ExecutionEngine(cfg_a, store_a, allowed_symbols={"EURUSD"}, verbose=False)
eng_a._connect = lambda: fake_mt5
sa = eng_a.scan_once()
assert len(sa["actions"]) == 3, sa
order_types = [a["order_type"] for a in sa["actions"]]
assert order_types == ["limit", "limit", "market"], order_types

# A4: Drift guard skips market fallback when eff_rr < min_rr or SL/TP breached
drift_bad_rr = dict(zone_setup, entry=1.0842, stop_loss=1.0820, take_profit=1.0870,
                    entry_zone=dict(zone_setup["entry_zone"], origin_time=1700000800))
drift_breached = dict(zone_setup, entry=1.0842, stop_loss=1.08415, take_profit=1.0880,
                      entry_zone=dict(zone_setup["entry_zone"], origin_time=1700000900))
store_a.save_analysis("EURUSD", "M15", {
    "symbol": "EURUSD", "tf": "M15", "generated_at": now_ts,
    "setups": [drift_bad_rr, drift_breached],
})
sa_drift = eng_a.scan_once()
assert len(sa_drift["actions"]) == 0, sa_drift
assert any("drift eff_rr" in k for k in sa_drift["skipped"]), sa_drift["skipped"]
assert any("drift breached" in k for k in sa_drift["skipped"]), sa_drift["skipped"]
print("pass 10 (Phase A A1-A5):", order_types, sa_drift["skipped"])

print("\nALL SMOKE TESTS PASSED")
