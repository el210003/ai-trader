"""Setup -> live MT5 order bridge.

ExecutionEngine scans stored analyses for setups whose hybrid verdict is BUY
or SELL (the AI stack says the setup is tradeable) and places corresponding
MT5 orders with the setup's stop-loss / take-profit attached.

Design notes
------------
- Owns the ONLY MT5 trading connection (the engine thread). The pipeline's
  ingest connections are separate; order routing lives in exactly one place
  and never runs inside ProcessPool analysis workers.
- Scans the SQLite `analysis` table instead of hooking the pipeline, so every
  trigger path works identically: bar-close watcher, auto-run timer, the
  dashboard's "Run analysis" button, and the `analyze` CLI.
- Dedup by setup identity (same scheme as the setups journal) + per-symbol
  cooldown + position caps + daily cap. Every attempt is recorded in the
  `trades` table; transient skips (spread, cooldown, ...) are not persisted.
- dry_run (default) records orders without sending them -- flip it only when
  you have watched the dashboard for a while.
"""
import json
import threading
import time
from collections import Counter
from pathlib import Path

from ..logsetup import debug
from . import mt5_trader

# Runtime-adjustable parameters (Trade tab / API): the strictness gates plus
# the bot's order identity. Values are clamped/validated no matter where they
# come from (config.yaml, overrides file, API).
GATE_DEFAULTS = {"min_score": 70.0, "min_ml_prob": 0.50}
GATE_LIMITS = {"min_score": (0.0, 100.0), "min_ml_prob": (0.0, 1.0)}
COMMENT_MAX = 31                     # MT5 order-comment display limit
MAGIC_MAX = 2**63 - 1                # MT5 stores the magic as an unsigned long


def trade_identity(symbol: str, tf: str, direction: str, setup: dict) -> str:
    """Same identity scheme as the setups journal (store.append_setups) so a
    journaled setup and its execution refer to the same idea exactly once."""
    zone = setup.get("entry_zone") or {}
    origin = int(zone.get("origin_time") or 0)
    return f"{symbol}|{tf}|{direction}|{origin}|{float(setup.get('entry') or 0):.8f}"


