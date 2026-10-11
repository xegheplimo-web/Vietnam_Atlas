#!/usr/bin/env python3
"""Safe OSM dataset -> existing Vietnam_Atlas SQLite sync.

Defaults to dry-run, only inserts new OpenStreetMap source IDs, keeps verified
and user-created records untouched; makes an online backup before any write.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sync(osm_db: Path, atlas_db: Path, apply: bool = False, update_existing: bool = False, limit: int = 0, backups: Path | None = None):
    if not osm_db.is_file():
        raise FileNotFoundError(f"Chưa có dữ liệu OSM: {osm_db}. Hãy tải PBF và nhập snapshot trước.")
    if limit < 0:
        raise ValueError("--limit không thể âm")
    from server.store import init_db, save_place  # only available inside full Vietnam_Atlas repository

    if apply and atlas_db.is_file():
        preview = sync(osm_db, atlas_db, apply=False, update_existing=update_existing, limit=limit)
        if preview["would_insert"] == 0 and preview["would_update"] == 0:
            preview["mode"] = "APPLY_NO_CHANGES"
            return preview  # avoids repeated multi-GB backups on unchanged daily snapshots

    src = sqlite3.connect(f"file:{osm_db.resolve().as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dest = None
    result = {
        "mode": "APPLY" if apply else "DRY_RUN",
        "would_insert": 0,
        "existing": 0,
        "would_update": 0,
        "protected": 0,
        "inserted": 0,
        "updated": 0,
        "skipped_invalid": 0,
        "backup": None,
    }
    try:
        if apply:
            atlas_db.parent.mkdir(parents=True, exist_ok=True)
            if atlas_db.is_file():
                backups = backups or atlas_db.parent.parent / "backups"
                backups.mkdir(parents=True, exist_ok=True)
                snap = backups / ("atlas-pre-osm-sync-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3")
                # Both paths come from this tool's own CLI flags, run locally by the operator.
                with closing(sqlite3.connect(atlas_db)) as live, closing(sqlite3.connect(snap)) as backup:  # NOSONAR(S8706)
                    live.backup(backup)  # WAL-safe SQLite online backup
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Backup database integrity_check FAIL")
                result["backup"] = str(snap)
            init_db(str(atlas_db))
            dest = sqlite3.connect(atlas_db)  # NOSONAR(S8706)
        elif atlas_db.is_file():
            dest = sqlite3.connect(f"file:{atlas_db.resolve().as_posix()}?mode=ro", uri=True)
        if dest is not None:
            dest.row_factory = sqlite3.Row
        rows = src.execute(
            "SELECT * FROM osm_places WHERE is_active=1 ORDER BY osm_type,osm_id" + (" LIMIT ?" if limit else ""), ([limit] if limit else [])
        )
        if apply:
            dest.execute("BEGIN IMMEDIATE")
        for row in rows:
            sid = f"{row['osm_type']}/{row['osm_id']}"
            previous = None
            if dest is not None:
                previous = dest.execute("SELECT * FROM places WHERE source='OpenStreetMap' AND source_id=?", (sid,)).fetchone()
            if previous is None:
                result["would_insert"] += 1
                if apply:
                    payload = {
                        "name": row["name"],
                        "category": row["category"],
                        "address": row["address"],
                        "province": "",
                        "phone": row["phone"],
                        "website": row["website"],
                        "lat": row["lat"],
                        "lon": row["lon"],
                        "notes": "",
                        "source": "OpenStreetMap",
                        "source_id": sid,
                        "status": "unverified",
                    }
                    save_place(dest, payload)
                    result["inserted"] += 1
                continue
            result["existing"] += 1
            if not update_existing or previous["status"] != "unverified":
                result["protected"] += 1
                continue
            fields = {
                "name": row["name"],
                "category": row["category"],
                "address": row["address"],
                "phone": row["phone"],
                "website": row["website"],
                "lat": row["lat"],
                "lon": row["lon"],
            }
            # Always preserve manually added province, notes, status and distinct user sources.
            if any(previous[k] != v for k, v in fields.items()):
                result["would_update"] += 1
                if apply:
                    payload = dict(previous)
                    payload.update(fields)
                    save_place(dest, payload, place_id=previous["id"])
                    result["updated"] += 1
        if apply:
            dest.commit()
        return result
    except BaseException:
        if apply and dest is not None:
            dest.rollback()
        raise
    finally:
        if dest is not None:
            dest.close()
        src.close()


def main():
    p = argparse.ArgumentParser(description="Sync independent OSM dataset into Vietnam_Atlas; DRY RUN by default")
    p.add_argument("--osm-db", type=Path, default=Path("data/osm_vietnam.sqlite3"))
    p.add_argument("--atlas-db", type=Path, default=Path("data/atlas.sqlite3"))
    p.add_argument("--apply", action="store_true", help="explicitly apply inserts after online backup")
    p.add_argument("--update-existing", action="store_true", help="update only unverified OSM rows; never verified/user records")
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args()
    try:
        print(json.dumps(sync(a.osm_db, a.atlas_db, a.apply, a.update_existing, a.limit), indent=2, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
