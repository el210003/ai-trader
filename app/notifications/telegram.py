"""Telegram Bot API notifier for market structure breaks (CHoCH & BOS)."""
import os
import time
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from ..data.store import Store
from ..logsetup import debug
from .choch_journal import record_choch_event

TELEGRAM_API = "https://api.telegram.org"


def _resolve_pair(
    section: dict,
    default_token_env: str = "TELEGRAM_BOT_TOKEN",
    default_chat_env: str = "TELEGRAM_CHAT_ID",
) -> Tuple[str, str]:
    token = str(section.get("bot_token") or "").strip()
    if not token:
        token = os.getenv(str(section.get("bot_token_env") or default_token_env), "").strip()
    chat_id = str(section.get("chat_id") or "").strip()
    if not chat_id:
        chat_id = os.getenv(str(section.get("chat_id_env") or default_chat_env), "").strip()
    return token, chat_id


def resolve_credentials(cfg: dict) -> Tuple[str, str]:
    """Resolve primary (bot_token, chat_id) from config.yaml with env-var fallback."""
    tg = cfg.get("telegram") or {}
    return _resolve_pair(tg)


def resolve_targets(cfg: dict) -> List[Dict]:
    """Resolve all configured Telegram bot targets (primary + `telegram.bots[]`).

    Each returned dict has:
      - name: display label ("default" for top-level credentials, or bot's `name`)
      - bot_token: resolved token string
      - chat_id: resolved chat_id string
      - target_prefix: "" for primary target (backwards-compatible DB keys),
        or "tg:<bot_id>:<chat_id>" for additional bots
      - events, timeframes, symbols, max_event_age_bars, quiet_hours, timeout
    """
    tg = cfg.get("telegram") or {}
    targets: List[Dict] = []
    seen_pairs = set()

    base_defaults = {
        "events": tg.get("events") or ["choch", "bos"],
        "timeframes": tg.get("timeframes") or [],
        "symbols": tg.get("symbols") or [],
        "max_event_age_bars": tg.get("max_event_age_bars", 2),
        "quiet_hours": tg.get("quiet_hours"),
        "timeout": tg.get("timeout", 10),
    }

    # 1. Primary top-level bot_token + chat_id (if configured)
    token, chat_id = _resolve_pair(tg)
    if token and chat_id:
        seen_pairs.add((token, chat_id))
        targets.append({
            "name": "default",
            "bot_token": token,
            "chat_id": chat_id,
            "target_prefix": "",
            **base_defaults,
        })

    # 2. Additional bots under telegram.bots
    for idx, entry in enumerate(tg.get("bots") or []):
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        b_token, b_chat = _resolve_pair(entry, default_token_env="", default_chat_env="")
        if not b_token or not b_chat:
            continue
        if (b_token, b_chat) in seen_pairs:
            continue
        seen_pairs.add((b_token, b_chat))
        bot_id = b_token.split(":", 1)[0]
        name = str(entry.get("name") or f"bot-{idx + 1}").strip()
        targets.append({
            "name": name,
            "bot_token": b_token,
            "chat_id": b_chat,
            "target_prefix": f"tg:{bot_id}:{b_chat}",
            "events": entry.get("events") if entry.get("events") is not None else base_defaults["events"],
            "timeframes": entry.get("timeframes") if entry.get("timeframes") is not None else base_defaults["timeframes"],
            "symbols": entry.get("symbols") if entry.get("symbols") is not None else base_defaults["symbols"],
            "max_event_age_bars": entry.get("max_event_age_bars", base_defaults["max_event_age_bars"]),
            "quiet_hours": entry.get("quiet_hours", base_defaults["quiet_hours"]),
            "timeout": entry.get("timeout", base_defaults["timeout"]),
        })

    return targets


def is_configured(cfg: dict) -> bool:
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled", False):
        return False
    return len(resolve_targets(cfg)) > 0


def _in_quiet_hours(quiet_hours: Optional[str], now_ts: Optional[float] = None) -> bool:
    if not quiet_hours or "-" not in str(quiet_hours):
        return False
    try:
        start_s, end_s = str(quiet_hours).split("-", 1)
        start_h, end_h = int(start_s), int(end_s)
        cur_h = time.gmtime(now_ts or time.time()).tm_hour
        if start_h <= end_h:
            return start_h <= cur_h < end_h
        return cur_h >= start_h or cur_h < end_h   # wraps midnight (e.g. "22-06")
    except ValueError:
        return False


def _target_timeframes(cfg: dict, explicit_tfs: Optional[list] = None) -> set:
    explicit = [str(t).strip().upper() for t in (explicit_tfs or []) if str(t).strip()]
    if explicit:
        return set(explicit)
    entry_tf = str((cfg.get("mtf") or {}).get("entry_tf") or "").strip().upper()
    if entry_tf:
        return {entry_tf}
    return {str(t).strip().upper() for t in (cfg.get("timeframes") or [])}


def _event_key(symbol: str, tf: str, ev: dict, target_prefix: str = "") -> str:
    base = (
        f"{symbol}|{tf}|{ev.get('type')}|{ev.get('direction')}|"
        f"{int(ev.get('time') or 0)}|{int(ev.get('level_time') or 0)}"
    )
    return f"{target_prefix}|{base}" if target_prefix else base


