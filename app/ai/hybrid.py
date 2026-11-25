"""Hybrid fusion: anchor on calibrated ML win-probability and apply bounded LLM risk adjustment."""


def fuse(setup: dict, ai_cfg: dict) -> dict:
    ml = setup.get("ml_prob")
    llm = setup.get("llm")
    direction = setup["direction"]
    want_bias = "bullish" if direction == "long" else "bearish"

    hyb = (ai_cfg or {}).get("hybrid", {})
    ml_w = float(hyb.get("ml_weight", 0.75))
    llm_w = float(hyb.get("llm_weight", 0.25))
    threshold = float(hyb.get("buy_threshold", 44))
    contra_penalty = float(hyb.get("contradiction_penalty", 5.0))
    avoid_override_ml = float(hyb.get("avoid_override_ml_prob", 0.42))
    min_adj = float(hyb.get("min_llm_adjustment", -8.0))
    max_adj = float(hyb.get("max_llm_adjustment", 5.0))

    has_adj = bool(llm) and llm.get("score_adjustment") is not None
    has_legacy_conf = bool(llm) and not has_adj and llm.get("confidence") is not None
    adj = 0.0
    if has_adj:
        adj = max(min_adj, min(max_adj, float(llm["score_adjustment"])))

    if ml is not None and has_adj:
        final = float(ml) * 100.0 + adj
        source = "ml+llm"
    elif ml is not None and has_legacy_conf:
        # backward-compatible path for pre-P2 cached/historical LLM dicts
        scores = [float(ml) * 100.0, float(llm["confidence"])]
        weights = [ml_w, llm_w]
        final = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        source = "ml+llm"
    elif ml is not None:
        final = float(ml) * 100.0
        source = "ml"
    elif has_adj:
        final = 50.0 + adj
        source = "llm"
    elif has_legacy_conf:
        final = float(llm["confidence"])
        source = "llm"
    else:
        # no AI available -> transparent confluence-based heuristic
        final = min(90.0, 35.0 + 8.0 * len(setup.get("confluences", []))
                    + (5.0 if setup.get("rr", 0) >= 2.0 else 0.0))
        source = "heuristic"

    aligned = True
    if llm and llm.get("bias") in ("bullish", "bearish") and llm["bias"] != want_bias:
        aligned = False
        final -= contra_penalty
    final = max(0.0, min(100.0, final))

    ml_strong = ml is not None and float(ml) >= avoid_override_ml
    if not aligned and not ml_strong:
        verdict = "AVOID"
    elif final >= threshold:
        verdict = "BUY" if direction == "long" else "SELL"
    else:
        verdict = "WAIT"

    llm_score_val = (
        round(adj, 1) if has_adj
        else (llm.get("confidence") if has_legacy_conf else None)
    )
    return {
        "final_score": round(final, 1),
        "verdict": verdict,
        "aligned": aligned,
        "score_source": source,
        "ml_score": round(ml * 100.0, 1) if ml is not None else None,
        "llm_score": llm_score_val,
        "llm_adjustment": round(adj, 1) if has_adj else None,
        "llm_quality": llm.get("quality") if llm else None,
        "llm_risk_flags": list(llm.get("risk_flags") or []) if llm else [],
    }
