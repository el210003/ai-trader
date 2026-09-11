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

    def __init__(self, lookback_setups: int = 200, min_samples: int = 30,
                 atr_period: int = 14):
        self.lookback = int(lookback_setups)
        self.min_samples = int(min_samples)
        self.atr_period = int(atr_period)
        self._static_cache: dict = {}   # symbol -> {avg_atr_pct, setup_density}

    def invalidate(self, symbol=None):
        if symbol is None:
            self._static_cache.clear()
        else:
            self._static_cache.pop(symbol, None)

    # ------------------------------------------------------------ public
    def static_features(self, df, symbol: str) -> dict:
        """Time-invariant per-symbol features (avg volatility, density)."""
        if symbol in self._static_cache:
            return self._static_cache[symbol]
        if len(df) < 5:
            feats = {"symbol_avg_atr_pct": 0.0, "symbol_setup_density": 0.0}
        else:
            atr = _atr_series(df, self.atr_period).dropna()
            atr_pct = (atr / df["close"]).dropna()
            avg_atr_pct = float(atr_pct.mean() * 100.0) if len(atr_pct) else 0.0
            avg_atr = float(atr.mean()) if len(atr) else 0.0
            avg_close = float(df["close"].mean())
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
        """Rolling win rate + realized RR from past outcomes (no look-ahead:
        caller passes only outcomes that formed before the current setup).

        `history` items: {"formed_index", "label" (0|1), "realized_rr"}
        None values signal cold-start (< min_samples historical setups).
        """
        if len(history) < self.min_samples:
            return {"symbol_recent_win_rate": None,
                    "symbol_avg_rr_realized": None}

        recent = history[-self.lookback:]
        labels = [h["label"] for h in recent if h.get("label") is not None]
        rrs = [h["realized_rr"] for h in recent if h.get("realized_rr") is not None]

        return {
            "symbol_recent_win_rate": (sum(labels) / len(labels)) if labels else None,
            "symbol_avg_rr_realized": (sum(rrs) / len(rrs)) if rrs else None,
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
