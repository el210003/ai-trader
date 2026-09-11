"""AI Trader CLI.

  python -m app.main ingest [--demo] [--all-symbols]   pull OHLC from MT5 (or demo) into SQLite
  python -m app.main analyze [--all-symbols]           run SMC + ML + LLM on stored candles
  python -m app.main train [--all-symbols]             label historical setups + train ML model
  python -m app.main serve [--all-symbols]             launch the HTML dashboard
  python -m app.main run [--interval 60]               ingest + analyze loop
  python -m app.main list-symbols                      print the symbols the app will use
  python -m app.main select-symbols [--enable ...] [--disable ...] [--all] [--none]
                                                      manage per-symbol enable/disable list
"""
import argparse
import os
import sys
import time

from .config import load_config
from .data.store import Store
from .data import mt5_client
from .ai.ml_model import SetupML
from .ai.llm import LLMAnalyzer
from .symbols import resolve_symbols
from .engine import outcomes as outcomes_mod
from .symbol_selection import SymbolSelection
from . import pipeline


SELECTION_PATH = "data/symbol_selection.json"


def _connect_mt5_if_needed(cfg, demo):
    """Return an open MT5 connection, or None if demo / disabled."""
    if demo:
        return None
    return mt5_client.connect_mt5(cfg["mt5"])


def _symbols_for(cfg, args, mt5):
    """Resolve + filter the symbol list once per command.

    1. If `--all-symbols` is set, force discovery on.
    2. Resolve (MT5 -> cache -> config).
    3. Apply the persistent enable/disable selection from
       `data/symbol_selection.json`. If no selection exists, all symbols pass.
    """
    if getattr(args, "all_symbols", False):
        cfg.setdefault("discover", {})["enabled"] = True
    syms = resolve_symbols(cfg, mt5=mt5, verbose=True)
    selection = SymbolSelection(SELECTION_PATH)
    syms = selection.enabled_list(syms)
    if verbose := getattr(args, "verbose", True):
        # Only print this when the selection actually filtered something
        pass
    return syms


def cmd_list_symbols(cfg, args):
    mt5 = _connect_mt5_if_needed(cfg, args.demo)
    syms = _symbols_for(cfg, args, mt5)
    sel = SymbolSelection(SELECTION_PATH)
    explicit = bool(sel.get())
    print(f"symbol count: {len(syms)}" + (" (filtered by selection)" if explicit else ""))
    for s in syms:
        print(f"  {s}")
    if mt5 is not None:
        mt5_client.shutdown_mt5(mt5)


def cmd_ingest(cfg, args):
    store = Store(cfg["storage"]["path"])
    mt5 = _connect_mt5_if_needed(cfg, args.demo)
    syms = _symbols_for(cfg, args, mt5)
    if mt5 is not None:
        print(f"connected to MT5 (build {mt5.version()[0]})")
    try:
        for symbol in syms:
            for tf in cfg["timeframes"]:
                try:
                    df = mt5_client.fetch_ohlcv(
                        symbol, tf, int(cfg["data"]["bars"]),
                        demo=args.demo, cfg_mt5=cfg["mt5"], mt5=mt5)
                    store.upsert_candles(df, symbol, tf)
                    print(f"  {symbol} {tf}: stored {len(df)} bars "
                          f"({time.strftime('%Y-%m-%d %H:%M', time.gmtime(df['time'].iloc[-1]))} latest)")
                except Exception as e:
                    print(f"  [warn] {symbol} {tf}: {e}")
    finally:
        if mt5 is not None:
            mt5_client.shutdown_mt5(mt5)
            print("MT5 connection closed")


def cmd_analyze(cfg, args):
    store = Store(cfg["storage"]
["path"])
    ml = SetupML(cfg["ai"]["ml"]["model_path"]); ml.load()
    llm = LLMAnalyzer(cfg["ai"]["llm"])
    syms = _symbols_for(cfg, args, mt5=None)
    # Auto-retrain on first use after a feature-set upgrade (per-symbol features):
    # Auto-retrain on first use after a feature-set upgrade: the deployed
    # model would otherwise predict with mismatched features (ml_prob None).
    if ml.loaded:
        pipeline.ensure_model_current(cfg, ml, symbols=syms, verbose=True)
    print(f"model: {'loaded' if ml.loaded else 'NOT TRAINED (run `python -m app.main train`)'} | "
          f"llm: {'enabled (' + llm.model + ')' if llm.usable else 'disabled'} | "
          f"symbols: {len(syms)}")
    n = 0
    for symbol in syms:
        for tf in cfg["timeframes"]:
            a = pipeline.analyze_symbol(store, symbol, tf, cfg, ml, llm)
            if a:
                n += 1
                tops = "; ".join(
                    f"{s['direction'].upper()} -> {s['verdict']} (score {s['final_score']}, "
                    f"ML {'-' if s['ml_prob'] is None else format(s['ml_prob']*100, '.0f') + '%'}, RR 1:{s['rr']})"
                    for s in a["setups"]) or "no qualified setups"
                print(f"  {symbol} {tf}: {tops}")
            else:
                print(f"  {symbol} {tf}: not enough data (run ingest)")
    print(f"done -- {n} analyses stored")


