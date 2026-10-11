#!/usr/bin/env python3
"""Export ODbL-only POI SQLite rows to streaming gzip CSV with attribution.

Do not claim completeness. Does not export any user-owned or registry data.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ["osm_type", "osm_id", "name", "category", "lat", "lon", "address", "phone", "website", "source_licence", "source_url", "last_seen"]


def export(db: Path, target: Path):
    if not db.is_file():
        raise FileNotFoundError(db)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    dbcon = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    dbcon.row_factory = sqlite3.Row
    count = 0
    try:
        with gzip.open(temp, "wt", encoding="utf-8", newline="", compresslevel=6) as out:
            writer = csv.DictWriter(out, fieldnames=FIELDS)
            writer.writeheader()
            for row in dbcon.execute("SELECT " + ",".join(FIELDS) + " FROM osm_places WHERE is_active=1 ORDER BY osm_type,osm_id"):
                # Escape spreadsheet formula prefixes in text fields without changing DB content.
                r = dict(row)
                for field in ("name", "category", "address", "phone", "website"):
                    value = str(r[field] or "")
                    if value.lstrip().startswith(("=", "+", "-", "@")):
                        value = "'" + value
                    r[field] = value
                writer.writerow(r)
                count += 1
        # Output path is the tool's own --out argument (local operator CLI run).
        temp.replace(target)  # NOSONAR(S8707)
        rights = target.with_name(target.name + ".README.json")
        rights.write_text(  # NOSONAR(S8707)
            json.dumps(
                {
                    "file": target.name,
                    "records": count,
                    "generated_utc": datetime.now(timezone.utc).isoformat(),
                    "data_source": "© OpenStreetMap contributors",
                    "license": "ODbL 1.0",
                    "license_url": "https://www.openstreetmap.org/copyright",
                    "note": "Only OpenStreetMap POIs, not an official Vietnamese enterprise register; ODbL obligations apply",
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return {"export_file": str(target), "records": count, "rights_file": str(rights)}
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        dbcon.close()


def main():
    p = argparse.ArgumentParser(description="Export OSM POI data as gzip CSV, ODbL attributed")
    p.add_argument("--db", type=Path, default=Path("data/osm_vietnam.sqlite3"))
    p.add_argument("--out", type=Path, default=Path("backups/vietnam_osm_places.csv.gz"))
    a = p.parse_args()
    try:
        print(json.dumps(export(a.db, a.out), indent=2, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
