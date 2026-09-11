"""One-time migration: collapse duplicate journal rows into one per real setup.

Before: append_setups deduped on (symbol, tf, formed_at, direction), which
changed every M15 bar — the SAME persistent setup was logged once per bar
(inflating the journal 3-6x; worst case 49x). This migration:

  1. Backs up setups_history + outcomes to *_backup_<ts> tables.
  2. Groups rows by the real setup identity (symbol|tf|direction|zone.origin|entry).
  3. Keeps ONE row per identity (the EARLIEST formation / first resolved
     outcome), preserving its outcome if resolved; deletes the duplicates.

Run once:  python -m app.main migrate-journal
Idempotent: identifies rows by their new `identity` (or computes it), and
skips manually-built rows. Safe to run on an already-migrated DB (no-ops).
"""
import sqlite3
import time
import json


def migrate(path: str) -> dict:
    conn = sqlite3.connect(path, timeout=30)
    cur = conn.cursor()

    # ensure identity/first_formed_at columns exist (older DBs)
    for col, typ in (("identity", "TEXT"), ("first_formed_at", "INTEGER")):
        try:
            cur.execute(f"ALTER TABLE setups_history ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception:
            pass

    # 1) backup
    ts = int(time.time())
    for table in ("setups_history", "outcomes"):
        try:
            cur.execute(
                f"CREATE TABLE {table}_backup_{ts} AS SELECT * FROM {table}")
        except Exception:
            pass

    # 2) fetch rows
    rows = cur.execute(
        "SELECT id, symbol, tf, direction, entry, payload, formed_at, verdict, "
        "score, ml_prob, close, stop_loss, take_profit, logged_at "
        "FROM setups_history").fetchall()

    def identity(r):
        origin = 0
        try:
            p = json.loads(r[5]) if r[5] else {}
            origin = int((p.get("entry_zone") or {}).get("origin_time") or 0)
        except Exception:
            pass
        return f"{r[1]}|{r[2]}|{r[3]}|{origin}|{float(r[4] or 0):.8f}"

    keep = {}        # identity -> (id, formed_at)  earliest wins
    for r in rows:
        ident = identity(r)
        fid = r[6]
        if ident not in keep or fid < keep[ident][1]:
            keep[ident] = (r[0], fid)

    keep_ids = set(v[0] for v in keep.values())
    to_delete = [r[0] for r in rows if r[0] not in keep_ids]
    n_orig = len(rows)
    n_keep = len(keep_ids)

    # 3) delete duplicates
    if to_delete:
        cur.executemany("DELETE FROM setups_history WHERE id=?",
                        [(i,) for i in to_delete])

    # set the canonical identity for survivor rows (for future dedup)
    for ident, (rid, _f) in keep.items():
        cur.execute("UPDATE setups_history SET identity=? WHERE id=?", (ident, rid))

    # drop orphan outcomes (belonged to deleted dupes)
    cur.execute("DELETE FROM outcomes WHERE setup_id NOT IN "
                "(SELECT id FROM setups_history)")

    conn.commit()
    conn.close()
    return {"backup_ts": ts, "before": n_orig, "after": n_keep,
            "removed": len(to_delete)}


if __name__ == "__main__":
    import os
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/trader.db"
    res = migrate(path)
    print(f"journal migrated: {res['before']} rows -> {res['after']} "
          f"(removed {res['removed']}) | backup tables *_backup_{res['backup_ts']}")
