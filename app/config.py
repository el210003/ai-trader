"""Configuration loading with defaults + YAML override."""
import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "mt5": {"terminal_path": None, "login": None, "password": None, "server": None, "timeout": 60000},
    "data": {"bars": 1500},
    "symbols": ["EURUSD", "GBPUSD", "USDJPY"],
    "timeframes": ["M15", "H1"],
    "discover": {
        "enabled": False,                 # when true, override `symbols` with MT5 discovery
        "group": "*",                     # MT5 filter pattern: "*", "*USD*", "Forex\\*", ...
        "only_tradeable": True,           # skip symbols whose trade_mode == DISABLED
        "only_visible": True,             # skip symbols not currently in Market Watch
        "max_count": 200,                 # safety cap on the number of symbols
        "cache_path": "data/discovered_symbols.json",
    },
    "storage": {"path": "data/trader.db"},
    "smc": {
        "swing_lookback": 3,
        "eq_tolerance_pct": 0.0006,
        "ob_max_age_bars": 300,
        "fvg_max_age_bars": 200,
        "sweep_lookback_bars": 30,
        "min_rr": 1.5,
        "default_rr": 2.0,
        "max_rr": 5.0,
        "sl_buffer_atr": 0.25,
        "min_risk_atr": 0.75,
        "atr_period": 14,
    },
    "ai": {
        "ml": {
            "model_path": "data/models/setup_classifier.joblib",
            "label_horizon_bars": 96,
            "label_step": 4,
            "warmup_bars": 300,
            "min_train_samples": 60,
            "auto_retrain": {
                "enabled": True,
                "max_age_days": 7,
                "on_refresh": "warn",   # warn | auto | manual
            },
        },
        "llm": {
            "enabled": True,
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "timeout": 45,
            "min_ml_prob": 0.30,      # skip the LLM when ML win-prob is below this
            "cache_ttl": 900,         # identical setups reuse the LLM answer for N seconds
        },
        "hybrid": {"ml_weight": 0.6, "llm_weight": 0.4, "buy_threshold": 60},
    },
    "csm": {
        "enabled": True,
        "lookback": 20,
        "ma_length": 10,
        "ma_type": "SMA",
        "display": "MA",
        "roc_len": 1,
        "bars": 400,
        "min_pairs": 10,
        "min_interval_seconds": 30,
    },
    "mtf": {
        "enabled": True,
        "require_htf_bias": False,
        "zone_buffer_atr": 0.5,
        "entry_tf": "",
    },
    "outcomes": {
        "enabled": True,
        "lookback_days": 30,
    },
    "dashboard": {"host": "127.0.0.1", "port": 8000, "refresh_seconds": 60,
                  "auto_run_interval": 0, "bar_close_watcher": False,
                  "bar_poll_seconds": 10},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    cfg = _deep_merge(DEFAULTS, user)
    # resolve relative paths against project root
    cfg["storage"]["path"] = str((ROOT / cfg["storage"]["path"]).resolve())
    cfg["ai"]["ml"]["model_path"] = str((ROOT / cfg["ai"]["ml"]["model_path"]).resolve())
    cfg["_root"] = str(ROOT)
    return cfg


def data_dir(cfg: dict) -> Path:
    d = Path(cfg["storage"]["path"]).parent
    d.mkdir(parents=True, exist_ok=True)
    return d
