"""Fast training-replay context (option A).

`analyze()` re-runs every SMC detector over the whole prefix on every replay
step — O(steps x bars), the dominant training cost (~230s per pair-timeframe).
All detectors are chronological and emit bar-indexed events, so the full
series is analyzed ONCE and each step assembles its context by index filters
plus cheap derivations: O(bars) total, ~60x faster end-to-end.

Exactness (validated against analyze(prefix) on EURUSD M15, USDJPY H1,
BTCUSD M15; end-to-end replay rows bit-identical):
  - swings, structure events/trend, liquidity pools, sweeps, dealing range,
    atr, order blocks, fvgs: exact per-step replicas
  - rejection events: exact per-step replicas via RejectionStream +
    rejection_tail() (smc/rejection.py) — the step-varying ATR noise gate and
    inferred point size are applied per step, and the pools branch of the
    detector is inert (index-vs-epoch comparison), so one stream suffices.
    The returned list covers a tail window only, which is all the consumers
    read (setup arming is bounded by smc.rejection.trigger_lookback_bars).
Keep in sync with the detectors if their event semantics change.
"""
from typing import List

import numpy as np
import pandas as pd

from .structure import find_swings, detect_structure
from .liquidity import cluster_equal_levels, detect_sweeps
from .order_blocks import find_order_blocks, ob_view
from .fvg import find_fvgs, fvg_view
from .rejection import RejectionStream, rejection_tail, infer_point_by_bar
from ..engine.mtf import higher_timeframes, TF_MINUTES


class FastSMC:
    """analyze() once over the full series; per-step assembly via at(i)."""

    def __init__(self, df: pd.DataFrame, cfg: dict):
        self.df = df
        self.cfg = cfg
        lookback = int(cfg.get("swing_lookback", 3))
        self.swings = find_swings(df, lookback)
        self.events, _ = detect_structure(df, self.swings)

        self.tol_pct = float(cfg.get("eq_tolerance_pct", 0.0006))
        use_day_extremes = bool((cfg.get("rejection") or {}).get("use_day_extremes", True))
        self.sweeps = detect_sweeps(
            df, self.swings, tol_pct=self.tol_pct, use_day_extremes=use_day_extremes,
        )
        self.obs = find_order_blocks(df, self.events, int(cfg.get("ob_max_age_bars", 300)))
        self.fvgs = find_fvgs(df)
        self.ob_max = int(cfg.get("ob_max_age_bars", 300))
        self.fvg_max = int(cfg.get("fvg_max_age_bars", 200))

        # causal ATR series: ewm is bar-causal, so value at i == prefix ATR
        h, l, c = df["high"], df["low"], df["close"]
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr_period = int(cfg.get("atr_period", 14))
        self.atr_series = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean().to_numpy()

        # exact per-step rejection events: stream once per distinct point size
        # (practically always one; infer_point can drift on unusual quotes)
        self.point_by_bar = infer_point_by_bar(df)
        rj_cfg = cfg.get("rejection") or {}
        self._rej_window = max(2, int(rj_cfg.get("trigger_lookback_bars", 2))) + 16
        self._rej_streams = {}
        for b in np.unique(self.point_by_bar) if len(self.point_by_bar) else ():
            p = float(b)
            self._rej_streams[p] = RejectionStream(df, cfg, p)

    # ------------------------------------------------------------------
    def _cluster(self, levels: List[dict], side: str, tol: float,
                 min_touches: int = 2) -> List[dict]:
        """Same clustering as liquidity.cluster_equal_levels, with an
        early-exit on the price-sorted list."""
        want = "high" if side == "buyside" else "low"
        ls = sorted((s for s in levels if s["type"] == want), key=lambda s: s["price"])
        pools: List[dict] = []
        used = [False] * len(ls)
        for a in range(len(ls)):
            if used[a]:
                continue
            group = [ls[a]]
            used[a] = True
            for b in range(a + 1, len(ls)):
                if used[b]:
                    continue
                if abs(ls[b]["price"] - group[0]["price"]) <= tol:
                    group.append(ls[b])
                    used[b] = True
                elif ls[b]["price"] - group[0]["price"] > tol:
                    break
            if len(group) >= min_touches:
                pools.append({
                    "side": side,
                    "price": float(np.mean([g["price"] for g in group])),
                    "touches": len(group),
                    "last_time": int(max(g["time"] for g in group)),
                    "last_index": int(max(g["index"] for g in group)),
                    "last_confirmed_at": int(max(int(g.get("confirmed_at", g["index"])) for g in group)),
                    "members": [int(g["index"]) for g in group],
                })
        pools.sort(key=lambda p: p["price"])
        return pools

    def at(self, i: int) -> dict:
        """SMC context as of bar i (inclusive), equivalent to
        analyze(df[:i+1]) (rejections cover a tail window — all consumers
        read events within smc.rejection.trigger_lookback_bars of the tip)."""
        df, cfg = self.df, self.cfg
        close_i = float(df["close"].iloc[i])
        tol_i = close_i * self.tol_pct
        atr_i = float(self.atr_series[i])

        swings_i = [s for s in self.swings if s["confirmed_at"] <= i][-60:]
        vis = [s for s in self.swings if s["confirmed_at"] <= i]
        pools_i = (self._cluster(vis, "buyside", tol_i)
                   + self._cluster(vis, "sellside", tol_i))
        events_i = [e for e in self.events if e["index"] <= i]
        trend_i = events_i[-1]["direction"] if events_i else "neutral"
        sweeps_i = [s for s in self.sweeps if s["index"] <= i]

        st = self._rej_streams.get(float(self.point_by_bar[i]))
        if st is not None:
            trig_lb = max(2, int((cfg.get("rejection") or {}).get(
                "trigger_lookback_bars", 2)))
            arm_from = i + 1 - trig_lb          # builder breaks past this
            rej_i = [e for e in rejection_tail(st, i, atr_i, self._rej_window)
                     if e["index"] >= arm_from]
        else:
            rej_i = []

        # dealing range from the most recent confirmed swings (as of i)
        last_high = last_low = None
        for sw in swings_i:
            if sw["type"] == "high":
                last_high = sw
            else:
                last_low = sw
        if last_high is None or last_low is None:      # fallback: rolling extremes
            win = min(i + 1, 200)
            seg = df.iloc[i + 1 - win:i + 1]
            top, bottom = float(seg["high"].max()), float(seg["low"].min())
            hi_t = lo_t = int(seg["time"].iloc[-1])
            hi_i = lo_i = i
        else:
            top, bottom = last_high["price"], last_low["price"]
            hi_t, hi_i = last_high["time"], last_high["index"]
            lo_t, lo_i = last_low["time"], last_low["index"]
            if bottom > top:                            # defensive swap
                top, bottom = bottom, top
                hi_t, hi_i, lo_t, lo_i = lo_t, lo_i, hi_t, hi_i
        eq = (top + bottom) / 2.0
        span = top - bottom
        position = (close_i - bottom) / span if span > 0 else 0.5
        zone = "premium" if position > 0.5 else ("discount" if position < 0.5 else "equilibrium")
        dr = {"top": top, "bottom": bottom, "equilibrium": eq,
              "high_time": int(hi_t), "low_time": int(lo_t),
              "high_index": int(hi_i), "low_index": int(lo_i),
              "position": float(min(max(position, 0.0), 1.0)), "zone": zone}

        return {
            "trend": trend_i,
            "swings": swings_i,
            "events": events_i,
            "sweeps": sweeps_i,
            "rejections": rej_i,
            "swing_pivots": [],            # not consumed by setup/feature builders
            "liquidity_pools": pools_i,
            "dealing_range": dr,
            "order_blocks": ob_view(self.obs, i, self.ob_max),
            "fvgs": fvg_view(self.fvgs, i, self.fvg_max),
            "atr": atr_i,
            "last_close": close_i,
            "n_bars": i + 1,
        }