def format_structure_message(symbol: str, tf: str, ev: dict, smc: dict) -> str:
    etype = str(ev.get("type", "")).upper()          # CHOCH | BOS
    direction = str(ev.get("direction", "")).lower()   # bullish | bearish
    is_bull = direction == "bullish"
    badge = "🟢" if is_bull else "🔴"
    kind_tag = "⚡ <b>CHoCH</b> (Reversal)" if etype == "CHOCH" else "📈 <b>BOS</b> (Continuation)"
    dir_label = "BULLISH" if is_bull else "BEARISH"
    level = ev.get("level")
    close = smc.get("last_close")
    trend = str(smc.get("trend") or "-").upper()
    ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ev.get("time") or 0)))

    lines = [
        f"{badge} <b>{symbol}</b> ({tf}) — {kind_tag}",
        f"Direction: <b>{dir_label}</b> (Trend: {trend})",
        f"Broken Level: <code>{level}</code>",
    ]
    if close is not None:
        lines.append(f"Bar Close: <code>{close}</code>")
    lines.append(f"Time: {ts_str}")
    return "\n".join(lines)


def send_message(bot_token: str, chat_id: str, text: str, timeout: int = 10) -> Tuple[bool, str]:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=max(3, int(timeout)))
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        if not data.get("ok"):
            return False, str(data.get("description") or "unknown Telegram API error")
        return True, "ok"
    except Exception as e:
        return False, str(e)


def notify_structure_events(
    store: Store,
    cfg: dict,
    pairs: Iterable[Tuple[str, str]],
) -> int:
    """Inspect freshly stored analyses for `pairs` and send Telegram alerts for
    any new CHoCH / BOS events across all configured bot targets.
    Returns total count of alerts sent."""
    if not is_configured(cfg):
        return 0

    targets = resolve_targets(cfg)
    pair_list = list(pairs)
    now_ts = int(time.time())
    sent_count = 0

    # Cache loaded analyses across targets so SQLite is queried once per pair
    analyses = {}
    for symbol, tf in pair_list:
        if (symbol, tf) not in analyses:
            analyses[(symbol, tf)] = store.load_analysis(symbol, tf)

    for target in targets:
        token = target["bot_token"]
        chat_id = target["chat_id"]
        prefix = target["target_prefix"]
        t_name = target["name"]
        allowed_events = {str(e).strip().lower() for e in (target.get("events") or ["choch", "bos"])}
        allowed_tfs = _target_timeframes(cfg, target.get("timeframes"))
        allowed_syms = {str(s).strip() for s in (target.get("symbols") or []) if str(s).strip()}
        max_age = int(target.get("max_event_age_bars", 2) or 2)
        timeout = int(target.get("timeout", 10) or 10)

        for symbol, tf in pair_list:
            tf_up = str(tf).strip().upper()
            if allowed_tfs and tf_up not in allowed_tfs:
                continue
            if allowed_syms and symbol not in allowed_syms:
                continue

            analysis = analyses.get((symbol, tf))
            if not analysis:
                continue
            smc = analysis.get("smc") or {}
            events: List[dict] = smc.get("events") or []
            n_bars = int((analysis.get("meta") or {}).get("bars") or 0)

            # First run for this (target, symbol, tf): seed existing events silently
            if not store.has_telegram_seed(symbol, tf, target_prefix=prefix):
                seed_rows = [
                    (
                        _event_key(symbol, tf, ev, target_prefix=prefix),
                        symbol,
                        tf,
                        str(ev.get("type", "")),
                        str(ev.get("direction", "")),
                        int(ev.get("time") or 0),
                        now_ts,
                    )
                    for ev in events
                ]
                if not seed_rows:
                    seed_key = f"{prefix}|{symbol}|{tf}|__seed__" if prefix else f"{symbol}|{tf}|__seed__"
                    seed_rows = [(seed_key, symbol, tf, "seed", "", 0, now_ts)]
                store.record_telegram_alerts(seed_rows)
                continue

            if _in_quiet_hours(target.get("quiet_hours"), now_ts):
                continue

            for ev in events:
                etype = str(ev.get("type") or "").lower()
                if etype not in allowed_events:
                    continue
                if n_bars and isinstance(ev.get("index"), int):
                    age_bars = (n_bars - 1) - ev["index"]
                    if age_bars > max_age:
                        continue
                key = _event_key(symbol, tf, ev, target_prefix=prefix)
                if store.is_telegram_alert_sent(key):
                    continue
                try:
                    record_choch_event(store, cfg, symbol, tf, ev, analysis)
                except Exception as e:
                    debug(f"[choch-csv] record error ({symbol} {tf}): {e}")
                msg = format_structure_message(symbol, tf, ev, smc)
                ok, err = send_message(token, chat_id, msg, timeout=timeout)
                if ok:
                    store.record_telegram_alerts([
                        (key, symbol, tf, etype, str(ev.get("direction", "")),
                         int(ev.get("time") or 0), int(time.time()))
                    ])
                    sent_count += 1
                    debug(f"[telegram:{t_name}] sent {etype.upper()} {ev.get('direction')} alert for {symbol} {tf}")
                else:
                    debug(f"[telegram:{t_name}] send failed for {symbol} {tf}: {err}")

    return sent_count
