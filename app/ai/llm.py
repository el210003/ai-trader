"""LLM analyst: any OpenAI-compatible chat endpoint (OpenAI, Ollama, LM Studio...)."""
import hashlib
import json
import os
import re
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from ..logsetup import debug

SYSTEM_PROMPT = """You are an elite Smart Money Concepts (SMC) forex risk auditor reviewing \
algorithmically generated trade setups whose baseline probability is already scored by a \
calibrated ML model (ml_win_probability).

Your job is NOT to re-estimate raw win probability from scratch, but to audit structural and \
execution quality and apply a bounded qualitative adjustment (-8 to +5 score points):
- Positive (+1 to +5): clean structure alignment, fresh unmitigated zone, unobstructed path to \
  TP (no opposing HTF OB/FVG in the way), supportive CSM/session, and tight stop width.
- Zero (0): standard setup with balanced pros and cons.
- Negative (-1 to -8): concrete red flags such as opposing HTF OB/FVG blocking TP, excessive \
  stop_distance_atr (>3.5 ATR), entry in the wrong HTF dealing-range half, sweep exhaustion, \
  CSM opposition, or thin off-hours chop.

Note: These setups are structural liquidity-sweep / zone-retest reversals (a prior liquidity \
sweep in the setup's favor is a reversal trigger, not a reason to oppose the trade).

Respond with ONLY a JSON object (no markdown, no prose) using exactly this schema:
{"bias": "bullish|bearish|neutral",
 "quality": "high|medium|low",
 "score_adjustment": <integer -8 to 5>,
 "risk_flags": ["<snake_case_flag>", ...],
 "narrative": "<2-3 sentence concise structural & risk analysis>",
 "invalidation": "<what specifically invalidates this setup>",
 "concerns": ["<specific risk factor>", ...]}"""

