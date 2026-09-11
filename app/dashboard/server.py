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
        def _bar_watcher():
            from ..data import mt5_client as _m5
            mt5 = None
            last_seen = {}          # (symbol, tf) -> last seen forming-bar open time
            bootstrapped = False
            in_flight = set()       # pairs currently being analyzed
            while True:
                try:
                    if mt5 is None and not demo_mode:
                        mt5 = _m5.connect_mt5(cfg["mt5"])
                    triggers = []
                    for symbol in visible_symbols():
                        for tf in cfg["timeframes"]:
                            key = (symbol, tf)
                            try:
                                t = pipeline.peek_last_bar_time(symbol, tf, demo=demo_mode, mt5=mt5)
                            except Exception:
                                t = None
                            if t is None:
                                continue
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
                    for (symbol, tf, t_new) in triggers:
                        key = (symbol, tf)
                        with lock:
                            busy = state["running"] or state["retraining"]
                        if busy or key in in_flight or len(in_flight) >= 3:
                            continue        # last_seen not advanced -> retried next poll
                        last_seen[key] = t_new          # claim this bar
                        in_flight.add(key)
                        def _work(symbol=symbol, tf=tf, t_new=t_new):
                            try:
                                t0 = time.time()
                                state["last_pull"] = {"at": int(t0)}   # bar pull starts
                                pipeline.refresh_pair(cfg, store, symbol, tf, mt5, ml, llm,
                                                      symbol_stats, drop_forming_bar=True,
                                                      demo=demo_mode)
                                state["last_analysis"] = {"at": int(time.time())}
                                state["last_run"] = {"at": int(time.time()), "analyses": 1,
                                                     "seconds": round(time.time() - t0, 1),
                                                     "bar_close": f"{symbol} {tf}"}
                                state["last_error"] = None
                            except Exception as e:
                                state["last_error"] = f"bar-close {symbol} {tf}: {e}"
                                last_seen[(symbol, tf)] = -1   # force retry next poll
                            finally:
                                in_flight.discard((symbol, tf))
                        threading.Thread(target=_work, daemon=True).start()
                    # refresh the CSM snapshot when bars closed (throttled internally)
                    if triggers:
                        try:
                            pipeline.update_csm(cfg, store, mt5=mt5, demo=demo_mode)
                        except Exception as e:
                            state["last_error"] = f"csm update: {e}"
                except Exception as e:
                    state["last_error"] = f"bar watcher: {e}"
                    try:
                        _m5.shutdown_mt5(mt5)
                    except Exception:
                        pass
                    mt5 = None                     # reconnect next cycle
                time.sleep(bar_poll)
        threading.Thread(target=_bar_watcher, daemon=True).start()

    def _refresh_job(demo: bool):
        try:
            t0 = time.time()
            # auto mode: retrain stale model before analyzing
            try:
                pipeline.retrain_if_stale(cfg, symbols=visible_symbols(), verbose=False)
            except Exception as e:
                state["last_error"] = f"auto-retrain failed: {e}"
            state["last_pull"] = {"at": int(time.time())}   # ingest (bar pull) starts now
            n = pipeline.run_all(cfg, store=store, demo=demo, ingest=True,
                                 verbose=False, symbols=visible_symbols())
            state["last_analysis"] = {"at": int(time.time())}
            state["last_run"] = {"at": int(time.time()), "analyses": n,
                                 "seconds": round(time.time() - t0, 1)}
            state["last_error"] = None
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
        return {"rows": store.load_setups_history(symbol, tf, limit),
                "stats": store.outcome_stats(symbol, tf)}

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
