"""Feature engineering: turn a setup + SMC context into a numeric vector.

11 setup-specific features + 4 per-symbol context features that let the
global model learn pair-specific quirks without per-symbol models.
"""
import math

import pandas as pd

FEATURES = [
    # ----- setup-specific (11) -----
    "rr",                  # risk:reward of the setup
    "atr_pct",             # volatility (ATR / price * 100)
    "range_position",      # entry position in the dealing range (0=bottom, 1=top)
    "sweep_depth_atr",     # sweep wick beyond the level (ATR units)
    "sweep_recency",       # 0 = sweep just happened, 1 = old/none
    "confluence_count",    # number of SMC confluences
    "zone_freshness",      # 1 = zone just formed, 0 = old
    "is_order_block",      # 1 if entry zone is an order block (vs FVG)
    "trend_alignment",     # 1 if structure trend matches direction
    "hour_sin",            # time-of-day encoding (session effects)
    "hour_cos",

    # ----- per-symbol context (4) -----
    "symbol_avg_atr_pct",      # mean volatility of this pair (near-constant per symbol)
    "symbol_setup_density",    # high-vol / trending signal (inverse bars-per-ATR)
    "symbol_recent_win_rate",  # rolling win rate over recent setups (None on cold-start)
    "symbol_avg_rr_realized",  # mean realized RR on this pair (None on cold-start)

    # ----- MTF confluence (4) — H1/H4 context projected onto the entry TF -----
    "htf_trend_align",         # mean(1 HTF trend matches direction, 0 opposed); 0.5 = no ctx
    "htf_pd_alignment",        # mean(1 entry in correct HTF premium/discount half)
    "entry_in_htf_zone",       # 1 entry zone overlaps/near a same-direction HTF zone
    "htf_tp_distance_atr",     # entry -> nearest HTF liquidity pool (ATR units, cap 20)
]


def make_features(df, setup: dict, smc: dict, cfg: dict,
                  symbol_static: dict = None,
                  symbol_dynamic: dict = None,
                  htf_ctx: dict = None) -> dict:
    """Build the full feature dict for one setup.

    Args:
        df: candle history (for ATR% and time-of-day)
        setup: the setup dict
        smc: the SMC context dict
        cfg: smc config
        symbol_static: optional SymbolStats.static_features() output
        symbol_dynamic: optional SymbolStats.dynamic_features() output
    """
    n = len(df)
    atr_v = smc["atr"] or 1e-9
    close = smc["last_close"]
    direction = setup["direction"]
    want_side = "sellside" if direction == "long" else "buyside"
    sweep_lb = int(cfg.get("sweep_lookback_bars", 30))
    max_age = max(1, int(cfg.get("ob_max_age_bars", 300)))

    aligned_sweeps = [s for s in smc["sweeps"] if s["side"] == want_side
                      and (n - 1) - s["index"] <= sweep_lb]

    depth = 0.0
    if aligned_sweeps:
        depths = []
        for s in aligned_sweeps:
            if s["side"] == "sellside":
                wick = float(s["level"] - df["low"].iat[s["index"]])
            else:
                wick = float(df["high"].iat[s["index"]] - s["level"])
            depths.append(max(wick, 0.0) / atr_v)
        depth = max(depths)

    recency = 1.0
    if aligned_sweeps:
        last = aligned_sweeps[-1]
        recency = min(1.0, (n - 1 - last["index"]) / max(1, sweep_lb))

    zone_age = n - 1 - setup["entry_zone"]["origin_index"]
    freshness = max(0.0, 1.0 - zone_age / max_age)

    ts = pd.to_datetime(df["time"].iat[-1], unit="s", utc=True)
    hour = ts.hour + ts.minute / 60.0

    # ---- MTF confluence metrics (computed by the setup builder, stored on the setup)
    htf = setup.get("htf_metrics") or {}
    trend_align = htf.get("trend_align")
    pd_align = htf.get("pd_alignment")

    # entry -> nearest HTF liquidity pool in the profit direction (ATR units)
    entry = float(setup["entry"])
    cap = 20.0
    tp_dist = cap
    if htf_ctx:
        want_pools = "buyside" if direction == "long" else "sellside"
        pools = []
        for c in htf_ctx.values():
            pools.extend(c.get(want_pools, []))
        if direction == "long":
            beyond = [p for p in pools if p > entry]
            if beyond:
                tp_dist = min(beyond) - entry
        else:
            beyond = [p for p in pools if p < entry]
            if beyond:
                tp_dist = entry - max(beyond)

    return {
        "rr": float(setup["rr"]),
        "atr_pct": float(atr_v / close * 100.0),
        "range_position": float(setup["range_position"]),
        "sweep_depth_atr": float(depth),
        "sweep_recency": float(recency),
        "confluence_count": float(len(setup["confluences"])),
        "zone_freshness": float(freshness),
        "is_order_block": 1.0 if setup["entry_zone"]["type"] == "order_block" else 0.0,
        "trend_alignment": 1.0 if setup.get("trend_aligned") else 0.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2 * math.pi * hour / 24.0),
        "symbol_avg_atr_pct": float((symbol_static or {}).get("symbol_avg_atr_pct", 0.0)),
        "symbol_setup_density": float((symbol_static or {}).get("symbol_setup_density", 0.0)),
        "symbol_recent_win_rate": (symbol_dynamic or {}).get("symbol_recent_win_rate"),
        "symbol_avg_rr_realized": (symbol_dynamic or {}).get("symbol_avg_rr_realized"),
        "htf_trend_align": float(trend_align) if trend_align is not None else 0.5,
        "htf_pd_alignment": float(pd_align) if pd_align is not None else 0.5,
        "entry_in_htf_zone": float(htf.get("in_htf_zone") or 0.0),
        "htf_tp_distance_atr": float(min(cap, tp_dist / atr_v)),
    }


def feature_vector(feats: dict):
    """Numeric vector aligned with FEATURES. None (cold-start) is imputed
    with a neutral prior: 0.5 for win rate, 1.0 for realized RR."""
    out = []
    for f in FEATURES:
        v = feats.get(f)
        if v is None:
            if f == "symbol_recent_win_rate":
                out.append(0.5)
            elif f == "symbol_avg_rr_realized":
                out.append(1.0)
            elif f in ("htf_trend_align", "htf_pd_alignment"):
                out.append(0.5)
            elif f == "htf_tp_distance_atr":
                out.append(20.0)
            else:
                out.append(0.0)
        else:
            out.append(float(v))
    return out
