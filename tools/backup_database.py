"""Safe SQLite online backup with timestamp; optional restore is manual.

--all backs up BOTH data/atlas.sqlite3 (atlas-<ts>.sqlite3) and the gazetteer
(gaz-<ts>.sqlite3) — the gazetteer is required by /api/admin/resolve and
/api/places?admin=1, so an atlas-only backup set cannot restore a full service.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def backup_one(source: Path, dest: Path) -> bool:
    """Online-backup `source` to `dest` and verify PRAGMA integrity_check.

    The source is opened read-only (`?mode=ro`) — a backup must never write to
    the live database, not even via an accidental schema/journal touch.
    """
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src, sqlite3.connect(str(dest)) as dst:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if ok:
        print(f"Backup OK: {dest} ({dest.stat().st_size:,} bytes)")
    else:
        print(f"Cảnh báo: bản sao lưu lỗi integrity_check ({dest})", file=sys.stderr)
    return ok


def main():
    parser = argparse.ArgumentParser(description="Back up Vietnam Atlas SQLite database safely")
    parser.add_argument("--db", default=str(ROOT / "data" / "atlas.sqlite3"))
    parser.add_argument("--gazetteer", default=str(ROOT / "data" / "gazetteer.sqlite3"))
    parser.add_argument("--out", default=str(ROOT / "backups"))
    parser.add_argument("--all", action="store_true", help="backup cả atlas lẫn gazetteer")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    jobs = [(Path(args.db), out / ("atlas-" + stamp + ".sqlite3"))]
    if args.all:
        jobs.append((Path(args.gazetteer), out / ("gaz-" + stamp + ".sqlite3")))
    failed = []
    for source, dest in jobs:
        if not source.is_file():
            print(f"File DB chưa tồn tại: {source}", file=sys.stderr)
            failed.append(source)
            continue
        if not backup_one(source, dest):
            failed.append(source)
    if failed:
        raise SystemExit(f"Backup lỗi: {len(failed)}/{len(jobs)} bản")


if __name__ == "__main__":
    main()
