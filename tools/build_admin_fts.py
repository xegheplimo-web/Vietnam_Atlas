#!/usr/bin/env python3
"""Build (or verify) the admin_fts FTS5 index inside data/gazetteer.sqlite3.

The gazetteer schema (admin_unit / admin_alias / admin_relation) is owned by
tools/import_gazetteer.py — this tool only ADDS one virtual table used by
tools/resolve_admin.py for accent-insensitive toponym matching:

    admin_fts(text, text_norm, kind, unit_id, code, level, unit_type,
              valid_from, valid_to)   tokenizer: unicode61 remove_diacritics 2

Rows: one 'unit' row per admin_unit (text=name, text_norm=name_norm, unit's own
code/level/validity) + one 'alias' row per admin_alias joined to its unit
(text=alias, text_norm=alias_norm, the PARENT unit's code/level/validity — a
candidate's validity is the unit's, not the alias's).

Idempotent behaviour (chosen semantics): without --rebuild the tool compares the
FTS row counts with the live table counts and is a no-op when they already
match; any drift (or --rebuild) drops and repopulates the table. IMPORTANT: the
importer drops and recreates the whole database, so after every re-run of
import_gazetteer.py this tool MUST be run again.

Usage:
    py -3 tools/build_admin_fts.py --db data/gazetteer.sqlite3 [--rebuild]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS admin_fts USING fts5(
    text, text_norm, kind,
    unit_id UNINDEXED, code UNINDEXED, level UNINDEXED,
    unit_type UNINDEXED, valid_from UNINDEXED, valid_to UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
)
"""

UNIT_ROWS = """
INSERT INTO admin_fts (text, text_norm, kind, unit_id, code, level, unit_type, valid_from, valid_to)
SELECT name, name_norm, 'unit', id, code, level, unit_type, valid_from, valid_to FROM admin_unit
"""

ALIAS_ROWS = """
INSERT INTO admin_fts (text, text_norm, kind, unit_id, code, level, unit_type, valid_from, valid_to)
SELECT a.alias, a.alias_norm, 'alias', u.id, u.code, u.level, u.unit_type, u.valid_from, u.valid_to
FROM admin_alias a JOIN admin_unit u ON u.id = a.unit_id
"""


def expected_counts(con) -> dict:
    """Rows the index must hold: one per admin_unit plus one per admin_alias."""
    return {
        "unit": con.execute("SELECT COUNT(*) FROM admin_unit").fetchone()[0],
        "alias": con.execute("SELECT COUNT(*) FROM admin_alias").fetchone()[0],
    }


def fts_counts(con) -> dict:
    return {row[0]: row[1] for row in con.execute("SELECT kind, COUNT(*) FROM admin_fts GROUP BY kind")}


def has_fts(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_fts'").fetchone() is not None


def build_admin_fts(db_path: Path, rebuild: bool = False) -> dict:
    """Ensure admin_fts mirrors admin_unit+admin_alias; returns a status/counts dict.

    Runs as one BEGIN IMMEDIATE transaction with foreign_key_check before commit
    (same precedent as import_gazetteer.write_db): a failure rolls back cleanly
    and never leaves a half-filled index.
    """
    con = sqlite3.connect(str(db_path))  # NOSONAR(S8706)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        expected = expected_counts(con)
        if has_fts(con) and not rebuild and fts_counts(con) == expected:
            con.commit()
            return {"status": "noop", "counts": expected}
        existed = has_fts(con)
        con.execute("DROP TABLE IF EXISTS admin_fts")
        con.execute(FTS_DDL)
        con.execute(UNIT_ROWS)
        con.execute(ALIAS_ROWS)
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"foreign_key_check failed: {violations[:5]}")
        con.commit()
        return {"status": "rebuilt" if existed else "built", "counts": expected}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the admin_fts FTS5 index inside a gazetteer DB")
    p.add_argument("--db", type=Path, default=Path("data/gazetteer.sqlite3"))
    p.add_argument("--rebuild", action="store_true", help="drop and repopulate even when counts already match")
    a = p.parse_args(argv)
    try:
        result = build_admin_fts(a.db, a.rebuild)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
