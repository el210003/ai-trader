"""Pipeline orchestration: ingest -> SMC -> setups -> ML -> LLM -> hybrid -> store."""
import json
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from . import smc as smc_mod
from .csm import update_csm
from .data.store import Store
from .data import mt5_client
from .engine.setup_builder import build_setups
from .engine.mtf import build_htf_context
from .engine.outcomes import resolve_pending
from .ai.features import make_features, FEATURES, feature_vector
from .ai.symbol_stats import SymbolStats
from .ai.ml_model import SetupML
from .ai.llm import LLMAnalyzer
from .ai.hybrid import fuse
from .util import jsonable


def build_llm_context(setup: dict, smc: dict, htf_ctx: dict = None) -> dict:
    ctx = {
        "task": "Evaluate this SMC trade setup. Respond with the JSON schema from your instructions.",
        "symbol": setup["symbol"],
        "timeframe": setup["tf"],
        "market_structure": {
            "trend": smc["trend"],
            "last_events": smc["events"][-3:],
        },
        "liquidity": {
            "recent_sweeps": smc["sweeps"][-4:],
            "pools": smc["liquidity_pools"][-5:],
        },
        "dealing_range": smc["dealing_range"],
        "setup": {k: setup[k] for k in
                  ("direction", "entry", "stop_loss", "take_profit", "rr",
                   "entry_zone", "range_position", "confluences")},
        "ml_win_probability": setup.get("ml_prob"),
        "atr": smc["atr"],
        "last_close": smc["last_close"],
    }
    if htf_ctx:
        ctx["htf_context"] = {
            htf: {"trend": c.get("trend"), "equilibrium": c.get("equilibrium")}
            for htf, c in htf_ctx.items()
        }
    return ctx


