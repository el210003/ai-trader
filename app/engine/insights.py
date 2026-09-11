"""Insight engine (tier 1) + feature discrimination report (tier 2).

Scans resolved outcomes + journaled setup features and emits actionable,
evidence-backed recommendations.  NEVER auto-applies anything — every insight
carries its sample size, and buckets below MIN_BUCKET resolved outcomes are
suppressed so the engine never optimizes noise.

Insights cover: SL placement (post_loss_tp_hit), TP reachability (MFE of
expired trades), min_rr pricing, dead builder triggers, verdict-threshold
miscalibration, LLM value, losing regimes, and horizon length.
"""
import json
import time
from collections import defaultdict

from ..ai.features import FEATURES

MIN_BUCKET = 20          # minimum resolved samples per bucket to report
MIN_TERCILE = 12         # minimum resolved samples per feature tercile


def _load_features(r: dict) -> dict:
    if "features_dict" not in r:
        try:
            p = json.loads(r["payload"]) if r.get("payload") else {}
        except Exception:
            p = {}
        r["features_dict"] = p.get("features") or {}
        r["llm_info"] = {"llm_score": p.get("llm_score"), "aligned": p.get("aligned")}
    return r["features_dict"]


def _terciles(rs, key):
    """Split resolved rows into terciles by a feature value.
    Returns (low, mid, high) win rates + ns, or None when insufficient
    samples or the feature has too little variation to split meaningfully."""
    vals = [(float(_load_features(r).get(key) or 0.0), r) for r in rs]
    vals = [v for v in vals if v[0] is not None]
    if len(vals) < MIN_TERCILE * 3:
        return None
    if len({v for v, _ in vals}) < 3:
        return None          # no variation in this sample -> cannot discriminate
    vals.sort(key=lambda t: t[0])
    third = len(vals) // 3
    groups = (vals[:third], vals[third:2 * third], vals[2 * third:])
    out = []
    for g in groups:
        wins = sum(1 for _, r in g if r["result"] == "WIN")
        out.append({"win_rate": round(wins / len(g), 3), "n": len(g)})
    return out


def _wr(rs):
    return round(sum(1 for r in rs if r["result"] == "WIN") / len(rs), 3) if rs else None


