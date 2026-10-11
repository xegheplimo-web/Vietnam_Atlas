#!/usr/bin/env python3
"""Báo cáo độ phủ POI theo polygon hành chính 34 tỉnh/thành (nguồn GSO).

Đọc data/atlas.sqlite3 (CHỈ ĐỌC, mode=ro) + server/geo.py: mỗi POI được gán tỉnh bằng
point-in-polygon trên tọa độ (lat, lon) THẬT — không suy diễn tâm điểm/bbox (khác
coverage cũ theo lưới 1° của sync_osm_into_atlas.py).

Output: bảng text (mỗi tỉnh: count + % tổng; dòng cuối outside_polygon) hoặc JSON
(--json-out). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import geo  # noqa: E402

PROVENANCE_FILE = ROOT / "data" / "provinces" / "PROVENANCE.json"


def load_provenance() -> dict:
    try:
        return json.loads(PROVENANCE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "atlas.sqlite3"), help="đường dẫn SQLite atlas (chỉ đọc)")
    parser.add_argument("--json-out", default=None, help="ghi kết quả ra file JSON (tùy chọn)")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"error: không tìm thấy DB: {db}", file=sys.stderr)
        return 2

    provenance = load_provenance()
    provinces = geo.all_provinces()
    counts = {p.code: 0 for p in provinces}
    outside = 0
    total_rows = 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for lat, lon in con.execute("SELECT lat, lon FROM places"):
            total_rows += 1
            province = geo.province_at(lat, lon) if lat is not None and lon is not None else None
            if province is None:
                outside += 1
            else:
                counts[province.code] += 1
    finally:
        con.close()
    total = sum(counts.values()) + outside

    print("# Độ phủ POI theo polygon hành chính 34 tỉnh/thành")
    print(
        f"# Nguồn polygon: {provenance.get('source_repo', '?')} (license {provenance.get('license', '?')}), "
        f"dataset {provenance.get('dataset_version', '?')}, {provenance.get('decree', '?')}"
    )
    print("# PIP trên tọa độ POI thật (point-in-polygon) — không suy diễn tâm điểm/bbox.")
    print(f"# DB: {db} ({total_rows:,} POI)")
    print()
    print(f"{'code':<6}{'tỉnh':<18}{'count':>10}{'%':>8}")
    for province in sorted(provinces, key=lambda p: (-counts[p.code], p.code)):
        pct = round(counts[province.code] / total * 100, 1) if total else 0.0
        print(f"{province.code:<6}{province.name:<18}{counts[province.code]:>10,}{pct:>8}")
    pct_outside = round(outside / total * 100, 1) if total else 0.0
    print(f"{'--':<6}{'outside_polygon':<18}{outside:>10,}{pct_outside:>8}")
    print(f"{'':<6}{'TỔNG':<18}{total:>10,}{100.0:>8}")

    if args.json_out:
        payload = {
            "total_pois": total,
            "polygon_source": {
                "repo": provenance.get("source_repo"),
                "license": provenance.get("license"),
                "dataset_version": provenance.get("dataset_version"),
                "decree": provenance.get("decree"),
                "generated_at": provenance.get("generated_at"),
            },
            "by_province": [
                {
                    "code": p.code,
                    "name": p.name,
                    "count": counts[p.code],
                    "pct": round(counts[p.code] / total * 100, 1) if total else 0.0,
                }
                for p in sorted(provinces, key=lambda p: (-counts[p.code], p.code))
            ],
            "outside_polygon": {"count": outside, "pct": pct_outside},
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n# JSON: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