def cmd_train(cfg, args):
    syms = _symbols_for(cfg, args, mt5=None)
    print(f"training on {len(syms)} symbols")
    pipeline.train(cfg, symbols=syms,
                   save_baseline=bool(getattr(args, "baseline", False)),
                   compare=bool(getattr(args, "compare", False)))


def cmd_serve(cfg, args):
    import uvicorn
    from .dashboard.server import create_app
    mt5 = _connect_mt5_if_needed(cfg, args.demo)
    syms = _symbols_for(cfg, args, mt5)
    if mt5 is not None:
        mt5_client.shutdown_mt5(mt5)
    cfg["_resolved_symbols"] = syms
    if args.demo:
        cfg["_demo"] = True
    if getattr(args, "auto", 0):
        cfg["dashboard"]["auto_run_interval"] = int(args.auto)
    if getattr(args, "on_bar_close", False):
        cfg["dashboard"]["bar_close_watcher"] = True
        cfg["dashboard"]["auto_run_interval"] = 0   # watcher replaces the timer
    if getattr(args, "auto", 0):
        cfg["dashboard"]["auto_run_interval"] = int(args.auto)
    app = create_app(cfg)
    auto = int(cfg["dashboard"].get("auto_run_interval", 0) or 0)
    if cfg["dashboard"].get("bar_close_watcher"):
        auto_txt = " | bar-close watcher ON (analyzes on confirmed bar closes)"
    elif auto > 0:
        auto_txt = f" | auto-run every {auto}s"
    else:
        auto_txt = ""
    print(f"dashboard -> http://{cfg['dashboard']['host']}:{cfg['dashboard']['port']} "
          f"({len(syms)} symbols){auto_txt}")
    uvicorn.run(app, host=cfg["dashboard"]["host"], port=int(cfg["dashboard"]["port"]),
                log_level="warning")


def cmd_run(cfg, args):
    interval = int(args.interval)
    demo = args.demo
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] running pipeline...")
            mt5 = _connect_mt5_if_needed(cfg, demo)
            try:
                syms = _symbols_for(cfg, args, mt5)
                pipeline.run_all(cfg, demo=demo, ingest=True, verbose=True, symbols=syms)
            finally:
                if mt5 is not None:
                    mt5_client.shutdown_mt5(mt5)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print(f"  [error] {e}")
        print(f"sleeping {interval}s...")
        time.sleep(interval)


def cmd_select_symbols(cfg, args):
    """Manage the persistent enable/disable list.

    --all           enable every resolved symbol (wipes any prior selection)
    --none          clear the selection (default policy becomes 'enable all')
    --enable X Y Z  add these symbols to the enabled set
    --disable X Y Z remove these symbols from the enabled set
    --show          print the current selection
    """
    mt5 = _connect_mt5_if_needed(cfg, args.demo)
    syms = resolve_symbols(cfg, mt5=mt5, verbose=False)
    if mt5 is not None:
        mt5_client.shutdown_mt5(mt5)

    sel = SymbolSelection(SELECTION_PATH)
    current = set(sel.get()) if sel.get() else set(syms)

    if args.action_all:
        sel.enable_all(syms)
        print(f"enabled all {len(syms)} symbols")
    elif args.action_none:
        sel.clear()
        print("cleared selection (default policy: enable all)")
    elif args.enable:
        current.update(args.enable)
        sel.set(sorted(current))
        print(f"enabled {len(args.enable)} additional; total enabled: {len(current)}")
    elif args.disable:
        for s in args.disable:
            current.discard(s)
        sel.set(sorted(current))
        print(f"disabled {len(args.disable)}; total enabled: {len(current)}")
    else:
        # default: show
        if sel.get():
            print(f"explicit selection ({len(sel.get())} symbols):")
            for s in sel.get():
                print(f"  {s}")
        else:
            print(f"no explicit selection -> all {len(syms)} symbols enabled")