def _exp(rs):
    vals = [r["r_multiple"] for r in rs if r.get("r_multiple") is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _insight(severity, title, finding, suggest=None, evidence=None, n=None):
    return {"severity": severity, "title": title, "finding": finding,
            "suggest": suggest or {}, "evidence": evidence or {}, "n": n}


def generate_insights(rows: list, cfg: dict) -> list:
    resolved = [r for r in rows if r["result"] in ("WIN", "LOSS")]
    losers = [r for r in resolved if r["result"] == "LOSS"]
    expired_open = [r for r in rows if r["result"] == "EXPIRED_OPEN"]
    out = []

    # 1 ------------------------------------------------- SL placement
    with_hit = [r for r in losers if r.get("post_loss_tp_hit") == 1]
    if len(losers) >= MIN_BUCKET:
        frac = len(with_hit) / len(losers)
        cur = cfg.get("smc", {}).get("sl_buffer_atr", 0.25)
        if frac >= 0.35:
            out.append(_insight(
                "action", "Stop losses are too tight",
                f"{len(with_hit)}/{len(losers)} losers ({frac:.0%}) saw price reach TP after "
                f"stopping out — the edge exists but the stop is inside the noise.",
                {"config_key": "smc.sl_buffer_atr", "current": cur,
                 "proposed": round(cur + 0.15, 2)},
                {"post_loss_tp_hit_rate": round(frac, 3), "losers": len(losers)}, len(losers)))
        elif frac <= 0.15:
            out.append(_insight(
                "info", "Stop placement healthy",
                f"Only {frac:.0%} of losers recovered to TP after SL — stops are not "
                f"the bottleneck.", n=len(losers)))

    # 2 ------------------------------------------------- TP reachability
    if len(expired_open) >= MIN_BUCKET:
        mfes = sorted(r["mfe_r"] for r in expired_open if r.get("mfe_r") is not None)
        med_mfe = mfes[len(mfes) // 2] if mfes else None
        rrs = sorted(r["rr"] for r in rows if r.get("rr"))
        med_rr = rrs[len(rrs) // 2] if rrs else None
        if med_mfe is not None and med_rr and med_mfe < 0.6 * med_rr:
            out.append(_insight(
                "action", "Take-profits are too far",
                f"Expired trades reached a median of {med_mfe:.2f}R while the median "
                f"planned RR is {med_rr:.2f}R — TP systematically outruns the move.",
                {"config_key": "smc.min_rr", "current": cfg.get("smc", {}).get("min_rr", 1.5),
                 "proposed": max(1.0, round(med_mfe * 1.2, 2))},
                {"median_mfe_r": med_mfe, "median_planned_rr": med_rr}, len(expired_open)))

    # 3 ------------------------------------------------- min_rr pricing
    buckets = {"1.5–2R": [], "2–3R": [], "3–4R": [], "4R+": []}
    for r in resolved:
        rr = r.get("rr") or 0
        k = "1.5–2R" if rr < 2 else "2–3R" if rr < 3 else "3–4R" if rr < 4 else "4R+"
        buckets[k].append(r)
    solid = {k: _exp(v) for k, v in buckets.items() if len(v) >= MIN_BUCKET}
    if len(solid) >= 2:
        lo_key = min(solid, key=solid.get)
        hi_keys = [k for k, v in solid.items() if v is not None and v > 0.2]
        if solid[lo_key] is not None and solid[lo_key] < 0 and hi_keys:
            cur = cfg.get("smc", {}).get("min_rr", 1.5)
            proposed = {"1.5–2R": 2.0, "2–3R": 3.0, "3–4R": 4.0}.get(lo_key, cur)
            if proposed > cur:
                out.append(_insight(
                    "action", f"Low-RR setups lose money ({lo_key})",
                    f"Expectancy {solid[lo_key]}R in {lo_key} vs "
                    f"{ {k: solid[k] for k in hi_keys} } — raise the floor.",
                    {"config_key": "smc.min_rr", "current": cur, "proposed": proposed},
                    {"expectancy_by_rr": solid}, sum(len(v) for v in buckets.values())))

    # 4 --------------------------------- verdict threshold + ML calibration
    bands = {"<60": [], "60–70": [], "70–80": [], "80+": []}
    for r in resolved:
        sc = r.get("score") or 0
        k = "<60" if sc < 60 else "60–70" if sc < 70 else "70–80" if sc < 80 else "80+"
        bands[k].append(r)
    solid_bands = {k: _exp(v) for k, v in bands.items() if len(v) >= MIN_BUCKET}
    if "60–70" in solid_bands and solid_bands["60–70"] is not None and solid_bands["60–70"] < 0:
        better = [k for k, v in solid_bands.items() if v is not None and v > 0.15 and k != "60–70"]
        if better:
            out.append(_insight(
                "action", "Buy threshold too permissive",
                f"60–70 score band has expectancy {solid_bands['60–70']}R while "
                f"{ {k: solid_bands[k] for k in better} } — raise the bar.",
                {"config_key": "ai.hybrid.buy_threshold", "current": 60, "proposed": 70},
                {"expectancy_by_band": solid_bands}, sum(len(v) for v in bands.values())))

    cal_buckets = defaultdict(lambda: {"pred": [], "real": []})
    for r in resolved:
        mp = r.get("ml_prob")
        if mp is None:
            continue
        lo = round(float(mp) * 10) / 10
        cal_buckets[f"{lo:.1f}"]["pred"].append(float(mp))
        cal_buckets[f"{lo:.1f}"]["real"].append(1 if r["result"] == "WIN" else 0)
    for k, v in cal_buckets.items():
        if len(v["real"]) >= MIN_BUCKET:
            pred, real = sum(v["pred"]) / len(v["pred"]), sum(v["real"]) / len(v["real"])
            if pred - real > 0.12:
                out.append(_insight(
                    "warn", f"ML overconfident in {k} bucket",
                    f"Predicted {pred:.0%} but realized {real:.0%} over {len(v['real'])} "
                    f"trades — model calibration drifted; consider retraining or raising "
                    f"ai.llm.min_ml_prob.",
                    {"config_key": "ai.llm.min_ml_prob", "current": cfg.get("ai", {})
                     .get("llm", {}).get("min_ml_prob", 0.30), "proposed": round(min(0.6, float(k) + 0.1), 2)},
                    {"predicted": round(pred, 3), "realized": round(real, 3)}, len(v["real"])))

    # 5 ------------------------------------------------- LLM value
    aligned = [r for r in resolved if _load_features(r) and r["llm_info"]
               and r["llm_info"].get("aligned") is True]
    contra = [r for r in resolved if r["llm_info"] and r["llm_info"].get("aligned") is False]
    if len(aligned) >= MIN_BUCKET and len(contra) >= MIN_BUCKET:
        wa, wc = _wr(aligned), _wr(contra)
        if wa is not None and wc is not None and wa - wc >= 0.10:
            out.append(_insight(
                "info", "LLM contradiction filter is earning its keep",
                f"Win rate {wa:.0%} when the LLM agrees vs {wc:.0%} when it "
                f"contradicts — keep the −20 penalty.", n=len(aligned) + len(contra)))
        elif abs((wa or 0) - (wc or 0)) < 0.05:
            out.append(_insight(
                "warn", "LLM adds no measurable value",
                f"Win rate {wa:.0%} (agree) vs {wc:.0%} (contradict) — consider "
                f"lowering hybrid.llm_weight or raising the ML gate.",
                {"config_key": "ai.hybrid.llm_weight",
                 "current": cfg.get("ai", {}).get("hybrid", {}).get("llm_weight", 0.4),
                 "proposed": 0.25}, n=len(aligned) + len(contra)))

    # 6 ------------------------------------------------- losing regimes
    def by(keyfn, label):
        groups = defaultdict(list)
        for r in resolved:
            groups[keyfn(r)].append(r)
        for k, v in groups.items():
            if len(v) >= MIN_BUCKET:
                e = _exp(v)
                if e is not None and e < -0.15:
                    out.append(_insight(
                        "warn", f"Losing regime: {label} {k}",
                        f"Expectancy {e}R over {len(v)} resolved trades — consider "
                        f"avoiding this {label}.", n=len(v)))
    by(lambda r: r.get("symbol"), "symbol")
    by(lambda r: (r.get("direction") or "?"), "direction")
    by(lambda r: time.gmtime(r["formed_at"]).tm_hour // 6 * 6, "session block")

    # 7 ------------------------------------------------- horizon too short
    filled = [r for r in rows if r.get("filled")]
    if len(filled) >= MIN_BUCKET and len(filled) and rows:
        share = len(expired_open) / max(1, len(filled))
        if share > 0.35:
            h = cfg.get("ai", {}).get("ml", {}).get("label_horizon_bars", 96)
            out.append(_insight(
                "info", "Horizon may be too short",
                f"{share:.0%} of filled trades expired without TP/SL within the "
                f"horizon — outcomes stay unresolved and labels stay noisy.",
                {"config_key": "ai.ml.label_horizon_bars", "current": h,
                 "proposed": int(h * 1.5)}, n=len(filled)))

    return out


def feature_report(rows: list) -> list:
    """Tier 2: per-feature tercile win rates -> powerful / weak / dead verdicts."""
    resolved = [r for r in rows if r["result"] in ("WIN", "LOSS")]
    report = []
    for feat in FEATURES:
        t = _terciles(resolved, feat)
        if t is None:
            report.append({"feature": feat, "verdict": "insufficient data",
                           "terciles": None, "spread": None})
            continue
        wrs = [x["win_rate"] for x in t]
        spread = round(max(wrs) - min(wrs), 3)
        verdict = ("powerful" if spread >= 0.15 else
                   "weak" if spread >= 0.07 else "dead")
        report.append({"feature": feat, "verdict": verdict, "terciles": t,
                       "spread": spread})
    report.sort(key=lambda x: -(x["spread"] if x["spread"] is not None else -1))
    return report
