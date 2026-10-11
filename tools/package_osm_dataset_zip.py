#!/usr/bin/env python3
"""Pack a locally populated OSM-only SQLite dataset as a portable ZIP.

Uses SQLite's online backup to make a consistent snapshot (not a raw WAL copy).
DO NOT include enterprise registry, user content, Google data or unreleased data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

RIGHTS = """Vietnam Atlas — OpenStreetMap-derived points of interest

© OpenStreetMap contributors; ODbL 1.0.
License: https://opendatacommons.org/licenses/odbl/1-0/
Attribution: https://www.openstreetmap.org/copyright
Source: https://download.geofabrik.de/asia/vietnam.html

Only named POIs matched by this importer. Not a complete register of Vietnamese
businesses, not proof of a license to operate or a guarantee of current status.
Any exported modified/derived OSM database remains governed by ODbL obligations.
The project's PolyForm Noncommercial software licence does not override ODbL.
"""


def hash_file(path):
    h = hashlib.sha256()
    # Path is the tool's own --db/--provenance/--include-pbf argument (local operator CLI run).
    with Path(path).open("rb") as f:  # NOSONAR(S8707)
        for chunk in iter(lambda: f.read(2 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def package(db: Path, out: Path, provenance: Path | None = None, include_pbf: Path | None = None):
    if not db.is_file():
        raise FileNotFoundError(f"Database chưa có: {db}. Nhập Geofabrik PBF trước khi đóng ZIP dữ liệu")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmpzip = out.with_name(out.name + ".partial")
    with tempfile.TemporaryDirectory(prefix="vn-atlas-export-") as td:
        snap = Path(td) / "osm_vietnam.sqlite3"
        with closing(sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(snap)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite online backup integrity_check failed")
            count = dst.execute("SELECT count(*) FROM osm_places WHERE is_active=1").fetchone()[0]
            if count <= 0:
                raise RuntimeError("Không có POI thật hoặc test record trong DB; từ chối tạo ZIP dữ liệu rỗng")
            snapshots = dst.execute("SELECT count(*) FROM ingestion_snapshots").fetchone()[0]
        report = {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "active_poi_records": count,
            "snapshot_count": snapshots,
            "db_sha256": hash_file(snap),
            "license": "ODbL-1.0",
            "not_an_official_business_registry": True,
            "source_metadata_included": bool(provenance and provenance.is_file()),
            "pbf_included": bool(include_pbf and include_pbf.is_file()),
        }
        try:
            with zipfile.ZipFile(tmpzip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
                z.write(snap, "data/osm_vietnam.sqlite3")
                z.writestr("DATA_RIGHTS.txt", RIGHTS)
                z.writestr("manifest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                if provenance and provenance.is_file():
                    z.write(provenance, "data/pbf_provenance.json")
                if include_pbf:
                    if not include_pbf.is_file():
                        raise FileNotFoundError(include_pbf)
                    z.write(include_pbf, "data/vietnam-latest.osm.pbf")
            with zipfile.ZipFile(tmpzip) as z:
                if z.testzip() is not None:
                    raise RuntimeError("ZIP integrity check failed")
            tmpzip.replace(out)  # NOSONAR(S8707)
        except BaseException:
            tmpzip.unlink(missing_ok=True)
            raise
    return {"zip_path": str(out), "bytes": out.stat().st_size, "sha256": hash_file(out), **report}


def main():
    p = argparse.ArgumentParser(description="Make a portable ZIP of the real locally imported ODbL POI SQLite dataset")
    p.add_argument("--db", type=Path, default=Path("data/osm_vietnam.sqlite3"))
    p.add_argument("--out", type=Path, default=Path("backups/Vietnam_Atlas_OSM_DATA.zip"))
    p.add_argument("--provenance", type=Path, default=Path("data/vietnam-latest.osm.pbf.provenance.json"))
    p.add_argument("--include-pbf", type=Path, help="optionally include raw 314MB+ PBF (large archive)")
    a = p.parse_args()
    try:
        print(json.dumps(package(a.db, a.out, a.provenance, a.include_pbf), ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
