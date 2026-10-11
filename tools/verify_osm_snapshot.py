#!/usr/bin/env python3
"""Check OSM staging SQLite integrity, unique IDs, attribution, bounds and coverage."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def verify(db: Path, min_active=1):
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
        result = {
            "database": str(db),
            "integrity": integrity,
            "active": active,
            "unique_id_duplicates": duplicate,
            "invalid_coordinates": invalid_coordinates,
            "wrong_osm_licence": wrong_license,
            "snapshots": snapshots,
            "types": breakdown,
            "minimum_required": min_active,
            "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        }
        result["pass"] = (
            integrity == "ok" and active >= min_active and snapshots >= 1 and duplicate == 0 and wrong_license == 0 and invalid_coordinates == 0
        )
        return result
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(description="Validate fully imported OSM SQLite staging DB")
    p.add_argument("--db", type=Path, default=Path("data/osm_vietnam.sqlite3"))
    p.add_argument("--min-active", type=int, default=1, help="expected min real records; choose based on verified source")
    a = p.parse_args()
    try:
        obj = verify(a.db, a.min_active)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return 0 if obj["pass"] else 2
    except (OSError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
