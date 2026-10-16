#!/usr/bin/env python3
"""Rebuild places.address from OSM addr:* tags with a correct display format.

The legacy builder joined housenumber/street/ward/city with spaces, producing
glued strings like ``34 Hàng Giấy Hà Nội``. This tool recomposes every
OpenStreetMap-sourced row in the atlas database from the canonical tags kept in
``data/osm_vietnam.sqlite3`` (``tools.vn_osm_dataset.compose_address``) and
refreshes ``search_text`` in the same write so FTS mirrors the new value.

Default (no ``--apply``): dry-run report only, no writes.
``--apply``: takes an online backup via ``tools/backup_database.py`` first,
writes all updates in a single transaction, leaves a JSON manifest under
``reports/`` and re-checks that nomains rows still differ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.store import build_search_text, utcnow  # noqa: E402 (import sau sys.path.insert là chủ ý của CLI)
from tools.vn_osm_dataset import compose_address  # noqa: E402 (như trên)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_osm_tags(osm_db: Path) -> dict[str, str]:
    """Map 'osm_type/osm_id' -> tags_json for active rows of the OSM dataset."""
    result: dict[str, str] = {}
    with sqlite3.connect(str(osm_db)) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute("SELECT osm_type, osm_id, tags_json FROM osm_places WHERE tags_json IS NOT NULL AND is_active=1"):
            result[f"{row['osm_type']}/{row['osm_id']}"] = row["tags_json"]
    return result


def plan_changes(atlas_db: Path, osm_map: dict[str, str]) -> tuple[list[tuple[str, str, str]], int, int, int]:
    """Compute (changes, scanned, missing_tags, unchanged) from the atlas DB.

    changes = [(place_id, old_address, new_address), ...] for rows whose
    composed display address differs from the stored one.
    """
    changes: list[tuple[str, str, str]] = []
    scanned = missing = unchanged = 0
    with sqlite3.connect(str(atlas_db)) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(
            "SELECT id, source_id, name, category, address, province, phone, notes, search_text FROM places WHERE source='OpenStreetMap'"
        ):
            scanned += 1
            raw = osm_map.get(row["source_id"])
            if raw is None:
                missing += 1
                continue
            new_addr = compose_address(json.loads(raw))
            want_text = build_search_text(
                {**{k: row[k] for k in ("name", "category", "province", "phone", "notes")}, "address": new_addr}
            )
            if new_addr == (row["address"] or "") and want_text == (row["search_text"] or ""):
                unchanged += 1
                continue
            changes.append((row["id"], row["address"] or "", new_addr))
    return changes, scanned, missing, unchanged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(ROOT / "data" / "atlas.sqlite3"))
    parser.add_argument("--osm", default=str(ROOT / "data" / "osm_vietnam.sqlite3"))
    parser.add_argument("--backup-dir", default=str(ROOT / "backups"))
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--sample", type=int, default=5, help="số dòng ví dụ in ra khi dry-run")
    parser.add_argument("--apply", action="store_true", help="thực sự ghi các thay đổi (backup trước khi ghi)")
    args = parser.parse_args()

    atlas_db = Path(args.db)
    osm_db = Path(args.osm)
    if not atlas_db.is_file():
        raise SystemExit(f"File DB chưa tồn tại: {atlas_db}")
    if not osm_db.is_file():
        raise SystemExit(f"File DB chưa tồn tại: {osm_db}")

    osm_map = load_osm_tags(osm_db)
    changes, scanned, missing, unchanged = plan_changes(atlas_db, osm_map)
    print(f"scanned={scanned} changed={len(changes)} unchanged={unchanged} missing_tags={missing}")

    for place_id, old, new in changes[: max(args.sample, 0)]:
        print(f"  {place_id}: {old!r} -> {new!r}")

    if not args.apply:
        print("DRY-RUN: chưa ghi gì. Chạy lại với --apply để cập nhật.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_before = sha256_file(atlas_db)
    backup_path = None
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "backup_database.py"), "--db", str(atlas_db), "--out", str(Path(args.backup_dir))],
        check=True,
    )
    backups = sorted(Path(args.backup_dir).glob("atlas-*.sqlite3"))
    if not backups:
        raise SystemExit("Không thấy file backup sau khi chạy backup_database.py")
    backup_path = backups[-1]

    changed = 0
    with sqlite3.connect(str(atlas_db)) as con:
        con.row_factory = sqlite3.Row
        now = utcnow()
        for place_id, _old, new in changes:
            row = con.execute("SELECT name, category, province, phone, notes FROM places WHERE id=?", (place_id,)).fetchone()
            search_text = build_search_text(
                {**{k: row[k] for k in ("name", "category", "province", "phone", "notes")}, "address": new}
            )
            con.execute(
                "UPDATE places SET address=?, search_text=?, updated_at=? WHERE id=?",
                (new, search_text, now, place_id),
            )
            changed += 1
        # một transaction duy nhất, commit khi exit() không lỗi

    leftover, _, _, _ = plan_changes(atlas_db, osm_map)

    manifest = {
        "tool": "tools/recompose_addresses.py",
        "requested_at": stamp,
        "db": str(atlas_db),
        "db_sha256_before": backup_before,
        "backup_path": str(backup_path),
        "backup_sha256": sha256_file(backup_path) if backup_path else None,
        "scanned": scanned,
        "changed": changed,
        "unchanged_after_apply": len(leftover),
        "osm_source": str(osm_db),
        "apply": True,
    }
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / f"recompose-addresses-{stamp}.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"APPLY: {changed} dòng đã cập nhật. Manifest: {out}")
    if leftover:
        print(f"CẢNH BÁO: vẫn còn {len(leftover)} dòng khác biệt sau khi apply")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