def analyze_symbol(store: Store, symbol: str, tf: str, cfg: dict,
                   ml: SetupML, llm: LLMAnalyzer,
                   symbol_stats: SymbolStats = None, drop_last_bar: bool = False):
    """Full analysis for one symbol/timeframe. Returns analysis dict or None.

    drop_last_bar=True analyzes only CLOSED bars (the newest candle MT5 returns
    is the still-forming bar). This aligns live inference with training, where
    the setup bar was always a closed bar."""
    df = store.load_candles(symbol, tf)
    if len(df) < 250:
        return None
    df = df.reset_index(drop=True)
    if drop_last_bar:
        df = df.iloc[:-1]
        if len(df) < 250:
            return None

    smc = smc_mod.analyze(df, cfg["smc"])

    # entry_tf: when set (e.g. "M15"), other timeframes are context only —
    # their SMC structure is stored for chart/HTF use but no trade setups,
    # ML predictions, or LLM calls are produced for them.
    entry_tf = (cfg.get("mtf", {}).get("entry_tf") or "").strip().upper()
    context_only = bool(entry_tf) and tf.strip().upper() != entry_tf

    # MTF confluence context: higher timeframes for this entry TF (H1/H4 for M15)
    mtf_cfg = cfg.get("mtf", {})
    htf_ctx = {}
    csm_dir = None
    if mtf_cfg.get("enabled", True) and not context_only:
        htf_ctx = build_htf_context(symbol, tf, cfg,
                                    up_to_time=int(df["time"].iat[-1]),
                                    load_df=store.load_candles)
        csm = store.load_csm()
        if csm:
            csm_dir = (csm.get("aligned") or {}).get(symbol)

    if context_only:
        analysis = {
            "symbol": symbol, "tf": tf, "generated_at": int(time.time()),
            "meta": {"bars": int(smc["n_bars"]), "model_loaded": ml.loaded,
                     "llm_used": llm.usable},
            "smc": {
                "trend": smc["trend"],
                "events": smc["events"][-40:],
                "sweeps": smc["sweeps"][-40:],
                "liquidity_pools": smc["liquidity_pools"],
                "dealing_range": smc["dealing_range"],
                "order_blocks": smc["order_blocks"],
                "fvgs": smc["fvgs"],
                "atr": smc["atr"],
                "last_close": smc["last_close"],
                "swings": smc["swings"][-40:],
            },
            "htf": {},
            "setups": [],
            "context_only": True,
        }
        store.save_analysis(symbol, tf, jsonable(analysis))
        return analysis

    setups = build_setups(symbol, tf, df, smc, cfg["smc"], htf_ctx=htf_ctx,
                          csm_dir=csm_dir, mtf_cfg=mtf_cfg)

    static = symbol_stats.static_features(df, symbol) if symbol_stats else None
    # live per-symbol dynamic features from RESOLVED outcomes (phase-2 feedback
    # loop; None on cold-start, matching training semantics)
    dynamic = symbol_stats.dynamic_features_live(store, symbol) if symbol_stats else None
    for s in setups:
        # At live inference the dynamic per-symbol features come from resolved
        # outcomes of this symbol (see dynamic_features_live).
        s["features"] = make_features(df, s, smc, cfg["smc"],
                                      symbol_static=static, symbol_dynamic=dynamic,
                                      htf_ctx=htf_ctx)
        s["ml_prob"] = ml.predict(s["features"])

    best = {}
    for s in setups:
        key = s["direction"]
        rank = ((s["ml_prob"] if s["ml_prob"] is not None else 0.5),
                len(s["confluences"]))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, s)
    chosen = [s for _, s in best.values()]

    # ---- LLM enrichment: gated by ML probability, calls run in parallel ----
    llm_cfg = cfg.get("ai", {}).get("llm", {})
    gate = float(llm_cfg.get("min_ml_prob", 0.0) or 0.0)
    for s in chosen:
        s["llm"] = None
    if llm.usable and chosen:
        to_call = []
        for s in chosen:
            mp = s.get("ml_prob")
            if mp is None or mp >= gate:      # gate: skip when ML says hopeless
                to_call.append(s)
        if to_call:
            contexts = [build_llm_context(s, smc, htf_ctx) for s in to_call]
            if len(to_call) == 1:
                to_call[0]["llm"] = llm.analyze_setup(contexts[0])
            else:
                with ThreadPoolExecutor(max_workers=min(4, len(to_call))) as ex:
                    results = list(ex.map(llm.analyze_setup, contexts))
                for s, res in zip(to_call, results):
                    s["llm"] = res
    for s in chosen:
        s.update(fuse(s, cfg["ai"]))

    chosen.sort(key=lambda s: (-s.get("final_score", 0), s["direction"]))

    analysis = {
        "symbol": symbol,
        "tf": tf,
        "generated_at": int(time.time()),
        "meta": {"bars": int(smc["n_bars"]), "model_loaded": ml.loaded,
                 "llm_used": llm.usable},
        "smc": {
            "trend": smc["trend"],
            "events": smc["events"][-40:],
            "sweeps": smc["sweeps"][-40:],
            "liquidity_pools": smc["liquidity_pools"],
            "dealing_range": smc["dealing_range"],
            "order_blocks": smc["order_blocks"],
            "fvgs": smc["fvgs"],
            "atr": smc["atr"],
            "last_close": smc["last_close"],
            "swings": smc["swings"][-40:],
        },
        "htf": {htf: {"trend": c.get("trend"),
                      "equilibrium": c.get("equilibrium"),
                      "zones": len(c.get("zones", [])),
                      "pools": len(c.get("buyside", [])) + len(c.get("sellside", []))}
                for htf, c in htf_ctx.items()},
        "setups": chosen,
    }
    store.save_analysis(symbol, tf, jsonable(analysis))
    # journal for forward validation (dedup on symbol+tf+formed_at+direction)
    try:
        store.append_setups(symbol, tf, chosen, int(time.time()))
    except Exception:
        pass
    return analysis


# --------------------------------------------------------- baseline tracking
ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "data" / "ml_baseline.json"


def store_baseline(metrics: dict):
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump({"saved_at": int(time.time()), "metrics": metrics}, f, indent=2)


def read_baseline():
    if not BASELINE_PATH.exists():
        return None
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _print_compare(current: dict):
    base = read_baseline()
    if not base:
        print("  no baseline saved -- run `python -m app.main train --baseline` first")
        return
    bm = base["metrics"]
    print(f"  comparison vs baseline ({time.strftime('%Y-%m-%d %H:%M', time.localtime(base['saved_at']))}):")
    for k in ("n_samples", "win_rate", "cv_auc", "cv_accuracy"):
        if k in bm and k in current:
            delta = (current[k] or 0) - (bm[k] or 0)
            arrow = "+" if delta > 0 else ("-" if delta < 0 else "=")
            print(f"    {k:14s} {round(bm[k], 4) if isinstance(bm[k], float) else bm[k]}"
                  f" -> {round(current[k], 4) if isinstance(current[k], float) else current[k]}"
                  f"  ({arrow}{abs(delta):+.4f})")