PROMPT_VERSION = hashlib.md5(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]


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
        self._cache_err_logged = False
        self._stats_lock = threading.Lock()
        self._stats = {
            "calls": 0,
            "succeeded": 0,
            "cache_hits_mem": 0,
            "cache_hits_db": 0,
            "timeouts": 0,
            "http_errors": 0,
            "parse_errors": 0,
            "total_latency_ms": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def _bump(self, **increments):
        with self._stats_lock:
            for k, v in increments.items():
                self._stats[k] = self._stats.get(k, 0) + v

    def stats(self) -> dict:
        with self._stats_lock:
            s = dict(self._stats)
        calls = s["calls"]
        s["avg_latency_ms"] = round(s["total_latency_ms"] / calls, 1) if calls else 0.0
        s["model"] = self.model if self.usable else None
        s["prompt_version"] = PROMPT_VERSION
        return s

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
                    self._bump(cache_hits_mem=1)
                    out = dict(resp)
                    out["_cached"] = True
                    return out
            if self._store is not None:
                try:
                    db_resp = self._store.get_llm_cache(key, self.model, ttl)
                    if db_resp:
                        self._cache[key] = (time.time(), db_resp)
                        self._bump(cache_hits_db=1)
                        out = dict(db_resp)
                        out["_cached"] = True
                        return out
                except Exception as e:
                    if not self._cache_err_logged:
                        self._cache_err_logged = True
                        debug(f"[llm] sqlite cache read failed: {e}")
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
        max_tokens = int(self.cfg.get("max_tokens", 2048) or 0)
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        if self.cfg.get("json_mode", False):
            body["response_format"] = {"type": "json_object"}
        if self.cfg.get("reasoning_split", False):
            body["reasoning_split"] = True
        t0 = time.time()
        self._bump(calls=1)
        try:
            resp = self._session.post(f"{self.base_url}/chat/completions",
                                      headers=headers, json=body, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
            usage = payload.get("usage") or {}
            choice = payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = (choice.get("message") or {}).get("content") or ""
            parsed = self._parse(content)
            lat_ms = int((time.time() - t0) * 1000)
            self._bump(
                total_latency_ms=lat_ms,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )
            if parsed:
                self._bump(succeeded=1)
                parsed["model"] = self.model
                self._cache[key] = (time.time(), parsed)
                if len(self._cache) > 256:   # keep the cache bounded
                    oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:-128]
                    for k, _ in oldest:
                        self._cache.pop(k, None)
                if ttl and self._store is not None:
                    try:
                        self._store.put_llm_cache(key, self.model, parsed)
                    except Exception as e:
                        if not self._cache_err_logged:
                            self._cache_err_logged = True
                            debug(f"[llm] sqlite cache write failed: {e}")
            else:
                self._bump(parse_errors=1)
                if finish_reason == "length" and "<think>" in content.lower() and "</think>" not in content.lower():
                    debug(f"[llm] truncated inside <think> at max_tokens={max_tokens} from {self.model} ({lat_ms}ms) -- increase ai.llm.max_tokens")
                else:
                    debug(f"[llm] unparseable response from {self.model} ({lat_ms}ms, finish={finish_reason}): {content[:160]!r}")
            return parsed
        except requests.exceptions.Timeout as e:
            self._bump(timeouts=1, total_latency_ms=int((time.time() - t0) * 1000))
            debug(f"[llm] timeout ({self.model}, {self.timeout}s): {e}")
            return None
        except Exception as e:
            self._bump(http_errors=1, total_latency_ms=int((time.time() - t0) * 1000))
            debug(f"[llm] request failed ({self.model}): {e}")
            return None

    def _cache_key(self, context: dict) -> str:
        s = context.get("setup", {})
        ez = s.get("entry_zone") or {}
        ms = context.get("market_structure") or {}
        htf = context.get("htf_context") or {}
        mp = context.get("ml_win_probability")
        ed = s.get("entry_distance_atr")
        raw = json.dumps({
            "pv": PROMPT_VERSION,
            "s": context.get("symbol"), "t": context.get("timeframe"),
            "d": s.get("direction"), "k": s.get("setup_kind", "zone"),
            "e": s.get("entry"), "sl": s.get("stop_loss"), "tp": s.get("take_profit"),
            "z": ez.get("origin_time") if ez.get("origin_time") is not None else ez.get("zone_age_bars"),
            "n": len(s.get("confluences", [])),
            "ml": round(float(mp), 3) if mp is not None else None,
            "tr": ms.get("trend"),
            "htf": {k: v.get("trend") for k, v in htf.items() if isinstance(v, dict)},
            "csm": (context.get("csm") or {}).get("aligned_direction"),
            "ed": round(float(ed), 1) if ed is not None else None,
        }, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()

    @staticmethod
    def _parse(text: str):
        # strip <think>...</think> blocks and markdown fences, extract outermost JSON
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
        if "<think>" in text.lower():
            idx_brace = text.find("{")
            if idx_brace != -1:
                text = text[idx_brace:]
        text = re.sub(r"```(?:json)?", "", text).strip()
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

        raw_adj = data.get("score_adjustment")
        if raw_adj is not None:
            try:
                adj = max(-8, min(5, int(round(float(raw_adj)))))
            except (TypeError, ValueError):
                adj = 0
        elif data.get("confidence") is not None:
            # backward-compat mapping if a model emits legacy 0-100 confidence
            try:
                conf_v = float(data.get("confidence", 50))
                adj = max(-8, min(5, int(round((conf_v - 50.0) / 10.0))))
            except (TypeError, ValueError):
                adj = 0
        else:
            adj = 0

        quality = str(data.get("quality", "")).lower().strip()
        if quality not in ("high", "medium", "low"):
            quality = "high" if adj >= 2 else ("low" if adj <= -3 else "medium")

        raw_flags = data.get("risk_flags") or []
        if not isinstance(raw_flags, list):
            raw_flags = [str(raw_flags)]
        risk_flags = []
        for f in raw_flags:
            clean_f = re.sub(r"[^a-z0-9_]+", "_", str(f).lower()).strip("_")[:48]
            if clean_f and clean_f not in risk_flags:
                risk_flags.append(clean_f)
            if len(risk_flags) >= 6:
                break

        concerns = data.get("concerns") or []
        if not isinstance(concerns, list):
            concerns = [str(concerns)]

        return {
            "bias": bias,
            "quality": quality,
            "score_adjustment": adj,
            "risk_flags": risk_flags,
            "narrative": str(data.get("narrative", ""))[:1200],
            "invalidation": str(data.get("invalidation", ""))[:500],
            "concerns": [str(c)[:200] for c in concerns][:6],
            "model": data.get("_model", ""),
        }
