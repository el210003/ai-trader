"""SQLite persistence for OHLCV candles and analysis snapshots."""
import json
import sqlite3
from pathlib import Path

import pandas as pd


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create()

    def _create(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candles(
                symbol TEXT NOT NULL,
                tf     TEXT NOT NULL,
                time   INTEGER NOT NULL,
                open   REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY(symbol, tf, time)
            );
            CREATE TABLE IF NOT EXISTS analysis(
                symbol     TEXT NOT NULL,
                tf         TEXT NOT NULL,
                updated_at INTEGER,
                payload    TEXT,
                PRIMARY KEY(symbol, tf)
            );
            CREATE TABLE IF NOT EXISTS csm(
                id         INTEGER PRIMARY KEY CHECK(id = 1),
                updated_at INTEGER,
                payload    TEXT
            );
            CREATE TABLE IF NOT EXISTS setups_history(
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT NOT NULL,
                tf         TEXT NOT NULL,
                logged_at  INTEGER NOT NULL,
                formed_at  INTEGER NOT NULL,
                direction  TEXT,
                verdict    TEXT,
                score      REAL,
                ml_prob    REAL,
                rr         REAL,
                entry      REAL, high REAL, low REAL, close REAL,
                stop_loss  REAL, take_profit REAL,
                payload    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_pair_time
                ON setups_history(symbol, tf, formed_at);
            CREATE TABLE IF NOT EXISTS outcomes(
                setup_id    INTEGER PRIMARY KEY,
                symbol      TEXT NOT NULL,
                tf          TEXT NOT NULL,
                direction   TEXT,
                resolved_at INTEGER,
                result      TEXT,
                filled      INTEGER,
                fill_time   INTEGER,
                r_multiple  REAL,
                mfe_r       REAL,
                mae_r       REAL,
                bars_to_outcome INTEGER
            );
            """
        )
        # migration: post_loss_tp_hit added in the outcome-driven improvement
        try:
            self.conn.execute("ALTER TABLE outcomes ADD COLUMN post_loss_tp_hit INTEGER")
            self.conn.commit()
        except Exception:
            pass   # column already exists
        # migration: journal identity (setup identity dedup) + first-formed time
        for col, typ in (("identity", "TEXT"), ("first_formed_at", "INTEGER")):
            try:
                self.conn.execute(f"ALTER TABLE setups_history ADD COLUMN {col} {typ}")
                self.conn.commit()
            except Exception:
                pass   # column already exists
        self.conn.commit()

    # ------------------------------------------------------------- candles
    def upsert_candles(self, df: pd.DataFrame, symbol: str, tf: str):
        rows = [
            (symbol, tf, int(r.time), float(r.open), float(r.high),
             float(r.low), float(r.close), float(getattr(r, "volume", 0.0) or 0.0))
            for r in df.itertuples()
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO candles(symbol,tf,time,open,high,low,close,volume) "
            "VALUES(?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def load_candles(self, symbol: str, tf: str) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT time,open,high,low,close,volume FROM candles "
            "WHERE symbol=? AND tf=? ORDER BY time ASC",
            self.conn, params=(symbol, tf))
        return df

    def candle_count(self, symbol: str, tf: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol=? AND tf=?", (symbol, tf))
        return cur.fetchone()[0]

    # ------------------------------------------------------------ analysis
    def save_analysis(self, symbol: str, tf: str, payload: dict):
        import time as _t
        self.conn.execute(
            "INSERT OR REPLACE INTO analysis(symbol,tf,updated_at,payload) VALUES(?,?,?,?)",
            (symbol, tf, int(_t.time()), json.dumps(payload)))
        self.conn.commit()

    def load_analysis(self, symbol: str, tf: str):
        cur = self.conn.execute(
            "SELECT payload,updated_at FROM analysis WHERE symbol=? AND tf=?", (symbol, tf))
        row = cur.fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def list_analyses(self):
        cur = self.conn.execute("SELECT symbol,tf,updated_at FROM analysis ORDER BY symbol,tf")
        return [{"symbol": r[0], "tf": r[1], "updated_at": r[2]} for r in cur.fetchall()]

    # ---------------------------------------------------------------- csm
    def save_csm(self, payload: dict):
        import time as _t
        self.conn.execute(
            "INSERT OR REPLACE INTO csm(id,updated_at,payload) VALUES(1,?,?)",
            (int(_t.time()), json.dumps(payload)))
        self.conn.commit()

    def load_csm(self):
        cur = self.conn.execute("SELECT payload FROM csm WHERE id = 1")
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------ setups history
    def append_setups(self, symbol: str, tf: str, setups: list, logged_at: int):
        """Journal setups for forward-validation.

        Dedupes on the REAL setup identity (symbol|tf|direction|zone.origin_time|entry)
        so a persistent setup is logged ONCE and updated in place on later cycles
        instead of being re-inserted every bar (which inflated the journal 3-6x).
        `first_formed_at` keeps the original formation time for the resolver's
        entry-validity window, so recurring setups still expire on schedule.
        """
        def _row(s):
            p = json.dumps({k: s.get(k) for k in
                            ("confluences", "entry_zone", "range_position",
                             "llm_score", "aligned", "score_source",
                             "features", "htf_metrics",
                             "fill_tolerance", "atr", "entry_distance_atr")})
            zone = s.get("entry_zone") or {}
            origin = int(zone.get("origin_time") or 0)
            identity = f"{symbol}|{tf}|{s.get('direction')}|{origin}|{float(s.get('entry') or 0):.8f}"
            return (symbol, tf, int(logged_at), int(s.get("formed_at", logged_at)),
                    s.get("direction"), s.get("verdict"), s.get("final_score"),
                    s.get("ml_prob"), s.get("rr"), s.get("entry"), None, None,
                    s.get("last_close"), s.get("stop_loss"), s.get("take_profit"),
                    p, identity)

        rows = [_row(s) for s in setups if s.get("entry") is not None]
        if not rows:
            return 0

        inserts = []
        for r in rows:
            identity, formed = r[16], r[3]
            cur = self.conn.execute("SELECT id, first_formed_at FROM setups_history WHERE identity=?",
                                    (identity,))
            ex = cur.fetchone()
            if ex:
                # update in place: latest verdict/score on the existing row
                self.conn.execute(
                    "UPDATE setups_history SET verdict=?, score=?, ml_prob=?, close=?, "
                    "logged_at=? WHERE id=?",
                    (r[5], r[6], r[7], r[12], r[2], ex[0]))
            else:
                inserts.append(r)

        if inserts:
            self.conn.executemany(
                "INSERT INTO setups_history(symbol,tf,logged_at,formed_at,direction,verdict,"
                "score,ml_prob,rr,entry,high,low,close,stop_loss,take_profit,payload,identity,"
                "first_formed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [r[0:16] + (r[16], r[3]) for r in inserts])  # + identity, first_formed_at=formed_at

        self.conn.commit()
        self._backfill_identities()
        return len(inserts)

    def _backfill_identities(self):
        """One-time backfill of identity/first_formed_at for pre-existing rows
        that predate the identity columns (old dedup used formed_at)."""
        try:
            rows = self.conn.execute(
                "SELECT id, symbol, tf, direction, entry, payload, formed_at FROM setups_history "
                "WHERE identity IS NULL").fetchall()
            for _id, sym, tf, _d, entry, payload, formed in rows:
                origin = 0
                try:
                    p = json.loads(payload) if payload else {}
                    origin = int((p.get("entry_zone") or {}).get("origin_time") or 0)
                except Exception:
                    pass
                ident = f"{sym}|{tf}|{_d}|{origin}|{float(entry or 0):.8f}"
                self.conn.execute("UPDATE setups_history SET identity=?, first_formed_at=? "
                                  "WHERE id=?", (ident, formed, _id))
            self.conn.commit()
        except Exception:
            pass

    def load_setups_history(self, symbol: str = None, tf: str = None,
                            limit: int = 200) -> list:
        q = ("SELECT h.id,h.symbol,h.tf,h.logged_at,h.formed_at,h.direction,h.verdict,h.score,"
             "h.ml_prob,h.rr,h.entry,h.close,h.stop_loss,h.take_profit,h.payload,"
             "o.setup_id AS outcome_id, o.result,o.filled,o.r_multiple,o.mfe_r,o.mae_r,o.bars_to_outcome "
             "FROM setups_history h LEFT JOIN outcomes o ON o.setup_id = h.id")
        params = []
        conds = []
        if symbol:
            conds.append("h.symbol=?"); params.append(symbol)
        if tf:
            conds.append("h.tf=?"); params.append(tf)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY h.formed_at DESC, h.id DESC LIMIT ?"
        params.append(int(limit))
        cur = self.conn.execute(q, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def outcome_stats(self, symbol: str = None, tf: str = None) -> dict:
        """Journal strip summary over RESOLVED outcomes (win rate, expectancy,
        fill rate, open count) plus log counts. Full aggregates live in
        app/engine/outcomes.py::aggregate (Performance tab)."""
        rows = self.load_setups_history(symbol, tf, limit=100000)
        from collections import Counter
        by_verdict = Counter(r["verdict"] for r in rows)
        resolved = [r for r in rows if r.get("result") in ("WIN", "LOSS")]
        wins = [r for r in resolved if r["result"] == "WIN"]
        rs = [r["r_multiple"] for r in resolved if r["r_multiple"] is not None]
        fills = [r for r in rows if r.get("filled") is not None]
        return {
            "total": len(rows),
            "by_verdict": dict(by_verdict),
            "by_direction": dict(Counter(r["direction"] for r in rows)),
            "resolved": len(resolved),
            "win_rate": round(len(wins) / len(resolved), 3) if resolved else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "fill_rate": round(sum(1 for r in fills if r["filled"]) / len(fills), 3) if fills else None,
            "open": sum(1 for r in rows if r.get("outcome_id") is None),
            "expired": sum(1 for r in rows if (r.get("result") or "").startswith("EXPIRED")),
        }

    # ------------------------------------------------------------ outcomes
    def save_outcome(self, o: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO outcomes(setup_id,symbol,tf,direction,resolved_at,"
            "result,filled,fill_time,r_multiple,mfe_r,mae_r,bars_to_outcome,post_loss_tp_hit) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["setup_id"], o["symbol"], o["tf"], o.get("direction"),
             o.get("resolved_at"), o.get("result"), o.get("filled"),
             o.get("fill_time"), o.get("r_multiple"), o.get("mfe_r"),
             o.get("mae_r"), o.get("bars_to_outcome"), o.get("post_loss_tp_hit")))
        self.conn.commit()

    def pending_setups(self, lookback_days: int = 30) -> list:
        """Journaled setups without a resolved outcome, newest first."""
        import time as _t
        cutoff = int(_t.time()) - int(lookback_days) * 86400
        cur = self.conn.execute(
            "SELECT h.id, h.symbol, h.tf, h.direction, h.formed_at, h.first_formed_at, "
            "h.entry, h.stop_loss, h.take_profit, h.rr, h.verdict, h.score, h.ml_prob, h.payload "
            "FROM setups_history h LEFT JOIN outcomes o ON o.setup_id = h.id "
            "WHERE o.setup_id IS NULL AND COALESCE(h.first_formed_at, h.formed_at) >= ? "
            "ORDER BY h.formed_at ASC", (cutoff,))
        cols = ["id", "symbol", "tf", "direction", "formed_at", "first_formed_at",
                "entry", "stop_loss", "take_profit", "rr", "verdict", "score",
                "ml_prob", "payload"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def load_outcome_rows(self, symbol: str = None, tf: str = None,
                          limit: int = 20000) -> list:
        """Resolved setups joined with their outcomes (for aggregation)."""
        q = ("SELECT h.id, h.symbol, h.tf, h.formed_at, h.direction, h.verdict, "
             "h.score, h.ml_prob, h.rr, h.entry, h.stop_loss, h.take_profit, h.payload, "
             "o.result, o.filled, o.fill_time, o.r_multiple, o.mfe_r, o.mae_r, "
             "o.bars_to_outcome, o.resolved_at, o.post_loss_tp_hit "
             "FROM outcomes o JOIN setups_history h ON h.id = o.setup_id")
        params = []
        conds = []
        if symbol:
            conds.append("h.symbol=?"); params.append(symbol)
        if tf:
            conds.append("h.tf=?"); params.append(tf)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY h.formed_at DESC LIMIT ?"
        params.append(int(limit))
        cur = self.conn.execute(q, params)
        cols = ["id", "symbol", "tf", "formed_at", "direction", "verdict",
                "score", "ml_prob", "rr", "entry", "stop_loss", "take_profit",
                "payload", "result", "filled", "fill_time", "r_multiple",
                "mfe_r", "mae_r", "bars_to_outcome", "resolved_at", "post_loss_tp_hit"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def outcome_history_for_symbol(self, symbol: str, limit: int = 500) -> list:
        """Resolved WIN/LOSS outcomes for one symbol, oldest first — feeds the
        live per-symbol dynamic features (rolling win rate, realized RR)."""
        cur = self.conn.execute(
            "SELECT h.formed_at, o.result, o.r_multiple "
            "FROM outcomes o JOIN setups_history h ON h.id = o.setup_id "
            "WHERE o.symbol = ? AND o.result IN ('WIN','LOSS') "
            "ORDER BY h.formed_at ASC LIMIT ?", (symbol, int(limit)))
        return [{"formed_at": r[0], "result": r[1], "r_multiple": r[2]}
                for r in cur.fetchall()]

    def live_training_samples(self) -> list:
        """Resolved live outcomes with their journaled features — tier-3
        training blend. WIN -> 1, LOSS -> 0 (labeler semantics)."""
        cur = self.conn.execute(
            "SELECT h.symbol, h.tf, h.formed_at, h.direction, h.payload, o.result "
            "FROM outcomes o JOIN setups_history h ON h.id = o.setup_id "
            "WHERE o.result IN ('WIN','LOSS') ORDER BY h.formed_at ASC")
        out = []
        for symbol, tf, formed_at, direction, payload, result in cur.fetchall():
            try:
                p = json.loads(payload) if payload else {}
            except Exception:
                continue
            feats = p.get("features")
            if not feats:
                continue   # pre-tier-0 rows have no journaled features
            out.append({"symbol": symbol, "tf": tf, "formed_at": formed_at,
                        "direction": direction, "features": feats,
                        "label": 1 if result == "WIN" else 0})
        return out

    def close(self):
        self.conn.close()
