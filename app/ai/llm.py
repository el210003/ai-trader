"""LLM analyst: any OpenAI-compatible chat endpoint (OpenAI, Ollama, LM Studio...)."""
import hashlib
import json
import os
import re
import time

import requests

SYSTEM_PROMPT = """You are an elite Smart Money Concepts (SMC) forex trading analyst reviewing \
algorithmically generated trade setups.

Weigh: market structure (BOS/CHoCH), liquidity sweeps, premium/discount positioning, \
order blocks, fair value gaps, risk:reward, and the supplied ML win-probability.

Note: These setups are structural liquidity-sweep / zone-retest reversals (a prior \
liquidity sweep is a bullish/bearish reversal trigger, not a reason to oppose the setup). \
Be skeptical only when: RR is poor, the entry sits in the wrong half of the dealing range, \
higher-timeframe (HTF) trend opposes the direction, the move looks extended, or \
confluences are weak.

Respond with ONLY a JSON object (no markdown, no prose) using exactly this schema:
{"bias": "bullish|bearish|neutral",
 "confidence": <integer 0-100, your conviction in the setup direction>,
 "narrative": "<2-4 sentence professional analysis>",
 "invalidation": "<what specifically invalidates this setup>",
 "concerns": ["<risk factor>", ...],
 "verdict": "BUY|SELL|WAIT|AVOID"}

verdict meaning: BUY = take the long setup, SELL = take the short setup,
WAIT = setup lacks quality, AVOID = the trade goes against your read."""


class LLMAnalyzer:
    def __init__(self, cfg: dict, store=None):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.base_url = (self.cfg.get("base_url") or "").rstrip("/")
        self.model = self.cfg.get("model", "gpt-4o-mini")
        self.timeout = int(self.cfg.get("timeout", 15))
        self._key_env = self.cfg.get("api_key_env", "OPENAI_API_KEY")
        self._cache = {}   # key -> (timestamp, response dict)
        self._store = store

    @property
    def has_key(self) -> bool:
        return bool(os.getenv(self._key_env, ""))

    def _is_local(self) -> bool:
        return any(h in self.base_url for h in ("localhost", "127.0.0.1"))

    @property
    def usable(self) -> bool:
        # remote endpoints need a key; local endpoints (Ollama/LM Studio) do not
        return self.enabled and bool(self.base_url) and (self.has_key or self._is_local())

    def analyze_setup(self, context: dict):
        """Returns normalized dict or None on any failure (never raises).

        Caching: identical setups (same symbol/tf/direction/zone/levels) within
        ai.llm.cache_ttl seconds reuse the cached response — backed by SQLite
        when `store` is attached so worker processes share cached narratives."""
        if not self.usable:
            return None
        key = self._cache_key(context)
        ttl = max(0, int(self.cfg.get("cache_ttl", 900)))
        if ttl:
            if key in self._cache:
                ts, resp = self._cache[key]
                if time.time() - ts <= ttl:
                    out = dict(resp)
                    out["_cached"] = True
                    return out
            if self._store is not None:
                try:
                    db_resp = self._store.get_llm_cache(key, self.model, ttl)
                    if db_resp:
                        self._cache[key] = (time.time(), db_resp)
                        out = dict(db_resp)
                        out["_cached"] = True
                        return out
                except Exception:
                    pass
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv(self._key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": self.model,
            "temperature": float(self.cfg.get("temperature", 0.2)),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, default=str)},
            ],
        }
        try:
            resp = requests.post(f"{self.base_url}/chat/completions",
                                 headers=headers, json=body, timeout=self.timeout)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = self._parse(content)
            if parsed:
                parsed["model"] = self.model
                self._cache[key] = (time.time(), parsed)
                if len(self._cache) > 256:   # keep the cache bounded
                    oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:-128]
                    for k, _ in oldest:
                        self._cache.pop(k, None)
                if ttl and self._store is not None:
                    try:
                        self._store.put_llm_cache(key, self.model, parsed)
                    except Exception:
                        pass
            return parsed
        except Exception:
            return None

    def _cache_key(self, context: dict) -> str:
        s = context.get("setup", {})
        raw = json.dumps({
            "s": context.get("symbol"), "t": context.get("timeframe"),
            "d": s.get("direction"), "e": s.get("entry"),
            "sl": s.get("stop_loss"), "tp": s.get("take_profit"),
            "z": (s.get("entry_zone") or {}).get("origin_time"),
            "n": len(s.get("confluences", [])),
        }, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()

    @staticmethod
    def _parse(text: str):
        # strip markdown fences, extract the outermost JSON object
        text = re.sub(r"```(?:json)?", "", text or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None

        bias = str(data.get("bias", "neutral")).lower()
        if bias not in ("bullish", "bearish", "neutral"):
            bias = "neutral"
        try:
            conf = max(0, min(100, int(float(data.get("confidence", 50)))))
        except (TypeError, ValueError):
            conf = 50
        verdict = str(data.get("verdict", "WAIT")).upper()
        if verdict not in ("BUY", "SELL", "WAIT", "AVOID"):
            verdict = "WAIT"
        concerns = data.get("concerns") or []
        if not isinstance(concerns, list):
            concerns = [str(concerns)]

        return {
            "bias": bias,
            "confidence": conf,
            "narrative": str(data.get("narrative", ""))[:1200],
            "invalidation": str(data.get("invalidation", ""))[:500],
            "concerns": [str(c)[:200] for c in concerns][:6],
            "verdict": verdict,
            "model": data.get("_model", ""),
        }
