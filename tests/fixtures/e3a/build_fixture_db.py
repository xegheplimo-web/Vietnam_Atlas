#!/usr/bin/env python3
"""Build the E3a fixture databases used by tests/test_benchmark_e3a.py.

Synthetic data only — a few dozen rows, NEVER a copy of the real corpus
(card C2: fixture DBs are created via server.store.init_db() so they get the real
schema + FTS5 + province_code, then save_place() assigns geo codes itself).

    python tests/fixtures/e3a/build_fixture_db.py --out <dir>

Writes <dir>/atlas_fixture.sqlite3 and <dir>/gazetteer_fixture.sqlite3.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

FIXTURE_DIR = Path(__file__).resolve().parent


def build_atlas(db_path: Path) -> Path:
    from server.store import init_db, save_place, session

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    init_db(db_path)
    places = json.loads((FIXTURE_DIR / "places.json").read_text(encoding="utf-8"))
    with session(db_path) as con:
        for place in places:
            save_place(con, place)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return db_path


def build_gazetteer(db_path: Path) -> Path:
    """Minimal gazetteer: 5 current provinces + 1 expired + 1 ward + aliases + FTS."""
    import import_gazetteer
    from build_admin_fts import build_admin_fts

    name_norm = import_gazetteer.name_norm
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(import_gazetteer.SCHEMA)
        con.execute(
            "INSERT INTO gazetteer_source(publisher, source_url, document_ref, effective_as_of, retrieved_at, sha256, license_id, rights_basis, redistribution_status)"
            " VALUES('Tổng cục Thống kê (fixture)', 'https://fixture.invalid/nso', 'fixture', '2025-07-01', '2026-10-19', '0' * 64, 'CC0', 'synthetic fixture', 'permitted')"
        )
        provinces = [
            ("01", "Thành phố Hà Nội", "Thành phố Trung ương"),
            ("31", "Thành phố Hải Phòng", "Thành phố Trung ương"),
            ("48", "Thành phố Đà Nẵng", "Thành phố Trung ương"),
            ("79", "Thành phố Hồ Chí Minh", "Thành phố Trung ương"),
            ("92", "Thành phố Cần Thơ", "Thành phố Trung ương"),
        ]
        for code, name, unit_type in provinces:
            con.execute(
                "INSERT INTO admin_unit(code, name, name_norm, level, unit_type, valid_from, source_id) VALUES(?,?,?,?,?,?,1)",
                (code, name, name_norm(name), "province", unit_type, "2025-07-01"),
            )
        # Expired pre-merger unit merged into HCM — exercises the G4 chain lookup.
        con.execute(
            "INSERT INTO admin_unit(code, name, name_norm, level, unit_type, valid_from, valid_to, source_id) VALUES(?,?,?,?,?,?,?,1)",
            ("74", "Tỉnh Bình Dương", name_norm("Tỉnh Bình Dương"), "province", "Tỉnh", "2020-01-01", "2025-06-30"),
        )
        bd_id = con.execute("SELECT id FROM admin_unit WHERE code='74'").fetchone()[0]
        hcm_id = con.execute("SELECT id FROM admin_unit WHERE code='79'").fetchone()[0]
        con.execute(
            "INSERT INTO admin_relation(from_unit_id, to_unit_id, relation_type, effective_date, source_id) VALUES(?,?,'merged_into','2025-07-01',1)",
            (bd_id, hcm_id),
        )
        con.execute(
            "INSERT INTO admin_unit(code, name, name_norm, level, unit_type, parent_id, valid_from, source_id) VALUES(?,?,?,?,?,?,?,1)",
            ("77001", "Phường Bến Thành", name_norm("Phường Bến Thành"), "ward", "Phường", hcm_id, "2025-07-01"),
        )
        for alias, target in (("Sài Gòn", hcm_id), ("TPHCM", hcm_id)):
            con.execute(
                "INSERT INTO admin_alias(unit_id, alias, alias_norm, alias_type, source_id) VALUES(?,?,?,'historical',1)",
                (target, alias, name_norm(alias)),
            )
        con.commit()
    finally:
        con.close()
    build_admin_fts(db_path)
    return db_path


def main() -> int:
    p = argparse.ArgumentParser(description="Build E3a fixture DBs (synthetic, not a corpus copy)")
    p.add_argument("--out", type=Path, default=FIXTURE_DIR)
    a = p.parse_args()
    atlas = build_atlas(a.out / "atlas_fixture.sqlite3")
    gaz = build_gazetteer(a.out / "gazetteer_fixture.sqlite3")
    print(f"atlas fixture: {atlas}")
    print(f"gazetteer fixture: {gaz}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
