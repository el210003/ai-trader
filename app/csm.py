"""Currency Strength Meter (CSM28 port).

For each of the 28 standard FX pairs, compute the z-score of close price over
`lookback` bars:  z = (close - SMA(close, lookback)) / STDEV(close, lookback)

Currency strength = mean of the signed z-scores of every pair containing that
currency (base adds +1, quote adds -1).  With all 28 pairs present the 8
strengths sum to ~0 by construction.

Strengths are smoothed (SMA/EMA over `ma_length`) and optionally expressed as
momentum (ROC over `roc_len` bars) when display == "ROC".

Also derives, per pair: base-vs-quote state on each timeframe ("up"/"down"),
cross-timeframe alignment ("up" when all TFs agree, "down", None when mixed),
and alignment TRANSITIONS vs the previous snapshot.
"""
import time

import pandas as pd

CURRENCIES = ["EUR", "USD", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY"]

# the 28 standard pairs: (pair, base, quote)
CSM_PAIRS = [
    ("EURUSD", "EUR", "USD"), ("GBPUSD", "GBP", "USD"), ("AUDUSD", "AUD", "USD"),
    ("NZDUSD", "NZD", "USD"), ("USDCAD", "USD", "CAD"), ("USDCHF", "USD", "CHF"),
    ("USDJPY", "USD", "JPY"),
    ("EURGBP", "EUR", "GBP"), ("EURAUD", "EUR", "AUD"), ("EURNZD", "EUR", "NZD"),
    ("EURCAD", "EUR", "CAD"), ("EURCHF", "EUR", "CHF"), ("EURJPY", "EUR", "JPY"),
    ("GBPAUD", "GBP", "AUD"), ("GBPNZD", "GBP", "NZD"), ("GBPCAD", "GBP", "CAD"),
    ("GBPCHF", "GBP", "CHF"), ("GBPJPY", "GBP", "JPY"),
    ("AUDNZD", "AUD", "NZD"), ("AUDCAD", "AUD", "CAD"), ("AUDCHF", "AUD", "CHF"),
    ("AUDJPY", "AUD", "JPY"),
    ("NZDCAD", "NZD", "CAD"), ("NZDCHF", "NZD", "CHF"), ("NZDJPY", "NZD", "JPY"),
    ("CADCHF", "CAD", "CHF"), ("CADJPY", "CAD", "JPY"),
    ("CHFJPY", "CHF", "JPY"),
]


def _smooth(series: pd.Series, ma_len: int, ma_type: str) -> pd.Series:
    return series.ewm(span=ma_len, adjust=False).mean() if ma_type == "EMA" \
        else series.rolling(ma_len).mean()


def compute_tf_strength(closes: dict, lookback: int, ma_len: int, ma_type: str,
                        display: str = "MA", roc_len: int = 1) -> dict:
    """closes: {pair: close-price Series (datetime-indexed)}.
    Returns {currency: latest smoothed strength}."""
    close_df = pd.DataFrame(closes).sort_index()
    if len(close_df) < lookback + 2:
        return {}
    z = (close_df - close_df.rolling(lookback).mean()) / close_df.rolling(lookback).std(ddof=0)
    z = z.dropna(how="all")
    if len(z) < max(ma_len, roc_len) + 1:
        return {}
    out = {}
    for ccy in CURRENCIES:
        cols = []
        for pair, base, quote in CSM_PAIRS:
            if pair not in z.columns:
                continue
            sign = 1.0 if base == ccy else (-1.0 if quote == ccy else 0.0)
            if sign == 0.0:
                continue
            cols.append(sign * z[pair])
        if not cols:
            continue
        ser = pd.concat(cols, axis=1).mean(axis=1)     # NaNs skipped per bar
        ser = _smooth(ser, ma_len, ma_type)
        if display == "ROC":
            ser = ser.diff(max(1, roc_len))
        out[ccy] = float(ser.iloc[-1])
    return out


def update_csm(cfg: dict, store, mt5=None, demo: bool = False,
               verbose: bool = False, force: bool = False):
    """Refresh the CSM snapshot: pull the 28 pairs for every configured TF,
    compute strengths + per-pair TF states + cross-TF alignment + transitions,
    and store the snapshot.  Throttled by csm.min_interval_seconds."""
    from .data import mt5_client
    from .util import jsonable

    csm_cfg = cfg.get("csm", {})
    if not csm_cfg.get("enabled", True):
        return None
    throttle = max(10, int(csm_cfg.get("min_interval_seconds", 30)))
    prev = store.load_csm()
    if prev and not force and time.time() - prev.get("generated_at", 0) < throttle:
        return prev

    lookback = int(csm_cfg.get("lookback", 20))
    ma_len = int(csm_cfg.get("ma_length", 10))
    ma_type = str(csm_cfg.get("ma_type", "SMA"))
    display = str(csm_cfg.get("display", "MA"))
    roc_len = max(1, int(csm_cfg.get("roc_len", 1)))
    bars = int(csm_cfg.get("bars", 400))
    min_pairs = int(csm_cfg.get("min_pairs", 10))
    tfs = cfg.get("timeframes", [])

    strength = {}       # tf -> {ccy: value}
    pair_states = {}    # pair -> {tf: "up"|"down"}
    pairs_used = 0

    for tf in tfs:
        closes = {}
        for pair, base, quote in CSM_PAIRS:
            df = None
            try:
                df = mt5_client.fetch_ohlcv(pair, tf, bars, demo=demo,
                                            cfg_mt5=cfg.get("mt5", {}), mt5=mt5)
                store.upsert_candles(df, pair, tf)
            except Exception:
                df = None
            if df is None or len(df) < lookback + 2:
                df = store.load_candles(pair, tf)     # fall back to stored bars
            if df is None or len(df) < lookback + 2:
                continue
            closes[pair] = df.set_index("time")["close"]

        if len(closes) < min_pairs:
            if verbose:
                print(f"  [csm] {tf}: only {len(closes)}/{len(CSM_PAIRS)} pairs — skipped")
            continue
        pairs_used = max(pairs_used, len(closes))

        tf_strength = compute_tf_strength(closes, lookback, ma_len, ma_type,
                                          display, roc_len)
        if not tf_strength:
            continue
        strength[tf] = tf_strength
        for pair, base, quote in CSM_PAIRS:
            if base in tf_strength and quote in tf_strength:
                pair_states.setdefault(pair, {})[tf] = \
                    "up" if tf_strength[base] > tf_strength[quote] else "down"

    if not strength:
        return None

    # carry over TFs that failed this round (e.g. transient MT5 fetch issues)
    # so a partial refresh never drops a timeframe that had data before
    prev_strength = (prev or {}).get("strength", {})
    prev_states = (prev or {}).get("pair_states", {})
    for tf, s in prev_strength.items():
        if tf not in strength:
            strength[tf] = s
            for pair, tfmap in prev_states.items():
                if tf in tfmap:
                    pair_states.setdefault(pair, {})[tf] = tfmap[tf]

    # cross-TF alignment per pair
    aligned = {}
    for pair, states in pair_states.items():
        vals = list(states.values())
        if vals and all(v == "up" for v in vals):
            aligned[pair] = "up"
        elif vals and all(v == "down" for v in vals):
            aligned[pair] = "down"
        else:
            aligned[pair] = None          # mixed / partial

    # transitions vs the previous snapshot
    transitions = []
    prev_aligned = (prev or {}).get("aligned", {})
    for pair, cur in aligned.items():
        p = prev_aligned.get(pair)
        if cur and cur != p:
            transitions.append({"pair": pair, "dir": cur, "prev": p,
                                "at": int(time.time())})

    payload = {
        "generated_at": int(time.time()),
        "strength": strength,
        "pair_states": pair_states,
        "aligned": aligned,
        "transitions": transitions,
        "pairs_used": pairs_used,
        "display": display,
    }
    store.save_csm(jsonable(payload))
    if verbose:
        top_tf = tfs[0] if tfs else next(iter(strength))
        s = strength.get(top_tf, {})
        if s:
            best = max(s, key=s.get); worst = min(s, key=s.get)
            print(f"  [csm] {pairs_used} pairs · strongest {best} {s[best]:+.2f} · "
                  f"weakest {worst} {s[worst]:+.2f}")
    return payload
