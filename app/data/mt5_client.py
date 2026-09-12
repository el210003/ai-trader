"""MT5 OHLC ingestion with an offline demo feed fallback.

The real feed requires MetaTrader 5 running locally on Windows
(pip package `MetaTrader5` talks to the terminal directly).
The demo feed generates deterministic synthetic OHLC so the whole
pipeline + dashboard can be developed/tested without a terminal.
"""
import time
import zlib

import numpy as np
import pandas as pd

# MetaTrader5 timeframe constants (numeric values are stable across versions)
TF_CONST = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H2": 16386, "H4": 16388, "H6": 16390, "H12": 16396,
    "D1": 16408, "W1": 32769, "MN1": 49153,
}
TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H2": 7200, "H4": 14400, "H6": 21600, "H12": 43200,
    "D1": 86400, "W1": 604800, "MN1": 2592000,
}


# ---------------------------------------------------------------- MT5 feed
def connect_mt5(cfg_mt5: dict):
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 package not installed. On Windows: pip install MetaTrader5. "
            "Or run with --demo to use synthetic data."
        ) from e

    kwargs = {}
    if cfg_mt5.get("terminal_path"):
        kwargs["path"] = cfg_mt5["terminal_path"]
    if cfg_mt5.get("login"):
        kwargs.update(
            login=int(cfg_mt5["login"]),
            password=cfg_mt5.get("password") or "",
            server=cfg_mt5.get("server") or "",
        )
    if cfg_mt5.get("timeout"):
        kwargs["timeout"] = int(cfg_mt5["timeout"])

    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    return mt5


def fetch_ohlcv_mt5(mt5, symbol: str, tf: str, bars: int) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, TF_CONST[tf], 0, bars)
    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        raise RuntimeError(
            f"No rates returned for {symbol} {tf} (error {err}). "
            "Check the symbol exists in Market Watch and the terminal is logged in."
        )
    df = pd.DataFrame(rates)
    df = df.rename(columns={"tick_volume": "volume"})
    df["time"] = df["time"].astype("int64")
    return df[["time", "open", "high", "low", "close", "volume"]]


def shutdown_mt5(mt5) -> None:
    try:
        mt5.shutdown()
    except Exception:
        pass


# ------------------------------------------------------------- discovery
# Trade-mode constants from MetaTrader5 (stable numeric values)
_TRADE_MODE_DISABLED = 0
_TRADE_MODE_LONGONLY = 1
_TRADE_MODE_SHORTONLY = 2
_TRADE_MODE_CLOSEONLY = 3
_TRADE_MODE_FULL = 4


def discover_symbols(mt5, group: str = "*", only_tradeable: bool = True,
                     only_visible: bool = True, max_count: int = 200) -> list:
    """Enumerate symbols from MT5 and return a list of dicts:

       [{"symbol", "path", "description", "trade_mode", "visible"}, ...]

    Args:
        mt5:            an already-initialized MetaTrader5 module reference.
        group:          MT5 filter pattern, e.g. "*" (all), "*USD*", "*FX*",
                        "Forex\\Majors*", "Metals*".
        only_tradeable: drop symbols whose trade_mode == DISABLED.
        only_visible:   drop symbols not currently in Market Watch.
        max_count:      safety cap to keep the pipeline manageable when a
                        broker exposes hundreds of instruments.
    """
    raw = mt5.symbols_get(group=group) or []
    out = []
    for s in raw:
        info = mt5.symbol_info(s.name)
        if info is None:
            continue
        if only_visible and not info.visible:
            continue
        if only_tradeable and info.trade_mode == _TRADE_MODE_DISABLED:
            continue
        out.append({
            "symbol": s.name,
            "path": getattr(s, "path", ""),
            "description": getattr(s, "description", ""),
            "trade_mode": int(info.trade_mode),
            "visible": bool(info.visible),
        })
        if len(out) >= max_count:
            break
    # Stable, predictable order: alphabetical, but majors first if recognised
    majors = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
              "USDCAD", "NZDUSD", "XAUUSD", "XAGUSD"}
    out.sort(key=lambda d: (0 if d["symbol"] in majors else 1, d["symbol"]))
    return out


def persist_discovered_symbols(path: str, symbols: list) -> None:
    """Write the discovered list so the dashboard can load it without
    re-querying MT5 at every server restart."""
    import json
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"symbols": symbols, "count": len(symbols)}, f, indent=2)


def load_discovered_symbols(path: str) -> list:
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("symbols", [])
    except Exception:
        return []


# ---------------------------------------------------------------- demo feed
_BASE_PRICES = {"EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": 151.50,
                "XAUUSD": 2350.0, "AUDUSD": 0.6600, "USDCAD": 1.3600}


def demo_ohlcv(symbol: str, tf: str, bars: int, seed: int = None) -> pd.DataFrame:
    """Deterministic synthetic OHLC with trending regimes and engineered liquidity sweeps."""
    tf_sec = TF_SECONDS[tf]
    seed = seed if seed is not None else zlib.crc32(f"{symbol}|{tf}".encode())
    rng = np.random.default_rng(seed)

    base = _BASE_PRICES.get(symbol, 100.0)
    vol = base * 0.00045

    # regime-switching drift -> produces swing structure
    n_reg = max(2, bars // 180)
    drifts = rng.choice([-1.0, 0.0, 1.0], size=n_reg, p=[0.38, 0.24, 0.38]) * vol * 0.30
    drift = np.resize(np.repeat(drifts, 180), bars)

    steps = rng.normal(0.0, vol, bars) + drift
    close = base + np.cumsum(steps)
    open_ = np.empty(bars)
    open_[0] = base
    open_[1:] = close[:-1]

    wick = np.abs(rng.normal(0.0, vol * 0.55, bars))
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, vol * 0.55, bars))

    # engineered stop-hunt spikes: wick beyond a recent extreme, close back inside
    # (needs enough bars to make sense; skipped for tiny windows like 2-bar peeks)
    spikes = max(4, bars // 120) if bars > 70 else 0
    for _ in range(spikes):
        i = int(rng.integers(60, bars - 2))
        look = slice(max(0, i - 40), i)
        if rng.random() < 0.5:  # buy-side sweep (above highs)
            level = high[look].max()
            high[i] = level + vol * (0.8 + rng.random())
            close[i] = min(close[i], level - vol * 0.15)
            open_[i] = min(max(open_[i], low[i]), close[i])
        else:                   # sell-side sweep (below lows)
            level = low[look].min()
            low[i] = level - vol * (0.8 + rng.random())
            close[i] = max(close[i], level + vol * 0.15)
            open_[i] = max(min(open_[i], high[i]), close[i])

    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    # anchor to the bar boundary so repeated ingests dedupe cleanly (INSERT OR REPLACE)
    now = int(time.time()) // tf_sec * tf_sec
    times = now - (np.arange(bars)[::-1] * tf_sec)
    return pd.DataFrame({
        "time": times.astype("int64"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(100, 5000, bars).astype(float),
    })


def fetch_ohlcv(symbol: str, tf: str, bars: int, demo: bool, cfg_mt5: dict = None, mt5=None):
    if demo:
        return demo_ohlcv(symbol, tf, bars)
    if mt5 is None:
        mt5 = connect_mt5(cfg_mt5 or {})
    return fetch_ohlcv_mt5(mt5, symbol, tf, bars)