def _print_feature_importances(ml: SetupML, top_n: int = 15):
    imps = (ml.metrics or {}).get("feature_importances")
    if not imps:
        return
    pairs = sorted(imps.items(), key=lambda x: -x[1])[:top_n]
    width = max(len(f) for f in FEATURES)
    print("  feature importance (permutation, roc_auc drop):")
    for name, imp in pairs:
        bar = "#" * int(max(0.0, imp) * 300)
        print(f"    {name:<{width}}  {imp:+.4f}  {bar}")


def peek_last_bar_time(symbol: str, tf: str, demo: bool = False,
                       cfg_mt5: dict = None, mt5=None):
    """Cheap 2-bar peek: returns the open-time of the current (forming) bar.
    When this value changes, a new bar has opened (the previous one closed)."""
    df = mt5_client.fetch_ohlcv(symbol, tf, 2, demo=demo, cfg_mt5=cfg_mt5, mt5=mt5)
    return int(df["time"].iloc[-1]) if len(df) else None


# ------------------------------------------------- parallel pair analysis
_ANALYSIS_POOL = None
_ANALYSIS_POOL_SIZE = None


def _analysis_pool(cfg: dict):
    """Persistent ProcessPoolExecutor for pair analyses (created lazily,
    reused across bar-close batches and run_all cycles)."""
    global _ANALYSIS_POOL, _ANALYSIS_POOL_SIZE
    if not cfg.get("dashboard", {}).get("parallel_analysis", True):
        return None
    size = int(cfg.get("dashboard", {}).get("analysis_workers") or 0)
    if size <= 0:
        size = max(1, min((os.cpu_count() or 2) - 1, 8))
    if _ANALYSIS_POOL is None or _ANALYSIS_POOL_SIZE != size:
        if _ANALYSIS_POOL is not None:
            _ANALYSIS_POOL.shutdown(wait=False)
        _ANALYSIS_POOL = ProcessPoolExecutor(max_workers=size)
        _ANALYSIS_POOL_SIZE = size
    return _ANALYSIS_POOL


def _analyze_pair_worker(payload):
    """ProcessPoolExecutor entry: full analysis for ONE pair in a child
    process (true CPU parallelism — the GIL no longer serializes pandas).
    Loads the model fresh per task so retrains are picked up immediately.
    analyze_symbol saves the analysis + journal rows itself (idempotent)."""
    symbol, tf, storage_path, cfg, drop_last_bar = payload
    out = {"symbol": symbol, "tf": tf, "ok": False, "setups": 0,
           "verdicts": [], "error": None, "seconds": 0.0}
    t0 = time.time()
    try:
        store = Store(storage_path)
        try:
            ml = SetupML(cfg["ai"]["ml"]["model_path"])
            ml.load()
            llm = LLMAnalyzer(cfg.get("ai", {}).get("llm", {}))
            a = analyze_symbol(store, symbol, tf, cfg, ml, llm, SymbolStats(),
                               drop_last_bar=drop_last_bar)
            out["ok"] = True
            out["setups"] = len(a["setups"]) if a else 0
            out["verdicts"] = [(s["direction"], s["verdict"], s.get("final_score"))
                                for s in (a["setups"] if a else [])]
        finally:
            store.conn.close()
    except Exception as e:
        out["error"] = str(e)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def refresh_pair(cfg: dict, store: Store, symbol: str, tf: str, mt5,
                 ml: SetupML, llm: LLMAnalyzer, symbol_stats: SymbolStats = None,
                 drop_forming_bar: bool = True, demo: bool = False):
    """Pull fresh candles for ONE pair, upsert, and re-analyze it.
    Used by the bar-close watcher (and reusable elsewhere)."""
    df = mt5_client.fetch_ohlcv(symbol, tf, int(cfg["data"]["bars"]),
                                demo=demo, cfg_mt5=cfg["mt5"], mt5=mt5)
    store.upsert_candles(df, symbol, tf)
    if symbol_stats:
        symbol_stats.invalidate(symbol)
    ensure_model_current(cfg, ml, store=store, demo=demo,
                         verbose=bool(cfg.get("dashboard", {}).get("debug", True)))
    return analyze_symbol(store, symbol, tf, cfg, ml, llm, symbol_stats,
                          drop_last_bar=drop_forming_bar)


