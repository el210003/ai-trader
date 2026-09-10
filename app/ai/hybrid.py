"""Hybrid fusion: combine ML win-probability and LLM conviction into a final score + verdict."""


def fuse(setup: dict, ai_cfg: dict) -> dict:
    ml = setup.get("ml_prob")
    llm = setup.get("llm")
    direction = setup["direction"]
    want_bias = "bullish" if direction == "long" else "bearish"

    hyb = (ai_cfg or {}).get("hybrid", {})
    ml_w = float(hyb.get("ml_weight", 0.6))
    llm_w = float(hyb.get("llm_weight", 0.4))
    threshold = float(hyb.get("buy_threshold", 60))

    scores, weights = [], []
    if ml is not None:
        scores.append(float(ml) * 100.0)
        weights.append(ml_w)
    if llm and llm.get("confidence") is not None:
        scores.append(float(llm["confidence"]))
        weights.append(llm_w)

    if scores:
        final = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        source = "ml+llm" if len(scores) == 2 else ("ml" if ml is not None else "llm")
    else:
        # no AI available -> transparent confluence-based heuristic
        final = min(90.0, 35.0 + 8.0 * len(setup.get("confluences", []))
                    + (5.0 if setup.get("rr", 0) >= 2.0 else 0.0))
        source = "heuristic"

    aligned = True
    if llm and llm.get("bias") in ("bullish", "bearish") and llm["bias"] != want_bias:
        aligned = False
        final -= 20.0
    final = max(0.0, min(100.0, final))

    if not aligned:
        verdict = "AVOID"
    elif final >= threshold:
        verdict = "BUY" if direction == "long" else "SELL"
    else:
        verdict = "WAIT"

    return {
        "final_score": round(final, 1),
        "verdict": verdict,
        "aligned": aligned,
        "score_source": source,
        "ml_score": round(ml * 100.0, 1) if ml is not None else None,
        "llm_score": llm.get("confidence") if llm else None,
    }
