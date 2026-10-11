"""Backfill places.province_code from point-in-polygon on (lat, lon).

Assigns the GSO province code of the polygon containing each POI
(data/provinces/provinces_34.geojson via server.geo.province_at). POIs outside
all 34 polygons get NULL — they are NOT removed; geo-aware search falls back to
text matching for them.

Dry-run by default (reports assignable/outside counts). --apply creates a
SQLite online backup in backups/ first, then writes — same pattern as
tools/migrate_old_provinces.py.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import geo  # noqa: E402 (import sau sys.path.insert là chủ ý của CLI)
from server.store import init_db, session  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Backfill places.province_code via point-in-polygon (GSO 34 provinces)")
    ap.add_argument("--db", default=str(ROOT / "data" / "atlas.sqlite3"))
    ap.add_argument("--apply", action="store_true", help="execute backfill after automatic backup")
    ap.add_argument("--only-null", action="store_true", help="chỉ tính cho POI chưa có province_code (mặc định tính lại toàn bộ)")
    args = ap.parse_args()
    if not Path(args.db).exists():
        raise SystemExit("SQLite database not found: " + args.db)
    init_db(args.db)  # ensures the province_code column exists (idempotent migration)
    with session(args.db) as con:
        select = "SELECT id, lat, lon, province_code FROM places" + (" WHERE province_code IS NULL" if args.only_null else "")
        rows = con.execute(select).fetchall()
    plan = []
    outside = []
    unchanged = 0
    for row in rows:
        province = geo.province_at(row["lat"], row["lon"])
        code = province.code if province is not None else None
        if code is None:
            outside.append(row["id"])
        if code != row["province_code"]:
            plan.append((code, row["id"]))
        else:
            unchanged += 1
    assigned = sum(1 for code, _ in plan if code is not None)
    print(f"Total rows scanned: {len(rows)}")
    print(f"- already correct : {unchanged}")
    print(f"- to assign       : {assigned}")
    print(
        f"- outside polygons (province_code → NULL): {len(outside)}"
        + ("" if args.only_null else f" ({sum(1 for code, _ in plan if code is None)} updates to NULL)")
    )
    if outside:
        print("Sample outside-polygon ids:", ", ".join(outside[:10]))
    if not args.apply:
        print("Dry-run only. Pass --apply to back up DB and write province_code.")
        return
    if not plan:
        print("Nothing to update.")
        return
    source = Path(args.db)
    directory = ROOT / "backups"
    directory.mkdir(exist_ok=True)
    dest = directory / ("before-province-code-backfill-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
    with sqlite3.connect(source) as original, sqlite3.connect(dest) as copied:  # NOSONAR(S8706)
        original.backup(copied)
    print(f"Backup completed: {dest}")
    with session(args.db) as con:
        con.executemany("UPDATE places SET province_code = ? WHERE id = ?", plan)
    print(f"Updated {len(plan)} rows ({assigned} assigned, {len(plan) - assigned} set NULL).")


if __name__ == "__main__":
    main()
