# Proposal: Telegram Alerts for CHoCH & BOS Structure Breaks

## 1. Overview & Motivation

Market structure shifts—**Change of Character (`CHoCH`)** and **Break of Structure (`BOS`)**—are detected in `app/smc/structure.py` (`detect_structure`) on every analysis run, but are currently only persisted to SQLite (`analysis.smc.events`) and rendered on the web dashboard. Traders who are away from the browser have no real-time notification when key pairs print a reversal (`CHoCH`) or trend continuation (`BOS`) on a confirmed candle close.

This proposal adds a lightweight, zero-extra-dependency Telegram notifier (`app/notifications/telegram.py`) wired into the live analysis lifecycle:

- **Configurable bot credentials & filtering** under a top-level `telegram:` block in `config.yaml` (with automatic fallback to `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment variables).
- **Confirmed-bar accuracy**: dispatches after confirmed-bar analysis (`bar_close_watcher`, `auto_run_interval`, and `python -m app.main run`), matching how `detect_structure` evaluates closed candles.
- **Zero historical flood & strict deduplication**: persists dispatched event keys in a SQLite `telegram_alerts` table and silently seeds existing historical events on a pair's first run so restarts or initial enablement never spam old breaks.
- **Non-blocking & fault-isolated**: network failures or Telegram rate limits log via `debug()` and never interrupt candle ingestion, ML inference, or live MT5 execution.

---

## 2. Architecture & Data Flow

```
config.yaml (telegram: enabled, bot_token, chat_id, events, timeframes, ...)
       │
       ▼
app/config.py (DEFAULTS["telegram"] + env fallback TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
       │
       ▼
Live Analysis Trigger:
  ├── app/dashboard/server.py  (_bar_watcher batch completion & _refresh_job)
  └── app/main.py              (cmd_run cycle)
       │
       ▼
app/notifications/telegram.py :: notify_structure_events(store, cfg, pairs)
  ├── Filters symbols/TFs (defaults to mtf.entry_tf when telegram.timeframes is empty)
  ├── Reads latest analysis from store.load_analysis(symbol, tf)
  ├── First time seeing (symbol, tf)? -> seeds existing events in SQLite, sends 0 alerts
  ├── Otherwise -> filters unseen events matching telegram.events (["choch", "bos"])
  ├── Formats HTML message (symbol, TF, CHoCH/BOS, direction, broken level, close, timestamp)
  ├── POST [https://api.telegram.org/bot](https://api.telegram.org/bot)<token>/sendMessage (requests, timeout=10s)
  └── Records event key in store.mark_telegram_alert(...) so it never fires twice
```

---

## 3. Configuration Schema (`config.yaml` & `app/config.py`)

### 3.1 `config.yaml`

Add a new top-level `telegram:` section (default `enabled: false` so existing setups remain silent until configured):

```yaml
# ============================================================
# TELEGRAM ALERTS — push notifications for CHoCH & BOS breaks
# ============================================================
# Create a bot via @BotFather to get bot_token, and fetch your chat_id
# via @userinfobot or [https://api.telegram.org/bot](https://api.telegram.org/bot)<TOKEN>/getUpdates.
# Credentials can be placed here OR supplied via environment variables
# (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) when the fields below are empty.
telegram:
  enabled: false               # master switch (or toggle live from the dashboard)
  bot_token: ""                # e.g. "123456789:AAH..." (empty -> $TELEGRAM_BOT_TOKEN)
  chat_id: ""                  # e.g. "-1001234567890" or "987654321" (empty -> $TELEGRAM_CHAT_ID)
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID
  events: [choch, bos]         # event types to alert on: choch, bos
  timeframes: []               # empty = follow mtf.entry_tf (e.g. [M15]); or explicit e.g. [M15, H1]
  symbols: []                  # empty = all enabled/visible symbols; or allowlist e.g. [EURUSD, XAUUSD]
  max_event_age_bars: 2        # ignore events older than N bars from the latest analyzed bar
  quiet_hours: null            # optional UTC window to mute alerts, e.g. "22-06" (null = always)
  timeout: 10                  # HTTP timeout in seconds for Telegram Bot API calls
  # Optional list of additional (or multiple) bot tokens / chat targets.
  # Each item inherits events/timeframes/symbols/quiet_hours unless overridden:
  bots: []
  #   - name: swing-bot
  #     bot_token: "987654321:BBH..."    # or bot_token_env: TG_SWING_TOKEN
  #     chat_id: "-1009876543210"        # or chat_id_env: TG_SWING_CHAT
  #     events: [choch]
  #     timeframes: [H1, H4]
  #     symbols: [EURUSD, XAUUSD]
```

### 3.2 `app/config.py`

Add the matching `"telegram"` block to `DEFAULTS` (lines 125–131) so existing `config.yaml` files that omit `telegram:` inherit safe defaults via `_deep_merge`:

```python
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "chat_id_env": "TELEGRAM_CHAT_ID",
        "events": ["choch", "bos"],
        "timeframes": [],
        "symbols": [],
        "max_event_age_bars": 2,
        "quiet_hours": None,
        "timeout": 10,
        "bots": [],
    },
```

--- 

## 4. Detailed Implementation Specification

### 4.1 SQLite Deduplication & Seeding (`app/data/store.py`)

Add a `telegram_alerts` table in `Store._create()` (around line 114) and three thread-safe (`@_locked`) helper methods:

#### Schema
```sql
CREATE TABLE IF NOT EXISTS telegram_alerts(
    event_key  TEXT PRIMARY KEY,
    symbol     TEXT NOT NULL,
    tf         TEXT NOT NULL,
    event_type TEXT NOT NULL,
    direction  TEXT,
    event_time INTEGER NOT NULL,
    sent_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_alerts_pair ON telegram_alerts(symbol, tf);
```

#### Event Identity Key
Each structure event dict produced by `app/smc/structure.py` (`detect_structure`) has the shape:
```python
{
    "type": "choch" | "bos",
    "direction": "bullish" | "bearish",
    "level": float,          # broken swing high/low price
    "level_time": int,       # pivot bar unix timestamp
    "level_index": int,
    "index": int,            # break bar index
    "time": int,             # break bar unix timestamp
}
```
Construct a deterministic, bar-index-independent key:
```python
event_key = f"{symbol}|{tf}|{ev['type']}|{ev['direction']}|{int(ev['time'])}|{int(ev.get('level_time') or 0)}"
```
Using `time` + `level_time` (both unix timestamps) rather than `index` ensures the key remains invariant as the rolling 1500-bar window slides forward.

#### Store Methods
```python
    @_locked
    def has_telegram_seed(self, symbol: str, tf: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM telegram_alerts WHERE symbol=? AND tf=? LIMIT 1",
            (symbol, tf),
        )
        return cur.fetchone() is not None

    @_locked
    def is_telegram_alert_sent(self, event_key: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM telegram_alerts WHERE event_key=? LIMIT 1",
            (event_key,),
        )
        return cur.fetchone() is not None

    @_locked
    def record_telegram_alerts(self, rows: list):
        """Insert (event_key, symbol, tf, event_type, direction, event_time, sent_at) rows."""
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO telegram_alerts"
            "(event_key, symbol, tf, event_type, direction, event_time, sent_at) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
```

---

### 4.2 Telegram Notifier Module (`app/notifications/telegram.py` — New File)

Create `app/notifications/__init__.py` (empty) and `app/notifications/telegram.py` using `requests` (already in `requirements.txt`):

```python
"""Telegram Bot API notifier for market structure breaks (CHoCH & BOS)."""
import os
import time
from typing import Iterable, List, Optional, Tuple

import requests

from ..data.store import Store
from ..logsetup import debug

TELEGRAM_API = "[https://api.telegram.org](https://api.telegram.org)"


def resolve_credentials(cfg: dict) -> Tuple[str, str]:
    """Resolve (bot_token, chat_id) from config.yaml with env-var fallback."""
    tg = cfg.get("telegram") or {}
    token = str(tg.get("bot_token") or "").strip()
    if not token:
        token = os.getenv(str(tg.get("bot_token_env") or "TELEGRAM_BOT_TOKEN"), "").strip()
    chat_id = str(tg.get("chat_id") or "").strip()
    if not chat_id:
        chat_id = os.getenv(str(tg.get("chat_id_env") or "TELEGRAM_CHAT_ID"), "").strip()
    return token, chat_id


def is_configured(cfg: dict) -> bool:
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled", False):
        return False
    token, chat_id = resolve_credentials(cfg)
    return bool(token and chat_id)


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


def _target_timeframes(cfg: dict) -> set:
    tg = cfg.get("telegram") or {}
    explicit = [str(t).strip().upper() for t in (tg.get("timeframes") or []) if str(t).strip()]
    if explicit:
        return set(explicit)
    entry_tf = str((cfg.get("mtf") or {}).get("entry_tf") or "").strip().upper()
    if entry_tf:
        return {entry_tf}
    return {str(t).strip().upper() for t in (cfg.get("timeframes") or [])}


def _event_key(symbol: str, tf: str, ev: dict) -> str:
    return (
        f"{symbol}|{tf}|{ev.get('type')}|{ev.get('direction')}|"
        f"{int(ev.get('time') or 0)}|{int(ev.get('level_time') or 0)}"
    )


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
    any new CHoCH / BOS events. Returns count of alerts sent."""
    if not is_configured(cfg):
        return 0

    tg = cfg.get("telegram") or {}
    token, chat_id = resolve_credentials(cfg)
    allowed_events = {str(e).strip().lower() for e in (tg.get("events") or ["choch", "bos"])}
    allowed_tfs = _target_timeframes(cfg)
    allowed_syms = {str(s).strip() for s in (tg.get("symbols") or []) if str(s).strip()}
    max_age = int(tg.get("max_event_age_bars", 2) or 2)
    timeout = int(tg.get("timeout", 10) or 10)
    now_ts = int(time.time())

    sent_count = 0
    for symbol, tf in pairs:
        tf_up = str(tf).strip().upper()
        if allowed_tfs and tf_up not in allowed_tfs:
            continue
        if allowed_syms and symbol not in allowed_syms:
            continue

        analysis = store.load_analysis(symbol, tf)
        if not analysis:
            continue
        smc = analysis.get("smc") or {}
        events: List[dict] = smc.get("events") or []
        n_bars = int((analysis.get("meta") or {}).get("bars") or 0)

        # First run for this (symbol, tf): seed existing events silently so
        # historical breaks never flood Telegram on initial startup.
        if not store.has_telegram_seed(symbol, tf):
            seed_rows = [
                (
                    _event_key(symbol, tf, ev),
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
                # Sentinel row so an eventless pair is still marked seeded
                seed_rows = [(f"{symbol}|{tf}|__seed__", symbol, tf, "seed", "", 0, now_ts)]
            store.record_telegram_alerts(seed_rows)
            continue

        if _in_quiet_hours(tg.get("quiet_hours"), now_ts):
            continue

        for ev in events:
            etype = str(ev.get("type") or "").lower()
            if etype not in allowed_events:
                continue
            if n_bars and isinstance(ev.get("index"), int):
                age_bars = (n_bars - 1) - ev["index"]
                if age_bars > max_age:
                    continue
            key = _event_key(symbol, tf, ev)
            if store.is_telegram_alert_sent(key):
                continue
            msg = format_structure_message(symbol, tf, ev, smc)
            ok, err = send_message(token, chat_id, msg, timeout=timeout)
            if ok:
                store.record_telegram_alerts([
                    (key, symbol, tf, etype, str(ev.get("direction", "")),
                     int(ev.get("time") or 0), int(time.time()))
                ])
                sent_count += 1
                debug(f"[telegram] sent {etype.upper()} {ev.get('direction')} alert for {symbol} {tf}")
            else:
                debug(f"[telegram] send failed for {symbol} {tf}: {err}")

    return sent_count
```

---

### 4.3 Hook Into Live Execution Loops

Three call sites cover all live operation modes while keeping `python -m app.main analyze` (offline/backfill CLI) silent:

1. **Dashboard bar-close watcher** — `app/dashboard/server.py` (inside `_bar_watcher()` right after `pipeline.resolve_pending(store, cfg)` around line 222):
   ```python
   from ..notifications.telegram import notify_structure_events
   try:
       ok_pairs = [(r["symbol"], r["tf"]) for r in results if r.get("ok")]
       if ok_pairs:
           notify_structure_events(store, cfg, ok_pairs)
   except Exception as e:
       debug(f"[telegram] bar-close notify error: {e}")
   ```

2. **Dashboard auto-run / manual refresh job** — `app/dashboard/server.py` (inside `_refresh_job()` right after `pipeline.run_all(...)` around line 273):
   ```python
   from ..notifications.telegram import notify_structure_events
   try:
       all_pairs = [(s, tf) for s in visible_symbols() for tf in cfg["timeframes"]]
       notify_structure_events(store, cfg, all_pairs)
   except Exception as e:
       debug(f"[telegram] refresh notify error: {e}")
   ```

3. **CLI continuous loop (`python -m app.main run`)** — `app/main.py` inside `cmd_run()` right after `pipeline.run_all(...)`:
   ```python
   from .notifications.telegram import notify_structure_events
   notify_structure_events(store, cfg, [(s, tf) for s in syms for tf in cfg["timeframes"]])
   ```

---

### 4.4 CLI Verification Command & Dashboard Endpoints

Following the pattern of `cmd_test_llm` in `app/main.py` and `/api/execution/*` in `app/dashboard/server.py`:

1. **CLI `python -m app.main test-telegram`** (`app/main.py`):
   - Resolves `bot_token` and `chat_id` (reporting whether each came from `config.yaml` or env var, masking the token as `123456:AAH...xyz`).
   - Sends a test message (`✅ AI Trader Telegram alerts connected`) and prints `[OK]` or the exact Telegram API error (`Unauthorized`, `chat not found`, etc.).

2. **Dashboard API endpoints** (`app/dashboard/server.py`):
   - Expose `"telegram": {"enabled": bool(cfg.get("telegram", {}).get("enabled", False)), "configured": is_configured(cfg)}` in `GET /api/meta`.
   - `POST /api/telegram/test` — sends a test message using current credentials and returns `{"ok": true}` or `{"ok": false, "error": "..."}`.
   - `POST /api/telegram/toggle` (`{"enabled": bool}`) — toggles runtime `cfg["telegram"]["enabled"]` without restarting the server (matching `/api/execution/toggle`).

---

## 5. Edge Cases & Behavioral Notes

- **Bar-close alignment (`drop_last_bar=True`)**: In `_bar_watcher()`, `analyze_symbol` runs with `drop_last_bar=True`, meaning `events[-1]` is always a confirmed closed candle. In `_refresh_job()` / `cmd_run()`, `drop_last_bar=False` is used; `max_event_age_bars: 2` ensures recent breaks still fire while stale breaks are ignored.
- **Simultaneous bullish & bearish breaks on an outside bar**: `detect_structure` checks `active_high` and `active_low` independently in the same bar (`app/smc/structure.py:66-92`). Because `_event_key` includes both `type` and `direction`, both events are tracked and alerted independently.
- **ProcessPoolExecutor safety**: Worker processes in `_analyze_pair_worker` only compute and save the analysis JSON; `notify_structure_events` runs once in the parent server process after `pool.map` completes, avoiding SQLite write contention or duplicate HTTP sends across workers.
- **Restart persistence**: Because `telegram_alerts` lives in `data/trader.db` (or `data/demo_trader.db` in `--demo` mode), restarting `serve.bat` will neither re-alert already sent events nor miss new breaks that occur after restart.

---

## 6. Setup Guide (Single & Multiple Telegram Tokens)

### 6.1 Create a Bot & Get Your Chat ID

1. **Create a bot token**: Open Telegram, message **@BotFather**, send `/newbot`, follow the prompts, and copy the HTTP API token (e.g. `123456789:AAH...`).
2. **Get your `chat_id`**:
   - **Direct messages**: Send `/start` to your new bot, then message **@userinfobot** (or visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging the bot) to read your numeric user ID.
   - **Group / Supergroup / Channel**: Add the bot to the group/channel (as admin for channels), send a message in it, and read `chat.id` (starts with `-100...`) from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 6.2 Single Bot Setup

Configure credentials either in `config.yaml` or via environment variables (recommended so secrets stay out of git):

**Option A — Environment variables (Windows):**
```bat
setx TELEGRAM_BOT_TOKEN "123456789:AAH..."
setx TELEGRAM_CHAT_ID "-1001234567890"
```
Then set `telegram.enabled: true` in `config.yaml` (leaving `bot_token: ""` and `chat_id: ""`).

**Option B — Directly in `config.yaml`:**
```yaml
telegram:
  enabled: true
  bot_token: "123456789:AAH..."
  chat_id: "-1001234567890"
  events: [choch, bos]
  timeframes: []         # empty = follow mtf.entry_tf (M15)
  symbols: []            # empty = all enabled symbols
```

### 6.3 Multiple Telegram Tokens / Channels (`telegram.bots`)

To fan out alerts across **multiple Telegram bots** (or route different timeframes/events/symbols to separate bots and chats), add entries under `telegram.bots:`. Each entry resolves `bot_token` (or `bot_token_env`) and `chat_id` (or `chat_id_env`), inherits top-level filters by default, and tracks its own deduplication & first-run seed independently:

```yaml
telegram:
  enabled: true
  # Optional primary bot (can be left blank if you only use bots: below)
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID
  events: [choch, bos]
  timeframes: [M15]

  bots:
    - name: scalping-desk
      bot_token: "111111111:AAH..."      # or bot_token_env: TG_SCALP_TOKEN
      chat_id: "-1001111111111"        # or chat_id_env: TG_SCALP_CHAT
      events: [choch, bos]
      timeframes: [M15]

    - name: htf-reversals
      bot_token_env: TG_SWING_TOKEN
      chat_id_env: TG_SWING_CHAT
      events: [choch]                    # override: CHoCH reversals only
      timeframes: [H1, H4]               # override: H1 & H4 only
      symbols: [EURUSD, GBPUSD, XAUUSD]  # override: symbol allowlist
```

### 6.4 Verify & Run

1. Send a test message to every configured bot target:
   ```bat
   python -m app.main test-telegram
   ```
2. Start the live bar-close watcher (first run silently seeds existing structure events, then sends alerts on every confirmed CHoCH/BOS close):
   ```bat
   serve.bat --on-bar-close
   ```

---

## 7. Verification Plan

1. **Config & credential resolution**:
   - Verify `load_config()` returns `telegram.enabled is False` by default and resolves `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` when YAML fields are blank.
2. **First-run seeding & deduplication**:
   - Run `notify_structure_events(store, cfg, [("EURUSD", "M15")])` against a populated analysis in a temp SQLite DB with `send_message` mocked: confirm **0** messages sent on call 1 (seeded), **0** messages sent on call 2 (unchanged), and **1** message sent when a new `choch` or `bos` event is appended.
3. **Filtering**:
   - Verify `events: ["choch"]` suppresses `bos` alerts, `timeframes: []` restricts alerts to `mtf.entry_tf`, and `quiet_hours` mutes delivery during the configured UTC window.
4. **Live credential check**:
   - Configure `telegram.bot_token` and `telegram.chat_id` in `config.yaml` (or env vars) and run `python -m app.main test-telegram` to confirm delivery in Telegram.