def run_all(cfg: dict, store: Store = None, demo: bool = False,
            ingest: bool = True, verbose: bool = True,
            symbols: list = None) -> int:
    """Ingest fresh candles for every symbol/timeframe then run analysis."""
    store = store or Store(cfg["storage"]["path"])
    ml = SetupML(cfg["ai"]["ml"]["model_path"])
    ml.load()
    ensure_model_current(cfg, ml, store=store, demo=demo, symbols=symbols,
                         verbose=verbose)
    llm = LLMAnalyzer(cfg["ai"]["llm"])
    sym_list = symbols if symbols is not None else list(cfg["symbols"])
    pairs = [(s, tf) for s in sym_list for tf in cfg["timeframes"]]
    symbol_stats = SymbolStats()

    if ingest:
        mt5 = None
        if not demo:
            mt5 = mt5_client.connect_mt5(cfg["mt5"])
        try:
            for symbol, tf in pairs:
                try:
                    df = mt5_client.fetch_ohlcv(symbol, tf, int(cfg["data"]["bars"]),
                                                demo=demo, cfg_mt5=cfg["mt5"], mt5=mt5)
                    store.upsert_candles(df, symbol, tf)
                    symbol_stats.invalidate(symbol)
                    if verbose:
                        print(f"  ingested {symbol} {tf}: {len(df)} bars")
                except Exception as e:
                    if verbose:
                        print(f"  [warn] ingest failed {symbol} {tf}: {e}")
            # currency strength snapshot (pulls the 28 CSM pairs itself)
            try:
                update_csm(cfg, store, mt5=mt5, demo=demo, verbose=verbose)
            except Exception as e:
                if verbose:
                    print(f"  [warn] csm update failed: {e}")
        finally:
            if mt5 is not None:
                mt5_client.shutdown_mt5(mt5)

    count = 0
    t_an = time.time()
    pool = _analysis_pool(cfg)
    if pool is not None and len(pairs) > 1:
        payloads = [(s, tf, cfg["storage"]["path"], cfg, False) for s, tf in pairs]
        for r in pool.map(_analyze_pair_worker, payloads):
            count += 1 if r["ok"] and r["setups"] >= 0 else 0
            if verbose:
                if r["error"]:
                    print(f"  [warn] analyze failed {r['symbol']} {r['tf']}: {r['error']}")
                else:
                    tops = ", ".join(f"{d.upper()} {v} ({sc})" for d, v, sc in r["verdicts"]) or "no setups"
                    print(f"  analyzed {r['symbol']} {r['tf']} ({r['seconds']}s): {tops}")
    else:
        for symbol, tf in pairs:
            try:
                a = analyze_symbol(store, symbol, tf, cfg, ml, llm, symbol_stats)
                if a and verbose:
                    tops = ", ".join(f"{s['direction'].upper()} {s['verdict']} ({s['final_score']})"
                                     for s in a["setups"]) or "no setups"
                    print(f"  analyzed {symbol} {tf}: {tops}")
                count += 1 if a else 0
            except Exception as e:
                if verbose:
                    print(f"  [warn] analyze failed {symbol} {tf}: {e}")
    if verbose:
        print(f"  analysis phase done: {count} pairs in {time.time() - t_an:.1f}s")

    # resolve outcomes of previously journaled setups against fresh candles
    try:
        resolve_pending(store, cfg, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"  [warn] outcome resolution failed: {e}")
    return count


# ------------------------------------------------------------ model versioning
FEATURE_SET_MARKER = ROOT / "data" / "models" / ".feature_set"