def cmd_test_llm(cfg, args):
    """Ping the configured LLM endpoint with a tiny prompt and report status."""
    llm = LLMAnalyzer(cfg["ai"]["llm"])
    print(f"llm config: enabled={llm.cfg.get('enabled')} | base_url={llm.base_url} | model={llm.model}")
    if not llm.cfg.get("enabled", False):
        print("[off] ai.llm.enabled is false in config.yaml"); return
    # common misconfiguration: the key pasted into api_key_env (expects a NAME)
    raw = str(llm.cfg.get("api_key_env", ""))
    if raw and ("sk-" in raw or len(raw) > 40):
        print("[config error] ai.llm.api_key_env must contain the NAME of an environment")
        print("               variable (e.g. MINIMAX_API_KEY), not the API key itself.")
        print("               Fix config.yaml, then:  setx MINIMAX_API_KEY \"<your key>\"")
        print("               (open a NEW terminal afterwards so the variable is visible)")
        return
    if not llm.usable:
        print(f"[off] endpoint is remote and env var {llm._key_env} is not set.\n"
              f"      set it with:  setx {llm._key_env} \"sk-...\"  then reopen the terminal")
        return
    print("pinging endpoint with a 1-token test...")
    import requests as _rq
    try:
        headers = {"Content-Type": "application/json"}
        key = os.getenv(llm._key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = _rq.post(f"{llm.base_url}/chat/completions", headers=headers, timeout=30, json={
            "model": llm.model, "max_tokens": 5,
            "messages": [{"role": "user", "content": "Reply with the single word OK"}]})
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        print(f"[OK] endpoint responded: {content.strip()[:40]!r}")
        print("     the LLM analyst is live -- run analyze and check the dashboard")
    except Exception as e:
        print(f"[FAIL] {e}")
        if "11434" in llm.base_url:
            print("       is Ollama running? start it and pull the model: ollama pull " + llm.model)
        if "1234" in llm.base_url:
            print("       is LM Studio running with the server started?")


def cmd_history(cfg, args):
    """Print the journaled setups (forward-validation data)."""
    store = Store(cfg["storage"]['path'])
    outcomes_mod.resolve_pending(store, cfg)   # resolve what we can first
    rows = store.load_setups_history(args.symbol, args.tf, limit=int(args.limit))
    stats = store.outcome_stats(args.symbol, args.tf)
    print(f"journaled setups: {stats['total']} total "
          f"(resolved: {stats.get('resolved')}, win rate: {stats.get('win_rate')}, "
          f"expectancy: {stats.get('expectancy_r')}R, open: {stats.get('open')})")
    if not rows:
        print("  (empty -- run analyze / refresh to populate)")
        return
    print(f"showing last {len(rows)}:\n")
    print(f"{'formed':<17} {'symbol':<8} {'tf':<5} {'dir':<6} {'verdict':<7} "
          f"{'score':>6} {'ml%':>5} {'rr':>5} {'result':<9} {'R':>6}  entry")
    for r in rows:
        formed = time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['formed_at'] or 0))
        ml = f"{r['ml_prob']*100:.0f}" if r['ml_prob'] is not None else '-'
        res = (r.get('result') or 'OPEN')
        rmul = f"{r['r_multiple']:+.2f}" if r.get('r_multiple') is not None else '-'
        print(f"{formed:<17} {r['symbol']:<8} {r['tf']:<5} "
              f"{(r['direction'] or '-'):<6} {(r['verdict'] or '-'):<7} "
              f"{(r['score'] if r['score'] is not None else 0):>6.1f} {ml:>5} "
              f"{(r['rr'] if r['rr'] is not None else 0):>5.2f} {res:<9} {rmul:>6}  {r['entry']}")


