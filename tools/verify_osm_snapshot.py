#!/usr/bin/env python3
"""Check OSM staging SQLite integrity, unique IDs, attribution, bounds and coverage."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def verify(db: Path, min_active=1, require_full=False):
    if not db.is_file():
        raise FileNotFoundError(db)
    con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        active = con.execute("SELECT count(*) FROM osm_places WHERE is_active=1").fetchone()[0]
        duplicate = con.execute("""SELECT count(*) FROM (
            SELECT osm_type,osm_id FROM osm_places GROUP BY osm_type,osm_id HAVING count(*)>1)""").fetchone()[0]
        wrong_license = con.execute("SELECT count(*) FROM osm_places WHERE source_licence != 'ODbL-1.0'").fetchone()[0]
        invalid_coordinates = con.execute("""SELECT count(*) FROM osm_places WHERE
            lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180
            OR lat IS NULL OR lon IS NULL""").fetchone()[0]
        snapshots = con.execute("SELECT count(*) FROM ingestion_snapshots").fetchone()[0]
        breakdown = {row[0]: row[1] for row in con.execute("SELECT osm_type, count(*) FROM osm_places WHERE is_active=1 GROUP BY osm_type")}
        # Full-import evidence: a snapshot recorded with a full scope (no --limit) proves the whole
        # source was processed; a pilot snapshot (limit=...) cannot certify national coverage.
        latest_cur = con.execute(
            "SELECT id, filename, file_sha256, mode, source_date, imported_at, inserted, updated, unchanged, skipped, retired "
            "FROM ingestion_snapshots ORDER BY imported_at DESC, rowid DESC LIMIT 1"
        )
        latest_row = latest_cur.fetchone()
        latest = dict(zip([d[0] for d in latest_cur.description], latest_row, strict=False)) if latest_row else None
        full_snapshots = con.execute("SELECT count(*) FROM ingestion_snapshots WHERE mode LIKE '%:full:%'").fetchone()[0]
        result = {
            "database": str(db),
            "integrity": integrity,
            "active": active,
            "unique_id_duplicates": duplicate,
            "invalid_coordinates": invalid_coordinates,
            "wrong_osm_licence": wrong_license,
            "snapshots": snapshots,
            "full_snapshots": full_snapshots,
            "latest_snapshot": latest,
            "types": breakdown,
            "minimum_required": min_active,
            "require_full": require_full,
            "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        }
        result["pass"] = (
            integrity == "ok"
            and active >= min_active
            and snapshots >= 1
            and duplicate == 0
            and wrong_license == 0
            and invalid_coordinates == 0
            and (not require_full or full_snapshots >= 1)
        )
        return result
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(description="Validate fully imported OSM SQLite staging DB")
    p.add_argument("--db", type=Path, default=Path("data/osm_vietnam.sqlite3"))
    p.add_argument("--min-active", type=int, default=1, help="expected min real records; choose based on verified source")
    p.add_argument("--require-full", action="store_true", help="fail unless at least one full-scope snapshot (import without --limit) exists")
    a = p.parse_args()
    try:
        obj = verify(a.db, a.min_active, a.require_full)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return 0 if obj["pass"] else 2
    except (OSError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