def feature_set_hash() -> str:
    """Content hash of the current feature list + setup-builder version.
    Any feature change OR setup-construction change (retest buffer, entry
    validity window, gates...) invalidates deployed models so they retrain,
    keeping the model aligned with the setup distribution it predicts."""
    import hashlib
    payload = {"features": FEATURES, "builder": BUILDER_VERSION}
    return hashlib.md5(json.dumps(payload).encode()).hexdigest()[:12]


# bump when setup-construction behavior changes (not when a feature changes)
BUILDER_VERSION = "v3-entry-validity"


def model_needs_retrain() -> bool:
    """True when the deployed model predates the current feature set.
    Auto-retrain on first use after an upgrade."""
    try:
        return FEATURE_SET_MARKER.read_text().strip() != feature_set_hash()
    except OSError:
        return True


def mark_model_retrained():
    FEATURE_SET_MARKER.parent.mkdir(parents=True, exist_ok=True)
    FEATURE_SET_MARKER.write_text(feature_set_hash())


_retrain_lock = threading.Lock()


def ensure_model_current(cfg: dict, ml: SetupML, store: Store = None,
                         symbols: list = None, demo: bool = False,
                         verbose: bool = False) -> bool:
    """Retrain once when the deployed model predates the current feature set
    (e.g. after a feature upgrade). Safe to call from every path (serve
    refresh job, bar-close watcher, analyze CLI) — only one retrain runs at
    a time; concurrent callers proceed with the old model (predictions
    degrade gracefully to None until the retrain lands)."""
    if not ml.loaded or not model_needs_retrain():
        return False
    if symbols is None:   # default: the enabled/selected symbols, not just config.yaml
        symbols = cfg.get("_resolved_symbols") or list(cfg.get("symbols", []))
    if not _retrain_lock.acquire(blocking=False):
        return False
    try:
        if verbose:
            print("[auto-retrain] deployed model predates the current feature set -- retraining once...")
        train(cfg, store=store, demo=demo, symbols=symbols, verbose=verbose)
        ml.load()
        return True
    except Exception:
        return False
    finally:
        _retrain_lock.release()


def is_model_stale(cfg: dict, ml: SetupML = None) -> bool:
    """True when auto_retrain is enabled and the model is older than
    ai.ml.auto_retrain.max_age_days (or missing/unloadable)."""
    ar = (cfg.get("ai", {}).get("ml", {}).get("auto_retrain") or {})
    if not ar.get("enabled", False):
        return False
    ml = ml or SetupML(cfg["ai"]["ml"]["model_path"])
    if not ml.load():
        return True
    max_age = float(ar.get("max_age_days", 7))
    age = ml.age_days
    return age is None or age >= max_age


def retrain_if_stale(cfg: dict, symbols: list = None, verbose: bool = True) -> bool:
    """Auto mode only: retrain when the model is stale. Returns True if a
    retrain ran. Called by the dashboard refresh job before analyzing."""
    ar = (cfg.get("ai", {}).get("ml", {}).get("auto_retrain") or {})
    if not ar.get("enabled", False):
        return False
    if str(ar.get("on_refresh", "warn")).lower() != "auto":
        return False
    if not is_model_stale(cfg):
        return False
    ml = SetupML(cfg["ai"]["ml"]["model_path"]); ml.load()
    age = ml.age_days
    if verbose:
        age_txt = f"{age:.1f} days old" if age is not None else "missing"
        print(f"  [auto-retrain] model {age_txt} (max {ar.get('max_age_days', 7)}) -- retraining...")
    train(cfg, symbols=symbols, verbose=verbose)
    return True


