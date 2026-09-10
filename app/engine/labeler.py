"""Historical outcome labeling for ML training.

A setup is labeled 1 (win) if, after the entry limit order fills, price hits
TP before SL within the horizon; 0 (loss/timeout) otherwise. Same-bar SL+TP
counts as a loss (conservative).
"""


def label_setup(df, setup: dict, horizon: int):
    i = setup["formed_index"]
    direction = setup["direction"]
    entry, sl, tp = setup["entry"], setup["stop_loss"], setup["take_profit"]

    filled = False
    for k in range(i + 1, min(i + 1 + horizon, len(df))):
        hi = df["high"].iat[k]
        lo = df["low"].iat[k]
        if not filled:
            if direction == "long":
                if lo <= entry:
                    filled = True
                else:
                    continue
            else:
                if hi >= entry:
                    filled = True
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
