"""FastAPI dashboard server: JSON API + static HTML UI."""
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..data.store import Store
from ..data.mt5_client import load_discovered_symbols
from ..ai.ml_model import SetupML
from ..ai.llm import LLMAnalyzer
from ..logsetup import debug, debug_enabled
from ..ai.symbol_stats import SymbolStats
from ..symbol_selection import SymbolSelection
from .. import pipeline

STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: dict) -> FastAPI:
    app = FastAPI(title="AI Trader — SMC Hybrid Dashboard")
    store = Store(cfg["storage"]["path"])
    ml = SetupML(cfg["ai"]["ml"]["model_path"])
    ml.load()
    llm = LLMAnalyzer(cfg["ai"]["llm"])
    symbol_stats = SymbolStats()   # shared cache for per-symbol features

    # Symbol list, possibly enriched from discovery cache
    resolved = cfg.get("_resolved_symbols") or list(cfg.get("symbols", []))
    cache_path = (cfg.get("discover") or {}).get("cache_path", "data/discovered_symbols.json")
    discovered_meta = {d["symbol"]: d for d in load_discovered_symbols(cache_path)}

    # Persistent per-symbol enable/disable selection
    selection = SymbolSelection("data/symbol_selection.json")

    def visible_symbols() -> list:
        return selection.enabled_list(resolved)

    state = {"running": False, "retraining": False, "last_run": None,
             "last_retrain": None, "last_error": None, "demo_mode": False,
             "last_pull": None, "last_analysis": None}
    lock = threading.Lock()
    _csm_lock = threading.Lock()   # one background CSM refresh at a time

    auto_cfg = (cfg.get("ai", {}).get("ml", {}).get("auto_retrain") or {})
    auto_run = int(cfg.get("dashboard", {}).get("auto_run_interval", 0) or 0)
    bar_close = bool(cfg.get("dashboard", {}).get("bar_close_watcher", False))
    bar_poll = max(5, int(cfg.get("dashboard", {}).get("bar_poll_seconds", 10)))
    demo_mode = bool(cfg.get("_demo", False))

    # auto-pipeline: replicate the "Run analysis" button every N seconds
    if auto_run > 0 and not bar_close:
        def _auto_loop():
            time.sleep(5)   # let the server finish booting
            while True:
                with lock:
                    if not state["running"] and not state["retraining"]:
                        state["running"] = True
                        threading.Thread(target=_refresh_job, args=(False,), daemon=True).start()
                time.sleep(auto_run)
        threading.Thread(target=_auto_loop, daemon=True).start()

    # bar-close watcher: cheap 2-bar peek per pair; analyze a pair the moment
    # its bar CLOSES, using only confirmed bars (matches training semantics)
    if bar_close:
        watch = {"last_poll_done": time.time()}
        def _bar_watcher():
            from ..data import mt5_client as _m5
            mt5 = None
            last_seen = {}          # (symbol, tf) -> last seen forming-bar open time
            bootstrapped = False
            fail_streak = 0         # consecutive polls with zero readable symbols
            pair_fail_at = {}       # (symbol, tf) -> last failure time (retry cooldown)
            last_hb = 0.0
            while True:
                try:
                    if mt5 is None and not demo_mode:
                        mt5 = _m5.connect_mt5(cfg["mt5"])
                    triggers = []
                    peeked = 0
                    for symbol in visible_symbols():
                        for tf in cfg["timeframes"]:
                            key = (symbol, tf)
                            try:
                                t = pipeline.peek_last_bar_time(symbol, tf, demo=demo_mode, mt5=mt5)
                            except Exception as e:
                                t = None
                                debug(f"watcher peek failed {symbol} {tf}: {e}")
                            if t is None:
                                continue
                            peeked += 1
                            prev = last_seen.get(key)
                            if prev is None:
                                last_seen[key] = t
                                if not bootstrapped and store.load_analysis(symbol, tf) is None:
                                    triggers.append((symbol, tf, t))   # no analysis yet -> analyze now
                                continue
                            if prev != t:
                                triggers.append((symbol, tf, t))       # new bar opened
                            else:
                                last_seen[key] = t
                    bootstrapped = True
                    if triggers:
                        debug("bar-close triggers: " + ", ".join(
                            f"{s} {tf}" for s, tf, _ in triggers))
                    batch = []
                    for (symbol, tf, t_new) in triggers:
                        key = (symbol, tf)
                        with lock:
                            busy = state["running"] or state["retraining"]
                        if busy:
                            continue        # retried next poll
                        if time.time() - pair_fail_at.get(key, 0) < 60:
                            continue        # failed recently -> cooldown before retry
                        last_seen[key] = t_new          # claim this bar
                        batch.append((symbol, tf))
                    if batch:
                        t_b = time.time()
                        debug(f"processing {len(batch)} bar-close pair(s): "
                              + ", ".join(f"{s} {tf}" for s, tf in batch))
                        state["last_pull"] = {"at": int(t_b)}
                        # phase 1: pull fresh candles (sequential MT5 IPC)
                        pull_fail = set()
                        for symbol, tf in batch:
                            try:
                                df = _m5.fetch_ohlcv(symbol, tf, int(cfg["data"]["bars"]),
                                                     demo=demo_mode, cfg_mt5=cfg["mt5"], mt5=mt5)
                                store.upsert_candles(df, symbol, tf)
                            except Exception as e:
                                debug(f"pull failed {symbol} {tf}: {e}")
                                pull_fail.add((symbol, tf))
                                last_seen[(symbol, tf)] = -1
                                pair_fail_at[(symbol, tf)] = time.time()
                        to_analyze = [(s, tf) for s, tf in batch if (s, tf) not in pull_fail]
                        # model freshness: one check per batch (workers load the
                        # model file per task, so retrains are picked up)
                        if to_analyze:
                            try:
                                pipeline.ensure_model_current(cfg, ml, store=store, demo=demo_mode,
                                                              verbose=debug_enabled(cfg))
                            except Exception as e:
                                debug(f"ensure_model_current failed: {e}")
                        # phase 2: analyses in the process pool (true CPU parallelism)
                        results = []
                        pool = pipeline._analysis_pool(cfg)
                        if pool is not None and len(to_analyze) > 1:
                            payloads = [(s, tf, cfg["storage"]["path"], cfg, True)
                                        for s, tf in to_analyze]
                            results = list(pool.map(pipeline._analyze_pair_worker, payloads))
                        else:
                            for s, tf in to_analyze:
                                try:
                                    pipeline.refresh_pair(cfg, store, s, tf, mt5, ml, llm,
                                                          symbol_stats, drop_forming_bar=True,
                                                          demo=demo_mode)
                                    results.append({"symbol": s, "tf": tf, "ok": True})
                                except Exception as e:
                                    results.append({"symbol": s, "tf": tf, "ok": False, "error": str(e)})
                        for r in results:
                            if not r.get("ok"):
                                k = (r["symbol"], r["tf"])
                                last_seen[k] = -1
                                pair_fail_at[k] = time.time()
                                debug(f"analyze FAILED {r['symbol']} {r['tf']}: {r.get('error')}")
                        ok_n = sum(1 for r in results if r.get("ok"))
                        state["last_analysis"] = {"at": int(time.time())}
                        state["last_run"] = {"at": int(time.time()), "analyses": ok_n,
                                             "seconds": round(time.time() - t_b, 1),
                                             "bar_close": f"{len(batch)} pairs"}
                        if not any(not r.get("ok") for r in results) and not pull_fail:
                            state["last_error"] = None
                        debug(f"bar-close batch done: {ok_n}/{len(batch)} analyzed "
                              f"in {time.time() - t_b:.1f}s")
                    # refresh the CSM snapshot when bars closed (throttled internally)
                    if batch:
                        try:
                            pipeline.update_csm(cfg, store, mt5=mt5, demo=demo_mode)
                        except Exception as e:
                            state["last_error"] = f"csm update: {e}"
                        # resolve outcomes of previously journaled setups
                        try:
                            pipeline.resolve_pending(store, cfg)
                        except Exception as e:
                            state["last_error"] = f"outcome resolve: {e}"
                except Exception as e:
                    state["last_error"] = f"bar watcher: {e}"
                    debug(f"bar watcher poll error: {e}")
                    try:
                        _m5.shutdown_mt5(mt5)
                    except Exception:
                        pass
                    mt5 = None                     # dead IPC -> force fresh reconnect
                # self-heal: 3+ polls with zero readable symbols -> reconnect MT5
                if peeked == 0 and not demo_mode:
                    fail_streak += 1
                    if fail_streak >= 3:
                        debug(f"watcher: {fail_streak} polls with no readable symbols "
                              f"-- forcing MT5 reconnect")
                        try:
                            _m5.shutdown_mt5(mt5)
                        except Exception:
                            pass
                        mt5 = None
                        fail_streak = 0
                else:
                    fail_streak = 0
                if time.time() - watch["last_poll_done"] > 60:
                    debug(f"previous watcher poll took "
                          f"{time.time() - watch['last_poll_done']:.0f}s (MT5 blocking?)")
                watch["last_poll_done"] = time.time()
                if time.time() - last_hb > 120:
                    debug(f"watcher alive: peeked {peeked} pairs")
                    last_hb = time.time()
                time.sleep(bar_poll)
        def _watchdog():
            """The MT5 python API blocks without timeout — if the poll loop hangs,
            this makes it visible instead of silent."""
            while True:
                time.sleep(60)
                gap = time.time() - watch["last_poll_done"]
                if gap > 120:
                    debug(f"WARNING: bar watcher has not completed a poll for {gap:.0f}s "
                          f"-- an MT5 call is likely blocking; restart serve.bat to recover")
        threading.Thread(target=_bar_watcher, daemon=True).start()
        threading.Thread(target=_watchdog, daemon=True).start()

    def _refresh_job(demo: bool):
        try:
            verbose = debug_enabled(cfg)
            t0 = time.time()
            debug(f"analysis cycle started ({len(visible_symbols())} symbols "
                  f"x {len(cfg['timeframes'])} TFs, demo={demo})")
            # auto mode: retrain stale model before analyzing
            try:
                pipeline.retrain_if_stale(cfg, symbols=visible_symbols(), verbose=verbose)
            except Exception as e:
                state["last_error"] = f"auto-retrain failed: {e}"
            state["last_pull"] = {"at": int(time.time())}   # ingest (bar pull) starts now
            n = pipeline.run_all(cfg, store=store, demo=demo, ingest=True,
                                 verbose=verbose, symbols=visible_symbols())
            state["last_analysis"] = {"at": int(time.time())}
            state["last_run"] = {"at": int(time.time()), "analyses": n,
                                 "seconds": round(time.time() - t0, 1)}
            state["last_error"] = None
            debug(f"analysis cycle finished: {n} analyses in "
                  f"{time.time() - t0:.1f}s")
        except Exception as e:
            state["last_error"] = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            state["running"] = False

    def _retrain_job():
        try:
            t0 = time.time()
            pipeline.train(cfg, symbols=visible_symbols(), verbose=False)
            state["last_retrain"] = {"at": int(time.time()),
                                     "seconds": round(time.time() - t0, 1)}
            state["last_error"] = None
        except Exception as e:
            state["last_error"] = f"retrain failed: {e}\n{traceback.format_exc(limit=3)}"
        finally:
            state["retraining"] = False

    @app.get("/")
    def index():
        # never cache the UI — otherwise code updates need a hard browser refresh
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                     "Pragma": "no-cache", "Expires": "0"})

    @app.get("/api/meta")
    def meta():
        age = ml.age_days
        stale = pipeline.is_model_stale(cfg, ml)
        import numpy as _np
        return {
            "symbols": visible_symbols(),
            "timeframes": cfg["timeframes"],
            "entry_tf": (cfg.get("mtf", {}).get("entry_tf") or "").strip().upper(),
            "refresh_seconds": int(cfg["dashboard"]["refresh_seconds"]),
            "auto_run_seconds": auto_run if not bar_close else 0,
            "bar_close_watcher": bar_close,
            "model_loaded": ml.loaded,
            "model_age_days": round(age, 2) if age is not None else None,
            "model_stale": bool(stale),
            "model_env": ml.env,
            "runtime_numpy": _np.__version__,
            "env_mismatch": ml.env_mismatch,
            "auto_retrain": {
                "enabled": bool(auto_cfg.get("enabled", False)),
                "max_age_days": auto_cfg.get("max_age_days", 7),
                "on_refresh": auto_cfg.get("on_refresh", "warn"),
            },
            "model_metrics": ml.metrics,
            "llm_enabled": llm.usable,
            "llm_model": llm.model if llm.usable else None,
            "llm_gate": float((cfg.get("ai", {}).get("llm", {}) or {}).get("min_ml_prob", 0.0) or 0.0),
            "analyses": store.list_analyses(),
        }

    # ---------------------------------------------------------- symbols
    class SelectionBody(BaseModel):
        enabled: list

    @app.get("/api/symbols/all")
    def all_symbols():
        """Return every known symbol with path + selection status + last
        analysis timestamp. Drives the settings panel."""
        last_analyses = {a["symbol"]: a["updated_at"]
                          for a in store.list_analyses()}
        items = []
        for s in resolved:
            meta = discovered_meta.get(s, {})
            items.append({
                "symbol": s,
                "path": meta.get("path", ""),
                "description": meta.get("description", ""),
                "enabled": selection.is_enabled(s),
                "last_analyzed_at": last_analyses.get(s),
            })
        return {"symbols": items, "count": len(items)}

    @app.get("/api/symbols/selection")
    def get_selection():
        return {"enabled": selection.get(),
                "has_explicit_selection": bool(selection.get())}

    @app.post("/api/symbols/selection")
    def set_selection(body: SelectionBody):
        selection.set(body.enabled)
        return {"ok": True, "enabled": selection.get()}

    @app.post("/api/symbols/select-all")
    def select_all():
        selection.enable_all(resolved)
        return {"ok": True, "enabled": selection.get()}

    @app.post("/api/symbols/clear")
    def clear_selection():
        selection.clear()
        return {"ok": True, "enabled": []}

    # ----------------------------------------------------------- analysis
    @app.get("/api/analysis")
    def analysis(symbol: str = Query(...), tf: str = Query(...), bars: int = 400):
        payload = store.load_analysis(symbol, tf)
        if payload is None:
            ml.load()   # pick up a model retrained by a background job
            payload = pipeline.analyze_symbol(store, symbol, tf, cfg, ml, llm)
        candles = store.load_candles(symbol, tf)
        if candles.empty:
            return JSONResponse(
                {"error": f"no candles for {symbol} {tf} - run ingest first"},
                status_code=404)
        tail = candles.tail(max(50, min(bars, len(candles))))
        candle_list = [
            {"time": int(r.time), "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close)}
            for r in tail.itertuples()
        ]
        return {"analysis": payload, "candles": candle_list}

    # ---------------------------------------------------------- lifecycle
    @app.post("/api/refresh")
    def refresh(demo: bool = False):
        with lock:
            if state["running"] or state["retraining"]:
                return {"ok": False, "message": "a job is already running"}
            state["running"] = True
            state["demo_mode"] = demo
            threading.Thread(target=_refresh_job, args=(demo,), daemon=True).start()
        return {"ok": True, "message": "analysis started"}

    @app.post("/api/retrain")
    def retrain():
        """Manual retrain trigger (warn mode: user clicks the dashboard button)."""
        with lock:
            if state["running"] or state["retraining"]:
                return {"ok": False, "message": "a job is already running"}
            state["retraining"] = True
            threading.Thread(target=_retrain_job, daemon=True).start()
        return {"ok": True, "message": "retraining started"}

    @app.get("/api/csm")
    def csm_data():
        snap = store.load_csm()
        # self-heal: with the dashboard open but no watcher/auto-run running,
        # kick a background refresh whenever the snapshot goes stale
        max_age = max(60, int(cfg.get("csm", {}).get("min_interval_seconds", 30)))
        if snap is None or time.time() - snap.get("generated_at", 0) > max_age:
            if _csm_lock.acquire(blocking=False):
                def _csm_work():
                    try:
                        pipeline.update_csm(cfg, store, mt5=mt5, demo=demo_mode)
                    except Exception:
                        pass
                    finally:
                        _csm_lock.release()
                threading.Thread(target=_csm_work, daemon=True).start()
        return snap or {"strength": {}, "aligned": {}, "transitions": [],
                        "pair_states": {}, "generated_at": 0, "pairs_used": 0}

    @app.get("/api/setups/ranked")
    def ranked_setups(limit: int = 30):
        """All current setups across every analyzed pair, ranked by hybrid score."""
        rows = []
        pairs = 0
        for entry in store.list_analyses():
            payload = store.load_analysis(entry["symbol"], entry["tf"])
            if not payload:
                continue
            pairs += 1
            for s in payload.get("setups", []):
                rows.append({
                    "symbol": entry["symbol"], "tf": entry["tf"],
                    "direction": s.get("direction"), "verdict": s.get("verdict"),
                    "score": s.get("final_score"), "ml_prob": s.get("ml_prob"),
                    "rr": s.get("rr"), "entry": s.get("entry"),
                    "stop_loss": s.get("stop_loss"), "take_profit": s.get("take_profit"),
                    "aligned": s.get("aligned"), "formed_at": s.get("formed_at"),
                    "confluences": len(s.get("confluences", [])),
                    "updated_at": entry["updated_at"],
                })
        rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
        return {"rows": rows[:max(1, limit)], "total": len(rows), "pairs": pairs}

    @app.get("/api/setups/history")
    def setups_history(symbol: str = None, tf: str = None, limit: int = 200):
        from ..engine import outcomes as outcomes_mod
        rows = store.load_setups_history(symbol, tf, limit)
        return {"rows": rows,
                "stats": store.outcome_stats(symbol, tf)}

    @app.get("/api/mtf_status")
    def mtf_status(symbol: str):
        """Per-timeframe structure status for one symbol (bullish/bearish/range)
        from the stored analyses. 'range' = no structure event, or the last
        BOS/CHoCH is older than 20 bars on that timeframe."""
        out = {}
        for tf in cfg["timeframes"]:
            p = store.load_analysis(symbol, tf)
            if not p:
                out[tf] = {"status": "—", "trend": None, "age": None}
                continue
            smc = p.get("smc") or {}
            events = smc.get("events") or []
            n_bars = (p.get("meta") or {}).get("bars") or 0
            last = events[-1] if events else None
            age = None
            if last and isinstance(last.get("index"), int) and n_bars:
                age = max(0, n_bars - 1 - last["index"])
            trend = smc.get("trend")
            status = trend if trend in ("bullish", "bearish") else "range"
            if age is not None and age > 20:
                status = "range"       # last structure event is stale
            dr = smc.get("dealing_range") or {}
            out[tf] = {"status": status, "trend": trend, "age": age,
                       "zone": dr.get("zone"), "position": dr.get("position")}
        return out

    @app.get("/api/outcomes/stats")
    def outcomes_stats(symbol: str = None, tf: str = None):
        """Aggregated setup-effectiveness views (Performance tab)."""
        from ..engine import outcomes as outcomes_mod
        rows = store.load_outcome_rows(symbol, tf)
        return outcomes_mod.aggregate(rows)

    @app.get("/api/insights")
    def insights(tf: str = None):
        """Tier 1+2: diagnosis insights + feature discrimination report."""
        from ..engine import insights as insights_mod
        rows = store.load_outcome_rows(None, tf)
        return {"insights": insights_mod.generate_insights(rows, cfg),
                "feature_report": insights_mod.feature_report(rows),
                "n_resolved": sum(1 for r in rows if r["result"] in ("WIN", "LOSS"))}

    @app.post("/api/outcomes/resolve")
    def outcomes_resolve():
        from ..engine import outcomes as outcomes_mod
        with lock:
            busy = state["running"] or state["retraining"]
        if busy:
            return JSONResponse({"ok": False, "error": "pipeline busy"}, status_code=409)
        n = outcomes_mod.resolve_pending(store, cfg)
        return {"ok": True, "resolved": n}

    @app.get("/api/status")
    def status():
        return {k: state[k] for k in ("running", "retraining", "last_run",
                                      "last_retrain", "last_error", "demo_mode",
                                      "last_pull", "last_analysis")}

    class NoCacheStatic(StaticFiles):
        """Serve static assets with no-cache so UI updates land on normal refresh."""
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/static", NoCacheStatic(directory=str(STATIC_DIR)), name="static")
    return app
