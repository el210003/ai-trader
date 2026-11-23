"""Per-symbol context features for the ML model.

These features let the global model learn pair-specific quirks without
maintaining per-symbol models.  Dynamic features use only outcomes of
setups that formed strictly before the current one — no look-ahead.

Features:

  symbol_avg_atr_pct       mean ATR% across this symbol's history
  symbol_setup_density     high-vol / trending signal (inverse bars-per-ATR)
  symbol_avg_rr_realized   mean realized RR over recent setups (None on cold-start)
  symbol_recent_win_rate   rolling win rate over recent setups (None on cold-start)
"""
import pandas as pd


def _atr_series(df, period: int = 14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


class SymbolStats:
    """Compute per-symbol context features for ML training and inference."""

    def __init__(self, lookback_setups: int = 200, min_samples: int = 10,
                 atr_period: int = 14, static_window_bars: int = 500,
                 prior_weight: float = 10.0, prior_win_rate: float = 0.38,
                 prior_rr: float = 0.10):
        self.lookback = int(lookback_setups)
        self.min_samples = int(min_samples)
        self.atr_period = int(atr_period)
        self.static_window = int(static_window_bars)
        self.prior_weight = float(prior_weight)
        self.prior_win_rate = float(prior_win_rate)
        self.prior_rr = float(prior_rr)
        self._static_cache: dict = {}   # symbol -> {avg_atr_pct, setup_density}

    def invalidate(self, symbol=None):
        if symbol is None:
            self._static_cache.clear()
        else:
            self._static_cache.pop(symbol, None)

    # ------------------------------------------------------------ public
    def static_features(self, df, symbol: str) -> dict:
        """Trailing-window per-symbol volatility & density (causal when given
        a prefix slice; bounded window avoids multi-year regime drift)."""
        if symbol in self._static_cache:
            return self._static_cache[symbol]
        sub = df.iloc[-self.static_window:] if self.static_window > 0 and len(df) > self.static_window else df
        if len(sub) < 5:
            feats = {"symbol_avg_atr_pct": 0.0, "symbol_setup_density": 0.0}
        else:
            atr = _atr_series(sub, self.atr_period).dropna()
            atr_pct = (atr / sub["close"]).dropna()
            avg_atr_pct = float(atr_pct.mean() * 100.0) if len(atr_pct) else 0.0
            avg_atr = float(atr.mean()) if len(atr) else 0.0
            avg_close = float(sub["close"].mean())
            if avg_atr > 0 and avg_close > 0:
                avg_atr_rel = avg_atr / avg_close
                # inverse bars-per-ATR: high-vol pairs -> higher density
                density = 1.0 / max(avg_atr_rel * 1000.0, 1e-6)
            else:
                density = 0.0
            feats = {"symbol_avg_atr_pct": avg_atr_pct,
                     "symbol_setup_density": float(min(density, 100.0))}
        self._static_cache[symbol] = feats
        return feats

    def dynamic_features(self, history: list) -> dict:
        """Rolling win rate + realized RR from past resolved outcomes (no look-ahead:
        caller passes only outcomes already resolved at or before the current bar).

        Applies Bayesian shrinkage toward the global prior so small sample
        counts (`>= min_samples`) smoothly transition from cold-start instead of
        jumping noisily or staying None for 100% of live setups.
        """
        if len(history) < self.min_samples:
            return {"symbol_recent_win_rate": None,
                    "symbol_avg_rr_realized": None}

        recent = history[-self.lookback:]
        labels = [h["label"] for h in recent if h.get("label") is not None]
        rrs = [h["realized_rr"] for h in recent if h.get("realized_rr") is not None]

        w = self.prior_weight
        wr = ((sum(labels) + w * self.prior_win_rate) / (len(labels) + w)) if labels else None
        rr = ((sum(rrs) + w * self.prior_rr) / (len(rrs) + w)) if rrs else None
        return {
            "symbol_recent_win_rate": wr,
            "symbol_avg_rr_realized": rr,
        }

    def dynamic_features_live(self, store, symbol: str) -> dict:
        """Phase-2 feedback loop: rolling win rate + realized RR from RESOLVED
        live outcomes (WIN/LOSS only — same semantics as the training labels).
        All resolved outcomes formed in the past, so there is no look-ahead.
        None values on cold-start (< min_samples), matching training."""
        hist = store.outcome_history_for_symbol(symbol, limit=self.lookback)
        mapped = [{"label": 1 if h["result"] == "WIN" else 0,
                   "realized_rr": h["r_multiple"]} for h in hist]
        return self.dynamic_features(mapped)