def _replay_symbol(symbol: str, tfs: list, store, cfg: dict,
                   warmup: int, horizon: int, step: int) -> dict:
    """Replay one symbol's history: build setups per step, label outcomes,
    collect feature rows. Module-level so ProcessPoolExecutor can pickle it
    (tier-parallel replay). Returns rows/labels/replay_ids/made/skip_msgs."""
    from .engine.labeler import label_setup

    rows, labels, rids = [], [], set()
    history_by_pair: dict = {}
    made: dict = {}
    skip_msgs: list = []
    symbol_stats = SymbolStats()
    mtf_on = cfg.get("mtf", {}).get("enabled", True)

    for tf in tfs:
        df = store.load_candles(symbol, tf)
        if df is None or len(df) < warmup + horizon + 10:
            skip_msgs.append(f"  [skip] {symbol} {tf}: "
                             f"only {0 if df is None else len(df)} bars")
            continue
        n = len(df)
        static = symbol_stats.static_features(df, symbol)
        history = history_by_pair.setdefault((symbol, tf), [])
        df_cache: dict = {}     # (symbol, htf) -> full HTF frame
        ctx_cache: dict = {}    # (symbol, htf) last closed HTF bar -> ctx
        made[tf] = 0
        for i in range(warmup, n - horizon - 1, step):
            sub = df.iloc[:i + 1].reset_index(drop=True)
            try:
                smc = smc_mod.analyze(sub, cfg["smc"])
                htf_ctx = build_htf_context(symbol, tf, cfg,
                                            up_to_time=int(sub["time"].iat[-1]),
                                            load_df=store.load_candles,
                                            df_cache=df_cache,
                                            ctx_cache=ctx_cache) if mtf_on else {}
                setups = build_setups(symbol, tf, sub, smc, cfg["smc"],
                                      htf_ctx=htf_ctx,
                                      mtf_cfg=cfg.get("mtf", {}))
            except Exception:
                continue
            # dynamic features: only outcomes that formed before i (no look-ahead)
            prior = [h for h in history if h["formed_index"] < i]
            dynamic = symbol_stats.dynamic_features(prior)
            for s in setups:
                y = label_setup(df, s, horizon, entry_valid_bars=int(cfg.get("smc", {}).get("entry_valid_bars", 24)))
                if y is None:
                    continue
                s["features"] = make_features(sub, s, smc, cfg["smc"],
                                              symbol_static=static,
                                              symbol_dynamic=dynamic,
                                              htf_ctx=htf_ctx)
                rows.append(s["features"])
                labels.append(y)
                rids.add((symbol, tf, int(s["formed_at"]), s["direction"]))
                history.append({
                    "formed_index": s["formed_index"],
                    "horizon_end_index": min(s["formed_index"] + horizon, n - 1),
                    "label": y,
                    "realized_rr": (abs(s["take_profit"] - s["entry"]) /
                                    max(abs(s["entry"] - s["stop_loss"]), 1e-9))
                    if y == 1 else -1.0,
                })
                made[tf] += 1
    return {"symbol": symbol, "rows": rows, "labels": labels, "replay_ids": rids,
            "made": made, "skip_msgs": skip_msgs}


def _replay_worker(payload):
    """ProcessPoolExecutor entry: opens its own Store in the child process."""
    symbol, tfs, storage_path, cfg, warmup, horizon, step = payload
    store = Store(storage_path)
    try:
        return _replay_symbol(symbol, tfs, store, cfg, warmup, horizon, step)
    finally:
        store.close()