class FastHTFContext:
    """Fast twin of engine.mtf.build_htf_context for the training replay.

    The original re-runs smc.analyze over the whole HTF prefix every time an
    HTF bar closes (~40% of replay cost with MTF enabled). Here each HTF is
    analyzed once (FastSMC) and per-step contexts are assembled from exact
    fields only (trend, atr, dealing_range, OB/FVG fresh, pools) -- the
    rejection-event caveat does not apply because HTF context never uses
    rejections. Caching and skip rules mirror the original exactly.
    """

    def __init__(self, symbol: str, tf: str, cfg: dict, load_df):
        self.symbol, self.tf, self.cfg = symbol, tf, cfg
        self.load_df = load_df
        self.htfs = higher_timeframes(tf, cfg) if cfg.get("mtf", {}).get("enabled", True) else []
        self._full = {}        # htf -> full df (or None)
        self._t_close = {}     # htf -> sorted array of bar close times
        self._fast = {}        # htf -> FastSMC (or None when skipped)
        self.ctx_cache = {}    # (htf, last_closed_open_time) -> ctx

    def _prepare(self, htf: str):
        full = self.load_df(self.symbol, htf)
        self._full[htf] = full
        if full is None or len(full) < 60:
            self._fast[htf] = None
            return
        self._t_close[htf] = full["time"].values + TF_MINUTES.get(htf, 0) * 60
        self._fast[htf] = FastSMC(full, self.cfg["smc"])

    def at(self, up_to_time: int) -> dict:
        out = {}
        for htf in self.htfs:
            if htf not in self._fast:
                self._prepare(htf)
            fast = self._fast.get(htf)
            if fast is None:
                continue
            idx = int(np.searchsorted(self._t_close[htf], up_to_time, side="right")) - 1
            if idx < 59:                      # len(closed slice) < 60 -> skip
                continue
            key = (htf, int(self._full[htf]["time"].iat[idx]))
            cached = self.ctx_cache.get(key)
            if cached is not None:
                out[htf] = cached
                continue
            smc = fast.at(idx)
            rng = smc["dealing_range"]
            if not rng.get("equilibrium"):
                continue
            zones = []
            for ob in (smc["order_blocks"].get("fresh") or [])[:20]:
                zones.append({"type": "order_block", "direction": ob["direction"],
                              "top": float(ob["top"]), "bottom": float(ob["bottom"])})
            for fg in (smc["fvgs"].get("fresh") or [])[:20]:
                zones.append({"type": "fvg", "direction": fg["direction"],
                              "top": float(fg["top"]), "bottom": float(fg["bottom"])})
            pools = smc["liquidity_pools"]
            ctx = {
                "trend": smc["trend"],
                "atr": float(smc["atr"]),
                "equilibrium": float(rng["equilibrium"]),
                "zones": zones,
                "buyside": [float(p["price"]) for p in pools if p["side"] == "buyside"],
                "sellside": [float(p["price"]) for p in pools if p["side"] == "sellside"],
            }
            self.ctx_cache[key] = ctx
            out[htf] = ctx
        return out
