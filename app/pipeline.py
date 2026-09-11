"""Pipeline orchestration: ingest -> SMC -> setups -> ML -> LLM -> hybrid -> store."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import smc as smc_mod
from .csm import update_csm
from .data.store import Store
from .data import mt5_client
from .engine.setup_builder import build_setups
from .engine.mtf import build_htf_context
from .ai.features import make_features, FEATURES
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

    # MTF confluence context: higher timeframes for this entry TF (H1/H4 for M15)
    mtf_cfg = cfg.get("mtf", {})
    htf_ctx = {}
    csm_dir = None
    if mtf_cfg.get("enabled", True):
        htf_ctx = build_htf_context(symbol, tf, cfg,
                                    up_to_time=int(df["time"].iat[-1]),
                                    load_df=store.load_candles)
        csm = store.load_csm()
        if csm:
            csm_dir = (csm.get("aligned") or {}).get(symbol)

    setups = build_setups(symbol, tf, df, smc, cfg["smc"], htf_ctx=htf_ctx,
                          csm_dir=csm_dir, mtf_cfg=mtf_cfg)

    static = symbol_stats.static_features(df, symbol) if symbol_stats else None
    for s in setups:
        # At live inference the dynamic per-symbol features are neutral
        # (no labeled history available at prediction time).
        s["features"] = make_features(df, s, smc, cfg["smc"],
                                      symbol_static=static, symbol_dynamic=None,
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
    ensure_model_current(cfg, ml, store=store, demo=demo)
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
    return count


# ------------------------------------------------------------ model versioning
FEATURE_SET_MARKER = ROOT / "data" / "models" / ".feature_set"


def feature_set_hash() -> str:
    """Content hash of the current feature list — any feature change
    invalidates deployed models without renaming marker files."""
    import hashlib
    return hashlib.md5(json.dumps(FEATURES).encode()).hexdigest()[:12]


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
    history_by_pair: dict = {}   # (symbol, tf) -> list of prior outcomes

    for symbol in sym_list:
        for tf in cfg["timeframes"]:
            df = store.load_candles(symbol, tf)
            if len(df) < warmup + horizon + 10:
                if verbose:
                    print(f"  [skip] {symbol} {tf}: only {len(df)} bars")
                continue
            n = len(df)
            static = symbol_stats.static_features(df, symbol)
            history = history_by_pair.setdefault((symbol, tf), [])
            mtf_on = cfg.get("mtf", {}).get("enabled", True)
            df_cache: dict = {}     # (symbol, htf) -> full HTF frame
            ctx_cache: dict = {}    # (symbol, htf, last closed HTF bar) -> ctx
            made = 0
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
                    y = label_setup(df, s, horizon)
                    if y is None:
                        continue
                    s["features"] = make_features(sub, s, smc, cfg["smc"],
                                                  symbol_static=static,
                                                  symbol_dynamic=dynamic,
                                                  htf_ctx=htf_ctx)
                    rows.append(s["features"])
                    labels.append(y)
                    history.append({
                        "formed_index": s["formed_index"],
                        "horizon_end_index": min(s["formed_index"] + horizon, n - 1),
                        "label": y,
                        "realized_rr": (abs(s["take_profit"] - s["entry"]) /
                                        max(abs(s["entry"] - s["stop_loss"]), 1e-9))
                        if y == 1 else -1.0,
                    })
                    made += 1
            if verbose:
                print(f"  {symbol} {tf}: {made} labeled setups")

    if not rows:
        raise RuntimeError("no labeled setups generated")

    ml = SetupML(ml_cfg["model_path"])
    metrics = ml.train(rows, labels, min_samples=int(ml_cfg["min_train_samples"]))
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
