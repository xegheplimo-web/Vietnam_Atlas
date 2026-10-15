#!/usr/bin/env python3
"""Safe OSM dataset -> existing Vietnam_Atlas SQLite sync.

Defaults to dry-run, only inserts new OpenStreetMap source IDs, keeps verified
and user-created records untouched; makes an online backup before any write.
Lifecycle writes are gated behind --apply --apply-lifecycle: retired staging rows
(is_active=0 from a full --mark-missing import) mark the matching unverified OSM
atlas rows inactive, and atlas rows whose POI reappears active in staging are
restored to unverified; verified/user records stay protected either way.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POSITION_JUMP_MIN_KM = 50.0  # bất thường vị trí giữa 2 snapshot: chỉ báo cáo, không chặn
MAX_RESTORE_SAMPLES = 10
MAX_POSITION_JUMP_SAMPLES = 20


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km; same 12742 m diameter as the server-side _DISTANCE_SQL."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742.0 * math.asin(min(1.0, math.sqrt(a)))


RETIRED_SYNC_STATE_DEFAULT = ROOT / "reports" / "_state" / "retired_by_sync.json"


def load_retired_sids(state_path: Path) -> dict:
    """source_id -> retired_at for rows retired by this tool; fail-closed (any problem = empty)."""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))  # NOSONAR(S8707): đường dẫn từ cờ CLI cục bộ do người vận hành chạy
        if isinstance(data, dict) and isinstance(data.get("sids"), dict):
            return {str(k): v for k, v in data["sids"].items() if isinstance(v, str)}
    except (OSError, ValueError):
        pass
    return {}


def save_retired_sids(state_path: Path, sids: dict) -> None:
    """Atomic write of the sync-retirement ledger (tmp + rename)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    payload = {"schema": 1, "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sids": sids}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)  # NOSONAR(S8707): đường dẫn từ cờ CLI cục bộ do người vận hành chạy


def coverage_report(src):
    """Coverage summary of the staging dataset: type mix, top categories, 1-degree grid cells.

    province is intentionally never inferred from a point (no centroid-to-province guessing),
    so grid cells are the coarse coverage proxy until authoritative administrative polygons land.
    """
    by_type = {r[0]: r[1] for r in src.execute("SELECT osm_type, count(*) FROM osm_places WHERE is_active=1 GROUP BY osm_type")}
    categories = [
        {"category": r[0], "count": r[1]}
        for r in src.execute("SELECT category, count(*) n FROM osm_places WHERE is_active=1 GROUP BY category ORDER BY n DESC LIMIT 15")
    ]
    grid = [
        {"lat_cell": r[0], "lon_cell": r[1], "count": r[2]}
        for r in src.execute(
            "SELECT CAST(lat AS INT) la, CAST(lon AS INT) lo, count(*) n FROM osm_places WHERE is_active=1 GROUP BY la, lo ORDER BY n DESC LIMIT 30"
        )
    ]
    cells = src.execute("SELECT count(*) FROM (SELECT DISTINCT CAST(lat AS INT), CAST(lon AS INT) FROM osm_places WHERE is_active=1)").fetchone()[0]
    return {
        "note": "province chưa được suy từ tâm điểm; lưới 1° là proxy độ phủ thô cho tới khi có polygon hành chính chính thống.",
        "by_osm_type": by_type,
        "top_categories": categories,
        "grid_1deg_top_cells": grid,
        "cells_with_data": cells,
    }


