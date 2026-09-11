"""Historical outcome labeling for ML training.

A setup is labeled 1 (win) if, after the entry limit order fills, price hits
TP before SL within the horizon; 0 (loss/timeout) otherwise. Same-bar SL+TP
counts as a loss (conservative).

The entry "fills" when price comes within the setup's fill_tolerance of the
entry level (retest_buffer_atr applied at build time). A setup that is not
filled within `entry_valid_bars` of the zone forming is NOT a training sample
(return None) — it never traded (matches the live resolver, no look-ahead).
"""


def label_setup(df, setup: dict, horizon: int, entry_valid_bars: int = 24):
    i = setup["formed_index"]
    direction = setup["direction"]
    entry, sl, tp = setup["entry"], setup["stop_loss"], setup["take_profit"]
    tol = float(setup.get("fill_tolerance") or 0.0)
    valid = int(entry_valid_bars)

    filled = False
    limit_start = i + 1
    limit_end = min(i + 1 + valid, len(df)) if valid > 0 else min(i + 1 + horizon, len(df))
    # fill window: if not filled within entry_valid_bars -> not a sample
    for k in range(i + 1, min(i + 1 + horizon, len(df))):
        hi = df["high"].iat[k]
        lo = df["low"].iat[k]
        if not filled:
            if direction == "long":
                if lo <= entry + tol:
                    filled = True
                elif k >= limit_end:
                    return None            # never filled within validity window
                else:
                    continue
            else:
                if hi >= entry - tol:
                    filled = True
                elif k >= limit_end:
                    return None
                else:
                    continue
        # in trade from this bar on (conservative: check SL first on same bar)
        if direction == "long":
            if lo <= sl:
                return 0
            if hi >= tp:
                return 1
        else:
            if hi >= sl:
                return 0
            if lo <= tp:
                return 1
    return 0 if filled else None  # never filled -> not a sample