class ExecutionEngine:
    def __init__(self, cfg: dict, store, allowed_symbols: list = None,
                 verbose: bool = True, dry_run_override: bool = None):
        self.cfg = cfg
        self.x = dict(cfg.get("execution") or {})
        self._apply_overrides()           # UI/CLI overrides beat config.yaml
        self.store = store
        self.verbose = verbose
        self.allowed_symbols = set(allowed_symbols) if allowed_symbols else None
        self.mt5 = None                      # lazy: only when enabled
        self.enabled = bool(self.x.get("enabled", False))
        self.dry_run = bool(self.x.get("dry_run", True))
        if dry_run_override is not None:
            self.dry_run = bool(dry_run_override)
        self.magic = int(self.x.get("magic", 862001))
        self._stop = threading.Event()
        self.state = {
            "last_scan": None, "last_error": None,
            "scans": 0, "executed": 0, "skipped": 0,
        }

    # ------------------------------------------------------- config helpers
    @property
    def entry_tf(self):
        return ((self.cfg.get("mtf", {}) or {}).get("entry_tf") or "").strip().upper() or None

    def _int(self, key, default):
        try:
            return int(self.x.get(key, default))
        except (TypeError, ValueError):
            return default

    def _float(self, key, default):
        try:
            return float(self.x.get(key, default))
        except (TypeError, ValueError):
            return default

    # -------------------------------------------------- runtime parameters
    # min_score / min_ml_prob (gates) and magic / comment (order identity) can
    # be changed live from the Trade tab (or the HTTP API). config.yaml stays
    # untouched (it is full of comments) — overrides live in
    # data/execution_overrides.json and are re-applied on every engine start,
    # so an adjustment survives restarts.
    def _overrides_path(self) -> Path:
        root = Path(self.cfg.get("_root") or ".")
        return root / "data" / "execution_overrides.json"

    def _load_overrides(self) -> dict:
        try:
            with open(self._overrides_path(), "r", encoding="utf-8") as f:
                ov = json.load(f)
            return ov if isinstance(ov, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_overrides(self, patch: dict):
        ov = self._load_overrides()
        ov.update(patch)
        try:
            self._overrides_path().parent.mkdir(parents=True, exist_ok=True)
            with open(self._overrides_path(), "w", encoding="utf-8") as f:
                json.dump(ov, f, indent=2)
        except OSError as e:
            debug(f"[execution] could not persist execution overrides: {e}")

    def _apply_overrides(self):
        ov = self._load_overrides()
        for key in GATE_DEFAULTS:
            if key not in ov:
                continue
            try:
                lo, hi = GATE_LIMITS[key]
                self.x[key] = max(lo, min(hi, float(ov[key])))
            except (TypeError, ValueError):
                pass
        if "magic" in ov:
            try:
                m = int(ov["magic"])
                if 1 <= m <= MAGIC_MAX:
                    self.x["magic"] = m
            except (TypeError, ValueError):
                pass
        if "comment" in ov:
            self.x["comment"] = str(ov["comment"]).strip()[:COMMENT_MAX] or "ai-trader"

    def gates(self) -> dict:
        """Current gate values (live snapshot — includes UI/API overrides)."""
        return {k: self._float(k, d) for k, d in GATE_DEFAULTS.items()}

    def set_gates(self, min_score: float = None, min_ml_prob: float = None) -> dict:
        """Adjust the score / ML-probability gates at runtime. Effective on the
        next engine scan and persisted so restarts keep the new values."""
        patch = {}
        if min_score is not None:
            patch["min_score"] = float(min_score)
        if min_ml_prob is not None:
            patch["min_ml_prob"] = float(min_ml_prob)
        for key, val in patch.items():
            lo, hi = GATE_LIMITS[key]
            self.x[key] = max(lo, min(hi, val))
        if patch:
            self._save_overrides({k: self.x[k] for k in patch})
            debug("[execution] gates updated: " +
                  ", ".join(f"{k}={self.x[k]:g}" for k in patch))
        return self.gates()

    def set_identity(self, magic: int = None, comment: str = None) -> dict:
        """Runtime-adjust the bot's order identity: the magic number tagging
        every bot order and the comment shown in MT5 (truncated to 31 chars,
        MT5's display limit). Applies to NEW orders only — existing orders keep
        their original tag, so a magic change orphans them from the engine's
        point of view (see _warn_orphans). Persisted like the gates."""
        changed = {}
        if magic is not None:
            m = int(magic)
            if m < 1 or m > MAGIC_MAX:
                raise ValueError("magic must be a positive integer")
            old = self.magic
            self.x["magic"] = m
            self.magic = m          # cached attribute used by every filter
            changed["magic"] = m
            if old != m:
                self._warn_orphans(old)
        if comment is not None:
            c = str(comment).strip()[:COMMENT_MAX]
            self.x["comment"] = c or "ai-trader"
            changed["comment"] = self.x["comment"]
        if changed:
            self._save_overrides(changed)
            debug("[execution] identity updated: " +
                  ", ".join(f"{k}={changed[k]}" for k in changed))
        return {"magic": self.magic,
                "comment": self.x.get("comment") or "ai-trader"}

    def _warn_orphans(self, old_magic: int):
        """Best-effort heads-up when the magic changes: orders tagged with the
        previous magic are no longer visible to status/flatten — the engine
        will never touch them again (they must be closed manually in MT5)."""
        if not self.enabled:
            return
        try:
            mt5 = self._connect()
            n = (len(mt5_trader.bot_positions(mt5, old_magic))
                 + len(mt5_trader.bot_pendings(mt5, old_magic)))
            if n:
                debug(f"[execution] WARNING: {n} order(s) tagged with the old "
                      f"magic {old_magic} are now orphaned — the engine will "
                      f"not manage or flatten them; close them manually in MT5")
        except Exception:
            pass  # MT5 unavailable — the warning is best-effort only

    # ---------------------------------------------------------- lifecycle
    def _connect(self):
        if self.mt5 is not None:
            return self.mt5
        from ..data import mt5_client
        self.mt5 = mt5_client.connect_mt5(self.cfg["mt5"])
        return self.mt5

    def _drop(self):
        if self.mt5 is not None:
            try:
                from ..data import mt5_client
                mt5_client.shutdown_mt5(self.mt5)
            except Exception:
                pass
        self.mt5 = None

    def stop(self):
        self._stop.set()

    def run_forever(self, poll_seconds: int = None):
        """Blocking loop for CLI use; the dashboard runs scan_once on a thread."""
        poll = max(5, int(poll_seconds or self.x.get("poll_seconds", 15)))
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:
                self.state["last_error"] = str(e)
                if self.verbose:
                    debug(f"[execution] scan error: {e}")
            self._stop.wait(poll)

    # ----------------------------------------------------------- scanning
    def _candidates(self, fresh_seconds: int) -> list:
        """Qualifying setups from the freshest analyses: verdict BUY/SELL only,
        entry timeframe only, symbols restricted to the allowed list."""
        etf = self.entry_tf
        now = int(time.time())
        out = []
        for meta in self.store.list_analyses():
            symbol, tf = meta["symbol"], meta["tf"]
            if self.allowed_symbols is not None and symbol not in self.allowed_symbols:
                continue
            if etf and tf.strip().upper() != etf:
                continue
            payload = self.store.load_analysis(symbol, tf)
            if not payload:
                continue
            generated_at = int(payload.get("generated_at") or 0)
            if fresh_seconds > 0 and now - generated_at > fresh_seconds:
                continue            # stale analysis -- a newer one will follow
            for s in payload.get("setups") or []:
                if (s.get("verdict") or "").upper() in ("BUY", "SELL") and s.get("entry"):
                    s = dict(s)
                    s["symbol"], s["tf"], s["generated_at"] = symbol, tf, generated_at
                    out.append(s)
        return out

    def scan_once(self) -> dict:
        """One execution pass over fresh analyses. Returns a summary dict."""
        summary = {"at": int(time.time()), "considered": 0,
                   "actions": [], "skipped": {}, "enabled": self.enabled}
        if not self.enabled:
            summary["skipped"]["execution disabled"] = 1
            return summary

        fresh = self._int("fresh_seconds", 120)
        try:
            cands = self._candidates(fresh)
        except Exception as e:
            self.state["last_error"] = f"scan: {e}"
            summary["error"] = str(e)
            return summary

        skipped = Counter()
        actions = []
        for s in cands:
            summary["considered"] += 1
            try:
                res = self.evaluate_and_execute(s)
            except Exception as e:
                res = {"status": "error", "reason": str(e),
                       "symbol": s.get("symbol"), "direction": s.get("direction")}
            if res["status"] in ("filled", "placed", "dry_run", "rejected", "error"):
                actions.append(res)
                if self.verbose:
                    debug(f"[execution] {res['status'].upper()} {res.get('symbol')} "
                          f"{res.get('direction')} {res.get('lot', '')} "
                          f"{res.get('reason') or res.get('price', '')}")
            else:
                skipped[res.get("reason", "?")] += 1
        summary["actions"], summary["skipped"] = actions, dict(skipped)
        self.state["scans"] += 1
        self.state["executed"] += len(actions)
        self.state["skipped"] += sum(skipped.values())
        self.state["last_scan"] = {"at": summary["at"], "considered": summary["considered"],
                                   "actions": len(actions), "skipped": dict(skipped)}
        self.state["last_error"] = None
        # housekeeping: cancel stale limit orders (portable expiry — some
        # brokers reject ORDER_TIME_SPECIFIED, so the engine enforces it)
        try:
            self._expire_pendings()
        except Exception as e:
            debug(f"[execution] pending expiry: {e}")
        return summary

    def _expire_pendings(self):
        ttl = self._int("pending_expiry_minutes", 240)
        if ttl <= 0:
            return
        mt5 = self._connect()
        cutoff = time.time() - ttl * 60
        for o in mt5_trader.bot_pendings(mt5, self.magic):
            if int(getattr(o, "time_setup", 0)) < cutoff:
                r = mt5_trader.cancel_order(mt5, int(o.ticket))
                debug(f"[execution] expired pending #{o.ticket} {o.symbol}: "
                      f"{'canceled' if r['ok'] else r['comment']}")

    # ------------------------------------------------------------- gates
    def _gates(self, s: dict, info: dict, tick, positions: list, pendings: list) -> str:
        """Return a skip reason, or empty string when all gates pass."""
        x = self.x
        symbol, direction = s["symbol"], s["direction"]
        verdict = (s.get("verdict") or "").upper()

        if verdict not in ("BUY", "SELL"):
            return "verdict not tradeable"
        if (verdict == "BUY") != (direction == "long"):
            return "verdict/direction mismatch"
        if float(s.get("final_score") or 0) < self._float("min_score", 70):
            return f"score {s.get('final_score')} < min_score"
        ml = s.get("ml_prob")
        if ml is not None and ml < self._float("min_ml_prob", 0.50):
            return f"ml_prob {ml:.2f} < min_ml_prob"

        # account caps (positions + pendings, our magic only)
        max_open = self._int("max_open_positions", 3)
        if max_open and len(positions) + len(pendings) >= max_open:
            return f"max_open_positions ({max_open})"
        per_sym = self._int("max_per_symbol", 1)
        if per_sym:
            mine = [p for p in positions + pendings if getattr(p, "symbol", None) == symbol]
            if len(mine) >= per_sym:
                return f"max_per_symbol ({per_sym}) for {symbol}"
        if self._int("max_trades_per_day", 5) and \
           self.store.trades_today(("filled", "placed", "dry_run")) >= self._int("max_trades_per_day", 5):
            return "max_trades_per_day reached"

        # cooldown per symbol+direction
        cooldown = self._float("cooldown_minutes", 240) * 60
        if cooldown > 0:
            last = self.store.last_trade_time(symbol, direction,
                                              ("filled", "placed", "dry_run"))
            if last and time.time() - last < cooldown:
                return f"cooldown ({self._float('cooldown_minutes', 240):.0f} min)"

        # broker-side trade mode
        tm = info["trade_mode"]
        if tm == mt5_trader._TRADE_MODE_DISABLED or tm == mt5_trader._TRADE_MODE_CLOSEONLY:
            return f"symbol {info['trade_mode_text']}"
        if tm == mt5_trader._TRADE_MODE_LONGONLY and direction == "short":
            return "symbol is long-only"
        if tm == mt5_trader._TRADE_MODE_SHORTONLY and direction == "long":
            return "symbol is short-only"

        if tick is None or not tick.ask or not tick.bid:
            return "no tick price"

        # spread cap (points)
        max_spread = self._int("max_spread_points", 40)
        if max_spread > 0 and info["point"] > 0:
            spread = (tick.ask - tick.bid) / info["point"]
            if spread > max_spread:
                return f"spread {spread:.0f} > {max_spread} points"

        # optional trading window in BROKER-SERVER hours ("07-20")
        hours = (self.x.get("trading_hours") or "").strip()
        if hours:
            try:
                a, b = hours.replace(" ", "").split("-")
                h = time.gmtime(int(tick.time)).tm_hour
                lo, hi = int(a), int(b)
                ok = lo <= h < hi if lo <= hi else (h >= lo or h < hi)
                if not ok:
                    return f"outside trading_hours {hours} (server hour {h})"
            except ValueError:
                pass    # malformed config -- ignore rather than block trading
        return ""

    # ---------------------------------------------------------- execution
    def evaluate_and_execute(self, s: dict) -> dict:
        """All gates + sizing + order send for ONE candidate setup."""
        symbol, direction, tf = s["symbol"], s["direction"], s["tf"]
        identity = trade_identity(symbol, tf, direction, s)
        base = {"identity": identity, "symbol": symbol, "tf": tf,
                "direction": direction, "verdict": s.get("verdict"),
                "score": s.get("final_score"), "ml_prob": s.get("ml_prob"),
                "entry": s.get("entry"), "stop_loss": s.get("stop_loss"),
                "take_profit": s.get("take_profit"), "rr": s.get("rr")}

        # setup sanity: SL/TP must exist and sit on the correct sides
        entry, sl, tp = float(s["entry"]), float(s["stop_loss"]), float(s["take_profit"])
        if direction == "long" and not (sl < entry < tp):
            return {**base, "status": "skipped", "reason": "invalid SL/TP geometry"}
        if direction == "short" and not (tp < entry < sl):
            return {**base, "status": "skipped", "reason": "invalid SL/TP geometry"}

        # identity already attempted? (any real attempt blocks re-entry forever)
        if self.store.trade_attempted(identity):
            return {**base, "status": "skipped", "reason": "setup already executed"}

        try:
            mt5 = self._connect()
            info = mt5_trader.symbol_trade_info(mt5, symbol)
        except Exception as e:
            self._drop()
            return {**base, "status": "error", "reason": f"MT5: {e}"}

        positions = mt5_trader.bot_positions(mt5, self.magic)
        pendings = mt5_trader.bot_pendings(mt5, self.magic)
        tick = mt5.symbol_info_tick(symbol)
        reason = self._gates(s, info, tick, positions, pendings)
        if reason:
            return {**base, "status": "skipped", "reason": reason}

        # ---- position sizing
        fixed = self.x.get("fixed_lot")
        try:
            if fixed:
                lot = float(fixed)
            else:
                lot = mt5_trader.calc_lot(mt5, symbol, entry, sl,
                                          self._float("risk_percent", 1.0), info=info)
        except Exception as e:
            return {**base, "status": "skipped", "reason": f"sizing: {e}"}
        lot = mt5_trader.normalize_lot(lot, info)
        if lot < info["volume_min"]:
            if self.x.get("allow_min_lot", False):
                lot = info["volume_min"]
            else:
                return {**base, "status": "skipped",
                        "reason": f"lot {lot} < broker minimum {info['volume_min']} "
                                  f"(raise risk_percent or set allow_min_lot)"}

        # ---- margin pre-check
        ref_price = tick.ask if direction == "long" else tick.bid
        need, free = mt5_trader.margin_available(mt5, info, direction, lot, ref_price)
        if need is not None and need > free:
            return {**base, "status": "skipped", "lot": lot,
                    "reason": f"insufficient margin (need {need:.0f}, free {free:.0f})"}

        comment = (self.x.get("comment") or "ai-trader")
        deviation = self._int("deviation", 20)

        # ---- dry run: record what WOULD be sent
        if self.dry_run:
            rec = {**base, "status": "dry_run", "lot": lot,
                   "order_type": self.x.get("entry_type", "market"),
                   "requested_price": ref_price, "reason": "dry run (no order sent)"}
            self.store.record_trade(self._row(rec, s))
            return {**rec, "price": ref_price}

        # ---- live send
        entry_type = (self.x.get("entry_type") or "market").strip().lower()
        if entry_type == "limit":
            # limit at the setup's zone entry; fall back to market when the
            # zone is already at/past the market (price would fill instantly)
            price = mt5_trader.normalize_price(entry, info)
            side_ok = (price < tick.ask) if direction == "long" else (price > tick.bid)
            if side_ok:
                res = mt5_trader.send_limit(mt5, symbol, direction, lot, price,
                                            sl, tp, self.magic, comment, info=info)
                status = "placed" if res["ok"] else "rejected"
            else:
                entry_type = "market (zone at market)"
                res = mt5_trader.send_market(mt5, symbol, direction, lot, sl, tp,
                                             self.magic, comment, deviation, info=info)
                status = "filled" if res["ok"] else "rejected"
        else:
            res = mt5_trader.send_market(mt5, symbol, direction, lot, sl, tp,
                                         self.magic, comment, deviation, info=info)
            status = "filled" if res["ok"] else "rejected"
            price = res.get("price") or ref_price

        rec = {**base, "status": status, "lot": lot, "order_type": entry_type,
               "requested_price": ref_price, "fill_price": res.get("price"),
               "ticket": res.get("order"), "deal": res.get("deal"),
               "retcode": res.get("retcode"),
               "reason": res.get("comment") or mt5_trader.retcode_text(res.get("retcode"))}
        self.store.record_trade(self._row(rec, s))
        if not res["ok"]:
            debug(f"[execution] order rejected {symbol} {direction}: "
                  f"{mt5_trader.retcode_text(res.get('retcode'))}")
        return {**rec, "price": res.get("price") or ref_price}

    def _row(self, rec: dict, setup: dict) -> dict:
        """Flatten an execution record into a trades-table row."""
        payload = {k: setup.get(k) for k in
                   ("confluences", "entry_zone", "range_position", "rr",
                    "htf_metrics", "generated_at", "llm_score")}
        payload["engine"] = {"magic": self.magic, "dry_run": self.dry_run,
                             "risk_percent": self.x.get("risk_percent"),
                             "fixed_lot": self.x.get("fixed_lot")}
        return {
            "identity": rec["identity"], "symbol": rec["symbol"], "tf": rec["tf"],
            "direction": rec["direction"], "verdict": rec.get("verdict"),
            "score": rec.get("score"), "ml_prob": rec.get("ml_prob"),
            "entry": rec.get("entry"), "stop_loss": rec.get("stop_loss"),
            "take_profit": rec.get("take_profit"), "rr": rec.get("rr"),
            "order_type": rec.get("order_type"), "lot": rec.get("lot"),
            "requested_price": rec.get("requested_price"),
            "fill_price": rec.get("fill_price"),
            "ticket": rec.get("ticket"), "deal": rec.get("deal"),
            "retcode": rec.get("retcode"), "status": rec["status"],
            "reason": rec.get("reason"), "placed_at": int(time.time()),
            "payload": json.dumps(payload, default=str),
        }

    # --------------------------------------------------------- management
    def status(self) -> dict:
        """Dashboard/CLI status snapshot (safe when MT5 is unreachable)."""
        out = {
            "enabled": self.enabled, "dry_run": self.dry_run, "magic": self.magic,
            "comment": self.x.get("comment") or "ai-trader",
            "entry_type": self.x.get("entry_type", "market"),
            "risk_percent": self._float("risk_percent", 1.0),
            "fixed_lot": self.x.get("fixed_lot"),
            "min_score": self._float("min_score", 70),
            "min_ml_prob": self._float("min_ml_prob", 0.50),
            "max_open_positions": self._int("max_open_positions", 3),
            "max_per_symbol": self._int("max_per_symbol", 1),
            "max_trades_per_day": self._int("max_trades_per_day", 5),
            "cooldown_minutes": self._float("cooldown_minutes", 240),
            "state": {k: v for k, v in self.state.items() if k != "last_error"},
            "last_error": self.state.get("last_error"),
            "account": None, "positions": [], "pendings": [],
        }
        if not self.enabled:
            return out
        try:
            mt5 = self._connect()
            out["account"] = mt5_trader.account_snapshot(mt5)
            out["positions"] = [mt5_trader.position_dict(p)
                                for p in mt5_trader.bot_positions(mt5, self.magic)]
            out["pendings"] = [mt5_trader.order_dict(o)
                               for o in mt5_trader.bot_pendings(mt5, self.magic)]
        except Exception as e:
            out["mt5_error"] = str(e)
        return out

    def flatten(self) -> dict:
        """Panic button: close every bot position and cancel every bot pending.
        Never touches manual trades (magic-number filter)."""
        closed, canceled, errors = [], [], []
        try:
            mt5 = self._connect()
        except Exception as e:
            return {"closed": [], "canceled": [], "errors": [str(e)]}
        for p in mt5_trader.bot_positions(mt5, self.magic):
            r = mt5_trader.close_position(mt5, p, self.magic, "flatten")
            (closed if r["ok"] else errors).append(
                {"ticket": int(p.ticket), "symbol": p.symbol, **r})
        for o in mt5_trader.bot_pendings(mt5, self.magic):
            r = mt5_trader.cancel_order(mt5, int(o.ticket))
            (canceled if r["ok"] else errors).append(
                {"ticket": int(o.ticket), "symbol": o.symbol, **r})
        return {"closed": closed, "canceled": canceled, "errors": errors}