def sync(
    osm_db: Path,
    atlas_db: Path,
    apply: bool = False,
    update_existing: bool = False,
    limit: int = 0,
    backups: Path | None = None,
    coverage: bool = False,
    apply_lifecycle: bool = False,
    retired_state: Path | None = None,
):
    if not osm_db.is_file():
        raise FileNotFoundError(f"Chưa có dữ liệu OSM: {osm_db}. Hãy tải PBF và nhập snapshot trước.")
    if limit < 0:
        raise ValueError("--limit không thể âm")
    retired_state = retired_state or RETIRED_SYNC_STATE_DEFAULT
    retired_sids = load_retired_sids(retired_state)
    retired_dirty = False
    from server.store import init_db, save_place  # only available inside full Vietnam_Atlas repository

    if apply and atlas_db.is_file():
        preview = sync(
            osm_db,
            atlas_db,
            apply=False,
            update_existing=update_existing,
            limit=limit,
            coverage=coverage,
            apply_lifecycle=apply_lifecycle,
            retired_state=retired_state,
        )
        pending_writes = preview["would_insert"] + preview["would_update"]
        if apply_lifecycle:
            pending_writes += preview["would_retire"] + preview["would_restore"]
        if pending_writes == 0:
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
        "would_retire": 0,
        "retired": 0,
        "retire_skipped_limit": False,
        "would_restore": 0,
        "restored": 0,
        "restore_samples": [],
        "restore_skipped_limit": False,
        "restore_blocked_manual": 0,
        "apply_lifecycle": bool(apply_lifecycle),
        "changed_in_source": 0,
        "position_jumps": [],
        "position_jumps_total": 0,
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
        if apply:
            # Acquire the write lock BEFORE preloading: the cached statuses must not race
            # with the server verifying/editing rows mid-sync, or the sync could overwrite
            # a freshly verified row or retire a just-verified place (stale cache).
            dest.execute("BEGIN IMMEDIATE")
        # Preload existing OSM rows once. The unique index on (source, source_id) is partial
        # (WHERE source_id <> ''), which the planner refuses for a bound lookup, so per-row
        # SELECTs degrade to a full scan each time: O(n*m) on a full national sync.
        existing = {}
        if dest is not None:
            for prev in dest.execute("SELECT * FROM places WHERE source='OpenStreetMap'"):
                existing[prev["source_id"]] = prev
        rows = src.execute(
            "SELECT * FROM osm_places WHERE is_active=1 ORDER BY osm_type,osm_id" + (" LIMIT ?" if limit else ""), ([limit] if limit else [])
        )
        # Reactivation evidence must come from a FULL import; a bounded pilot re-activation is not enough.
        full_snapshot_keys = {r[0] for r in src.execute("SELECT id FROM ingestion_snapshots WHERE mode LIKE '%:full:%'")}
        for row in rows:
            sid = f"{row['osm_type']}/{row['osm_id']}"
            previous = existing.get(sid)
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
            if previous["status"] not in ("unverified", "inactive"):
                # verified/closed/user-managed atlas rows are never touched by the sync.
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
            changed = any(previous[k] != v for k, v in fields.items())
            if changed:
                result["changed_in_source"] += 1
                km = haversine_km(previous["lat"], previous["lon"], row["lat"], row["lon"])
                if km > POSITION_JUMP_MIN_KM:
                    result["position_jumps_total"] += 1
                    if len(result["position_jumps"]) < MAX_POSITION_JUMP_SAMPLES:
                        result["position_jumps"].append(
                            {
                                "source_id": sid,
                                "name": row["name"],
                                "from": [previous["lat"], previous["lon"]],
                                "to": [row["lat"], row["lon"]],
                                "km": round(km, 1),
                            }
                        )
            if previous["status"] == "inactive":
                if limit:
                    # Lifecycle evaluation stays off on bounded pilots, same guard as retirement.
                    result["protected"] += 1
                    continue
                # E1: chỉ khôi phục dòng do CHÍNH sync đánh dấu inactive (sổ retired_by_sync).
                # Dòng người vận hành tự đặt inactive trong editor không bao giờ bị ghi đè.
                if sid not in retired_sids:
                    result["restore_blocked_manual"] += 1
                    continue
                # Dấu hiệu tái xuất hiện phải đến từ import FULL; pilot import không đủ tin cậy.
                if row["last_snapshot"] not in full_snapshot_keys:
                    result["protected"] += 1
                    continue
                # POI tái xuất hiện: ứng viên khôi phục; chỉ ghi với --apply-lifecycle.
                result["would_restore"] += 1
                if len(result["restore_samples"]) < MAX_RESTORE_SAMPLES:
                    result["restore_samples"].append({"source_id": sid, "name": row["name"]})
                if apply and apply_lifecycle:
                    payload = dict(previous)
                    payload.update(fields)
                    payload["status"] = "unverified"
                    save_place(dest, payload, place_id=previous["id"])
                    result["restored"] += 1
                    retired_sids.pop(sid, None)
                    retired_dirty = True
                continue
            if not update_existing:
                result["protected"] += 1
                continue
            if changed:
                result["would_update"] += 1
                if apply:
                    payload = dict(previous)
                    payload.update(fields)
                    save_place(dest, payload, place_id=previous["id"])
                    result["updated"] += 1
        if limit:
            # Retirement derives from a full --mark-missing snapshot; never evaluate it on a bounded pilot.
            result["retire_skipped_limit"] = True
            result["restore_skipped_limit"] = True
        else:
            for row in src.execute("SELECT osm_type,osm_id FROM osm_places WHERE is_active=0 ORDER BY osm_type,osm_id"):
                sid = f"{row['osm_type']}/{row['osm_id']}"
                previous = existing.get(sid)
                if previous is None:
                    continue
                if previous["status"] != "unverified":
                    result["protected"] += 1
                    continue
                result["would_retire"] += 1
                if apply and apply_lifecycle:
                    payload = dict(previous)
                    payload["status"] = "inactive"
                    save_place(dest, payload, place_id=previous["id"])
                    result["retired"] += 1
                    # Ghi sổ để sau này chỉ khôi phục đúng các dòng do sync retire.
                    retired_sids[sid] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    retired_dirty = True
        if coverage:
            result["coverage"] = coverage_report(src)
        if apply:
            dest.commit()
        if apply and retired_dirty and dest is not None:
            try:
                still_inactive = {r[0] for r in dest.execute("SELECT source_id FROM places WHERE source='OpenStreetMap' AND status='inactive'")}
                save_retired_sids(retired_state, {s: v for s, v in retired_sids.items() if s in still_inactive})
            except OSError as e:
                print(f"WARNING: không ghi được sổ retired_by_sync ({e}); các dòng đã retire sẽ không được khôi phục tự động.", file=sys.stderr)
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
    p.add_argument("--limit", type=int, default=0, help="pilot: sync first N staging rows; disables retirement evaluation")
    p.add_argument(
        "--apply-lifecycle", action="store_true", help="with --apply: write lifecycle changes (retire + restore); dry-run counters stay report-only"
    )
    p.add_argument(
        "--retired-state",
        type=Path,
        default=None,
        help="sổ retired_by_sync.json (mặc định reports/_state/); chỉ dòng trong sổ mới được sync khôi phục",
    )
    p.add_argument("--coverage-report", action="store_true", help="include staging coverage summary (type mix, top categories, 1° grid)")
    a = p.parse_args()
    try:
        print(
            json.dumps(
                sync(
                    a.osm_db,
                    a.atlas_db,
                    a.apply,
                    a.update_existing,
                    a.limit,
                    coverage=a.coverage_report,
                    apply_lifecycle=a.apply_lifecycle,
                    retired_state=a.retired_state,
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