def train(cfg: dict, store: Store = None, demo: bool = False,
          symbols: list = None, save_baseline: bool = False,
          compare: bool = False, verbose: bool = True) -> dict:
    """Walk history, generate setups, label outcomes, train the ML model.

    Per-symbol features: static features (avg ATR%, density) come from the
    pair's own history; dynamic features (rolling win rate, realized RR) use
    only outcomes of setups that formed strictly before the current one.
    """
    from .engine.labeler import label_setup

    store = store or Store(cfg["storage"]["path"])
    ml_cfg = cfg["ai"]["ml"]
    horizon = int(ml_cfg["label_horizon_bars"])
    step = max(1, int(ml_cfg["label_step"]))
    warmup = int(ml_cfg["warmup_bars"])

    sym_list = symbols if symbols is not None else list(cfg["symbols"])
    symbol_stats = SymbolStats()

    rows, labels = [], []
    replay_ids: set = set()      # (symbol, tf, formed_at, direction) — tier-3 dedup

    # ---- replay: serial or parallel across symbols (the dominant training cost)
    parallel = bool(ml_cfg.get("parallel", True)) and len(sym_list) > 1
    workers = int(ml_cfg.get("workers") or 0)
    t_replay = time.time()
    if parallel:
        import os as _os
        from concurrent.futures import ProcessPoolExecutor
        n_workers = workers if workers > 0 else max(1, min((_os.cpu_count() or 2) - 1, 8))
        if verbose:
            print(f"  replaying {len(sym_list)} symbols on {n_workers} workers...")
        try:
            payloads = [(sym, list(cfg["timeframes"]), cfg["storage"]["path"], cfg,
                         warmup, horizon, step) for sym in sym_list]
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                for res in ex.map(_replay_worker, payloads):
                    rows.extend(res["rows"])
                    labels.extend(res["labels"])
                    replay_ids.update(res["replay_ids"])
                    if verbose:
                        for msg in res["skip_msgs"]:
                            print(msg)
                        for tf, m in res["made"].items():
                            print(f"  {res['symbol']} {tf}: {m} labeled setups")
        except Exception as e:
            if verbose:
                print(f"  [warn] parallel replay failed ({e}) -- falling back to serial")
            parallel = False
    if not parallel:
        for symbol in sym_list:
            res = _replay_symbol(symbol, list(cfg["timeframes"]), store, cfg,
                                 warmup, horizon, step)
            rows.extend(res["rows"])
            labels.extend(res["labels"])
            replay_ids.update(res["replay_ids"])
            if verbose:
                for msg in res["skip_msgs"]:
                    print(msg)
                for tf, m in res["made"].items():
                    print(f"  {symbol} {tf}: {m} labeled setups")
    if verbose:
        print(f"  replay done: {len(rows)} samples in {time.time() - t_replay:.1f}s")

    if not rows:
        raise RuntimeError("no labeled setups generated")

    # ---- tier 3: blend resolved live outcomes into the training set
    live_cfg = ml_cfg.get("live_outcomes", {})
    live_rows, live_labels = [], []
    if live_cfg.get("enabled"):
        min_live = int(live_cfg.get("min_samples", 200))
        live = store.live_training_samples() if store else []
        # dedupe against replay rows by (symbol, tf, formed_at, direction)
        for s in live:
            key = (s["symbol"], s["tf"], s["formed_at"], s["direction"])
            if key in replay_ids:
                continue
            live_rows.append(s["features"])
            live_labels.append(s["label"])
        if len(live_rows) < min_live:
            if verbose:
                print(f"  [live-blend] {len(live_rows)} live samples < min {min_live} "
                      f"-- training on replay only")
            live_rows, live_labels = [], []

    ml = SetupML(ml_cfg["model_path"])
    backend = str(ml_cfg.get("backend", "sklearn"))
    all_rows, all_labels = rows + live_rows, labels + live_labels
    metrics = ml.train(all_rows, all_labels, min_samples=int(ml_cfg["min_train_samples"]),
                       backend=backend)
    if live_rows:
        metrics["n_live"] = len(live_rows)
        # keep the better of blended vs replay-only by cv_auc
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.model_selection import cross_val_score
        import numpy as _np
        try:
            m_tmp = HistGradientBoostingClassifier(
                max_iter=250, max_depth=4, learning_rate=0.06,
                min_samples_leaf=10, l2_regularization=1.0, random_state=42)
            X0 = _np.array([feature_vector(r) for r in rows], dtype=float)
            y0 = _np.array(labels, dtype=int)
            n_split = 3 if len(y0) >= 90 else 2
            auc0 = float(_np.mean(cross_val_score(m_tmp, X0, y0, cv=n_split, scoring="roc_auc")))
            metrics["cv_auc_replay_only"] = round(auc0, 3)
            if metrics.get("cv_auc") is not None and auc0 > metrics["cv_auc"] + 0.01:
                if verbose:
                    print(f"  [live-blend] replay-only cv_auc {auc0:.3f} beats blended "
                          f"{metrics['cv_auc']:.3f} -- retraining on replay only")
                metrics = ml.train(rows, labels, min_samples=int(ml_cfg["min_train_samples"]), backend=backend)
                metrics["n_live"] = 0
                metrics["cv_auc_replay_only"] = round(auc0, 3)
        except Exception:
            pass
    mark_model_retrained()

    if verbose:
        print(f"  trained model -> {ml_cfg['model_path']}")
        print(f"  metrics: {metrics}")
        _print_feature_importances(ml)
    if save_baseline:
        store_baseline(metrics)
        if verbose:
            print(f"  baseline saved -> {BASELINE_PATH}")
    if compare:
        _print_compare(metrics)
    return metrics