def cmd_outcomes(cfg, args):
    """Resolve pending setup outcomes and print the performance summary."""
    import json
    store = Store(cfg["storage"]["path"])
    n = outcomes_mod.resolve_pending(store, cfg, verbose=True)
    print(f"resolved {n} pending setup(s)")
    rows = store.load_outcome_rows(args.symbol, args.tf)
    agg = outcomes_mod.aggregate(rows)
    o = agg["overall"]
    print(f"\nresolved outcomes: {o['resolved']} (wins {o['wins']}) | "
          f"win rate {o['win_rate']} | expectancy {o['expectancy_r']}R | "
          f"fill rate {o['fill_rate']}")
    print("\nby verdict:")
    for k, v in agg["by_verdict"].items():
        print(f"  {k:<9} n={v['n']:<5} win rate {v['win_rate']} | expectancy {v['expectancy_r']}R")
    print("\nby HTF alignment:")
    for k, v in agg["by_htf_alignment"].items():
        print(f"  {k:<12} n={v['n']:<5} win rate {v['win_rate']} | expectancy {v['expectancy_r']}R")
    print("\nML calibration (predicted vs realized):")
    for k, v in agg["calibration"].items():
        print(f"  {k:<9} n={v['n']:<5} predicted {v['predicted']} | realized {v['realized']}")
    print("\nfull aggregates: GET /api/outcomes/stats (Performance tab)")


def cmd_insights(cfg, args):
    """Print tier-1 diagnosis insights + tier-2 feature discrimination report."""
    from .engine import insights as insights_mod
    store = Store(cfg["storage"]["path"])
    outcomes_mod.resolve_pending(store, cfg)
    rows = store.load_outcome_rows(args.symbol, args.tf)
    ins = insights_mod.generate_insights(rows, cfg)
    print(f"insights ({len(ins)}) — buckets under {insights_mod.MIN_BUCKET} "
          f"resolved outcomes are suppressed:\n")
    if not ins:
        print("  no insights yet — accumulate resolved outcomes (weeks of M15 data)")
    for i in ins:
        sev = {"action": "!!", "warn": " !", "info": "  "}.get(i["severity"], "  ")
        print(f"{sev} [{i['title']}] (n={i['n']})")
        print(f"     {i['finding']}")
        if i["suggest"]:
            print(f"     -> suggest {i['suggest'].get('config_key')}: "
                  f"{i['suggest'].get('current')} -> {i['suggest'].get('proposed')}")
        print()
    rep = insights_mod.feature_report(rows)
    print(f"feature discrimination (tercile win-rate spread, "
          f"powerful >= 0.15, dead < 0.07):")
    for r in rep:
        t = r["terciles"]
        detail = f"  {[x['win_rate'] for x in t]}" if t else ""
        print(f"  {r['feature']:<26} {r['verdict']:<12} spread={r['spread']}{detail}")


def main():
    p = argparse.ArgumentParser(prog="ai-trader")
    p.add_argument("command", choices=["ingest", "analyze", "train", "serve",
                                       "run", "list-symbols", "select-symbols",
                                       "history", "outcomes", "insights", "test-llm"])
    p.add_argument("--demo", action="store_true", help="use synthetic data instead of MT5")
    p.add_argument("--all-symbols", action="store_true",
                   help="override config.yaml symbols with symbols discovered from MT5")
    p.add_argument("--interval", default=60, help="seconds between runs (run command)")
    p.add_argument("--config", default=None, help="path to config.yaml")
    # select-symbols options
    p.add_argument("--enable", nargs="*", default=None,
                   help="symbols to add to the enabled set")
    p.add_argument("--disable", nargs="*", default=None,
                   help="symbols to remove from the enabled set")
    p.add_argument("--all", dest="action_all", action="store_true",
                   help="enable every resolved symbol")
    p.add_argument("--none", dest="action_none", action="store_true",
                   help="clear the selection")
    p.add_argument("--baseline", action="store_true",
                   help="(train) save metrics as baseline for --compare")
    p.add_argument("--compare", action="store_true",
                   help="(train) compare metrics against the saved baseline")
    p.add_argument("--symbol", default=None, help="(history/outcomes/insights) filter by symbol")
    p.add_argument("--tf", default=None, help="(history/outcomes/insights) filter by timeframe")
    p.add_argument("--limit", default=50, help="(history) max rows to show")
    p.add_argument("--auto", type=int, default=0,
                   help="(serve) auto-run the full pipeline every N seconds (0 = off)")
    p.add_argument("--on-bar-close", action="store_true",
                   help="(serve) analyze each pair when its bar closes (recommended)")
    args = p.parse_args()

    cfg = load_config(args.config)
    handlers = {
        "ingest": cmd_ingest, "analyze": cmd_analyze, "train": cmd_train,
        "serve": cmd_serve, "run": cmd_run,
        "list-symbols": cmd_list_symbols,
        "select-symbols": cmd_select_symbols,
        "history": cmd_history,
        "outcomes": cmd_outcomes,
        "insights": cmd_insights,
        "test-llm": cmd_test_llm,
    }
    handlers[args.command](cfg, args)


if __name__ == "__main__":
    main()
