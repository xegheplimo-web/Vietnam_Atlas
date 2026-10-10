"""Safe SQLite online backup with timestamp; optional restore is manual."""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Back up Vietnam Atlas SQLite database safely")
    parser.add_argument("--db", default=str(ROOT / "data" / "atlas.sqlite3"))
    parser.add_argument("--out", default=str(ROOT / "backups"))
    args = parser.parse_args()
    source = Path(args.db)
    out = Path(args.out)
    if not source.is_file():
        raise SystemExit(f"File DB chưa tồn tại: {source}")
    out.mkdir(parents=True, exist_ok=True)
    filename = out / ("atlas-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(filename)) as dst:
        src.backup(dst)
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit("Cảnh báo: bản sao lưu lỗi integrity_check")
    print(f"Backup OK: {filename} ({filename.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
