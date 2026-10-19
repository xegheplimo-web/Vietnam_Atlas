"""Offline tests for the C3a pilot builder (tools/pilot_build.py).

Synthetic corpus only — a few dozen rows built via server.store.init_db +
save_place, plus a hand-rolled gazetteer (import_gazetteer.SCHEMA). The real
data/atlas.sqlite3 + data/gazetteer.sqlite3 are NEVER touched. Quotas are shrunk
via the internal `quotas` parameter so the test corpus stays small; the CLI
always uses the frozen card allocation.
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.store import init_db, save_place, session  # noqa: E402

_spec = importlib.util.spec_from_file_location("pilot_build", ROOT / "tools" / "pilot_build.py")
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)

_gaz_spec = importlib.util.spec_from_file_location("import_gazetteer", ROOT / "tools" / "import_gazetteer.py")
import_gazetteer = importlib.util.module_from_spec(_gaz_spec)
_gaz_spec.loader.exec_module(import_gazetteer)

NOW = "2026-10-19T00:00:00+07:00"
TINY_QUOTAS = {"G1": (4, 0), "G2": (2, 1), "G3": (2, 1), "G4": (1, 1), "G5": (1, 1), "G6": (2, 1), "G7": (1, 1)}


def _gaz_unit(con, code, name, level, unit_type, parent_id=None, valid_to=None):
    con.execute(
        "INSERT INTO admin_unit(code, name, name_norm, level, unit_type, parent_id, valid_from, valid_to, source_id) VALUES(?,?,?,?,?,?,?,?,1)",
        (code, name, import_gazetteer.name_norm(name), level, unit_type, parent_id, "2020-01-01", valid_to),
    )
    return con.execute("SELECT id FROM admin_unit WHERE code=? AND level=? AND name=?", (code, level, name)).fetchone()[0]


def build_gazetteer(path: Path) -> Path:
    """3 provinces, 2 current wards, 1 expired Quận district, 1 ward alias."""
    con = sqlite3.connect(str(path))
    try:
        con.executescript(import_gazetteer.SCHEMA)
        con.execute(
            "INSERT INTO gazetteer_source(publisher, source_url, document_ref, effective_as_of, retrieved_at, sha256, license_id, rights_basis, redistribution_status)"
            " VALUES('fixture', 'https://fixture.invalid/nso', 'fixture', '2025-07-01', '2026-10-19', '0' * 64, 'CC0', 'synthetic', 'permitted')"
        )
        hn = _gaz_unit(con, "01", "Thành phố Hà Nội", "province", "Thành phố Trung ương")
        dn = _gaz_unit(con, "48", "Thành phố Đà Nẵng", "province", "Thành phố Trung ương")
        hcm = _gaz_unit(con, "79", "Thành phố Hồ Chí Minh", "province", "Thành phố Trung ương")
        _gaz_unit(con, "001", "Quận Ba Đình", "district", "Quận", parent_id=hn, valid_to="2025-06-30")
        _gaz_unit(con, "01001", "Phường Trúc Bạch", "ward", "Phường", parent_id=hn)
        _gaz_unit(con, "79001", "Phường Bến Thành", "ward", "Phường", parent_id=hcm)
        old = _gaz_unit(con, "48001", "Xã Mới Đoài", "ward", "Xã", parent_id=dn)
        con.execute(
            "INSERT INTO admin_alias(unit_id, alias, alias_norm, alias_type, source_id) VALUES(?,?,?,'old_name',1)",
            (old, "Xã Cũ Đoài", import_gazetteer.name_norm("Xã Cũ Đoài")),
        )
        con.commit()
    finally:
        con.close()
    return path


def _place(con, pid, name, category, address, lat, lon, pcode, source_id):
    place, _action = save_place(
        con,
        {
            "name": name,
            "category": category,
            "address": address,
            "lat": lat,
            "lon": lon,
            "source": "OpenStreetMap",
            "source_id": source_id,
            "status": "verified",
        },
        place_id=pid,
    )
    con.execute("UPDATE places SET province_code=? WHERE id=?", (pcode, pid))
    return place


def build_atlas(path: Path) -> Path:
    init_db(path)
    with session(path) as con:
        # G1/G2 pool: unique names >= 12 chars, no digits, spread over 3 provinces.
        _place(con, "g1-1", "Nhà Hàng Sen Việt", "amenity:restaurant", "1 Phố Huế, Phường Trúc Bạch, Hà Nội", 21.046, 105.836, "01", "node/90001")
        _place(
            con, "g1-2", "Cửa Hàng Sách Mới Việt", "shop:convenience", "2 Phố Huế, Phường Trúc Bạch, Hà Nội", 21.0461, 105.8362, "01", "node/90002"
        )
        _place(con, "g1-3", "Bệnh Viện Đa Khoa Xanh Lá", "amenity:hospital", "9 Bạch Đằng, Đà Nẵng", 16.07, 108.22, "48", "node/90003")
        _place(
            con,
            "g1-4",
            "Trường Học Tư Thục Ánh Sao",
            "amenity:school",
            "5 Lê Lợi, Phường Bến Thành, Thành phố Hồ Chí Minh",
            10.772,
            106.698,
            "79",
            "node/90004",
        )
        _place(
            con,
            "g1-5",
            "Khách Sạn Bông Hồng Trắng",
            "tourism:hotel",
            "7 Lê Lợi, Phường Bến Thành, Thành phố Hồ Chí Minh",
            10.7721,
            106.6981,
            "79",
            "node/90005",
        )
        _place(con, "g1-6", "Quán Cà Phê Sân Vườn Xanh", "amenity:cafe", "3 Hải Châu, Đà Nẵng", 16.065, 108.221, "48", "node/90006")
        # G3/G4 pool: same-category POIs clustered in admin units.
        for i in range(4):
            _place(
                con,
                f"r79-{i}",
                f"Quán Ăn Bến Thành {i + 1}",
                "amenity:restaurant",
                f"{10 + i} Lê Lợi, Phường Bến Thành, Thành phố Hồ Chí Minh",
                10.773 + i * 0.001,
                106.699,
                "79",
                f"node/9100{i}",
            )
        for i in range(4):
            _place(
                con,
                f"c01-{i}",
                f"Cà Phê Ba Đình {i + 1}",
                "amenity:cafe",
                f"{20 + i} Đội Cấn, Quận Ba Đình, Hà Nội",
                21.04 + i * 0.001,
                105.83,
                "01",
                f"node/9200{i}",
            )
        for i in range(3):
            _place(
                con,
                f"s79-{i}",
                f"Trường Tiểu Học Bến Thành {i + 1}",
                "amenity:school",
                f"{30 + i} Trần Hưng Đạo, Phường Bến Thành, Thành phố Hồ Chí Minh",
                10.774 + i * 0.001,
                106.697,
                "79",
                f"node/9300{i}",
            )
        for i in range(3):
            _place(
                con,
                f"p48-{i}",
                f"Nhà Thuốc An Tâm {i + 1}",
                "amenity:pharmacy",
                f"{40 + i} Nguyễn Văn Linh, Đà Nẵng",
                16.06 + i * 0.001,
                108.21,
                "48",
                f"node/9400{i}",
            )
        for i in range(3):
            _place(
                con,
                f"h48-{i}",
                f"Quán Ăn Cũ Đoài {i + 1}",
                "amenity:restaurant",
                f"{50 + i} QL1, Xã Cũ Đoài, Đà Nẵng",
                16.08 + i * 0.001,
                108.19,
                "48",
                f"node/9500{i}",
            )
        # G5: two landmarks with same-category POIs within 3 km.
        _place(con, "lm-1", "Chùa Một Cột Xưa", "amenity:place_of_worship", "Phường Trúc Bạch, Hà Nội", 21.035, 105.833, "01", "node/96001")
        for i in range(3):
            _place(
                con,
                f"gc1-{i}",
                f"Cà Phê Gần Chùa {i + 1}",
                "amenity:cafe",
                f"{60 + i} Hùng Vương, Hà Nội",
                21.0355 + i * 0.0008,
                105.8335,
                "01",
                f"node/9601{i}",
            )
        _place(con, "lm-2", "Tượng Cá Chép Hóa Rồng", "tourism:attraction", "Đà Nẵng", 16.061, 108.227, "48", "node/96002")
        for i in range(4):
            _place(
                con,
                f"gc2-{i}",
                f"Nhà Hàng Gần Tượng {i + 1}",
                "amenity:restaurant",
                f"{70 + i} Võ Văn Kiệt, Đà Nẵng",
                16.0615 + i * 0.001,
                108.2275,
                "48",
                f"node/9602{i}",
            )
        # G7: same name in >= 3 provinces. Names stay < 12 chars so G1's
        # >=12-char filter can't eat them first (tiny corpus shares the pool).
        for i, pc in enumerate(("01", "48", "79")):
            _place(con, f"b7-{i}", "Bánh Mì Hoa", "amenity:restaurant", f"{80 + i} Đường Chung, Tỉnh X{i}", 20.0 + i, 105.0 + i, pc, f"node/9700{i}")
        for i, pc in enumerate(("01", "48", "79")):
            _place(con, f"c7-{i}", "Sửa Xe Sao", "shop:hairdresser", f"{90 + i} Đường Riêng, Tỉnh Y{i}", 19.0 + i, 104.0 + i, pc, f"node/9701{i}")
    con = sqlite3.connect(str(path))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return path


class PilotBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        cls.atlas = build_atlas(base / "atlas.sqlite3")
        cls.gaz = build_gazetteer(base / "gazetteer.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def build(self, outdir, seed="777"):
        return pb.build_pilot(self.atlas, self.gaz, seed, outdir, now=NOW, quotas=TINY_QUOTAS)

    def test_build_counts_and_ids(self):
        out = Path(self.temp.name) / "b1"
        manifest = self.build(out)
        queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))["queries"]
        expected = {"G1": 4, "G2": 3, "G3": 3, "G4": 2, "G5": 2, "G6": 3, "G7": 2}
        self.assertEqual(len(queries), 19)
        counts = {}
        for q in queries:
            counts[q["group"]] = counts.get(q["group"], 0) + 1
            self.assertEqual(q["split"], "dev")
            self.assertFalse(q["regression150"])
            self.assertIn(q["panel"], ("pilot_random_85", "pilot_hard_15"))
        self.assertEqual(counts, expected)
        ids = [q["id"] for q in queries]
        self.assertEqual(ids, [f"q{i:03d}" for i in range(1, 20)])
        # Group order: ids run G1 -> G7 contiguously.
        groups_in_order = [q["group"] for q in queries]
        self.assertEqual(groups_in_order, sorted(groups_in_order))
        n_random = sum(1 for q in queries if q["panel"] == "pilot_random_85")
        n_hard = sum(1 for q in queries if q["panel"] == "pilot_hard_15")
        self.assertEqual((n_random, n_hard), (13, 6))
        self.assertEqual(manifest["counts_by_group"], expected)

    def test_deterministic_bytes(self):
        a = Path(self.temp.name) / "d1"
        b = Path(self.temp.name) / "d2"
        self.build(a)
        self.build(b)
        for name in ("queries.json", "evidence.jsonl"):
            self.assertEqual((a / name).read_bytes(), (b / name).read_bytes())

    def test_g1_query_is_exact_name_and_gold_is_poi(self):
        out = Path(self.temp.name) / "g1"
        self.build(out)
        queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))["queries"]
        g1 = [q for q in queries if q["group"] == "G1"]
        con = sqlite3.connect(self.atlas.resolve().as_uri() + "?mode=ro", uri=True)
        names = {r[0] for r in con.execute("SELECT name FROM places")}
        con.close()
        for q in g1:
            self.assertIn(q["query"], names)
        # G2 queries must be corrupted variants, never an exact stored name.
        g2 = [q for q in queries if q["group"] == "G2"]
        for q in g2:
            self.assertNotIn(q["query"], names)
            self.assertTrue(q["query"].strip())

    def test_evidence_validates_against_harness(self):
        out = Path(self.temp.name) / "e1"
        self.build(out)
        import tools.benchmark_e3a as be3a

        queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))["queries"]
        records = [
            (i, json.loads(line)) for i, line in enumerate((out / "evidence.jsonl").read_text(encoding="utf-8").splitlines(), 1) if line.strip()
        ]
        errors = be3a.validate_query_set(queries) + be3a.validate_evidence(records, queries)
        self.assertEqual(errors, [])
        # Every non-G6 query has at least one evidence record.
        with_ev = {r["query_id"] for _i, r in records}
        for q in queries:
            if q["group"] == "G6":
                continue
            self.assertIn(q["id"], with_ev)
        for q in queries:
            if q["group"] == "G5":
                self.assertIn("near", q)
                self.assertEqual(q["near"]["crs"], "EPSG:4326")

    def test_g6_zero_name_norm_on_corpus(self):
        out = Path(self.temp.name) / "g6"
        self.build(out)
        queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))["queries"]
        con = sqlite3.connect(self.atlas.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for q in queries:
                if q["group"] != "G6":
                    continue
                n = con.execute("SELECT COUNT(*) FROM places WHERE name_norm = ?", (pb._normalize_query(q["query"]),)).fetchone()[0]
                self.assertEqual(n, 0, q["query"])
        finally:
            con.close()

    def test_master_sha_unchanged(self):
        before = pb._sha256_file(self.atlas)
        gaz_before = pb._sha256_file(self.gaz)
        self.build(Path(self.temp.name) / "sha")
        self.assertEqual(before, pb._sha256_file(self.atlas))
        self.assertEqual(gaz_before, pb._sha256_file(self.gaz))

    def test_worksheet_top10_from_run(self):
        out = Path(self.temp.name) / "w"
        manifest = self.build(out)
        del manifest
        queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))["queries"]
        run = {
            "schema": "e3a-run/v1",
            "adapters": [
                {
                    "adapter": "legacy-http",
                    "results": [
                        {
                            "query_id": queries[0]["id"],
                            "top_ids": ["p1", "p2"],
                            "top_rows": [
                                {"id": "p1", "name": "One", "category": "amenity:cafe", "distance_km": 0.4},
                                {"id": "p2", "name": "Two", "category": "amenity:cafe"},
                            ],
                            "latencies_ms": [1.0],
                            "ids_by_repeat": [["p1", "p2"]],
                            "error": None,
                        }
                    ],
                    "latency_ms_p50": 1.0,
                    "latency_ms_p95": 1.0,
                }
            ],
        }
        run_path = Path(self.temp.name) / "run.json"
        run_path.write_text(json.dumps(run), encoding="utf-8")
        sheet = Path(self.temp.name) / "worksheet.jsonl"
        rc = pb.main(["worksheet", "--run", str(run_path), "--queries", str(out / "queries.json"), "--out", str(sheet)])
        self.assertEqual(rc, 0)
        lines = [json.loads(x) for x in sheet.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 19)
        first = lines[0]
        self.assertEqual(first["query_id"], queries[0]["id"])
        self.assertEqual(first["query"], queries[0]["query"])
        self.assertEqual(first["group"], queries[0]["group"])
        self.assertEqual(first["persona"], queries[0]["persona"])
        self.assertEqual(len(first["candidates"]), 2)
        self.assertEqual(first["candidates"][0]["rank"], 1)
        self.assertEqual(first["candidates"][0]["place_id"], "p1")
        self.assertEqual(first["candidates"][0]["distance_km"], 0.4)
        self.assertNotIn("distance_km", first["candidates"][1])
        # Queries absent from the run still get a row with empty candidates.
        self.assertEqual(lines[5]["candidates"], [])

    def test_cli_build_end_to_end(self):
        out = Path(self.temp.name) / "cli"
        from unittest.mock import patch

        with patch.object(pb, "QUOTAS", TINY_QUOTAS):  # corpus fixture is small; CLI path giữ quota đóng băng trong thật
            rc = pb.main(
                [
                    "build",
                    "--db",
                    str(self.atlas),
                    "--gazetteer-db",
                    str(self.gaz),
                    "--seed",
                    "777",
                    "--out",
                    str(out),
                    "--now",
                    NOW,
                ]
            )
        self.assertEqual(rc, 0)
        for name in ("queries.json", "evidence.jsonl", "pilot_manifest.json"):
            self.assertTrue((out / name).is_file(), name)
        manifest = json.loads((out / "pilot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["seed"], 777)
        self.assertEqual(manifest["atlas_sha256"], pb._sha256_file(self.atlas))
        self.assertEqual(manifest["gazetteer_sha256"], pb._sha256_file(self.gaz))

    def test_g2_nodia_skips_names_without_diacritics(self):
        """Tên vốn không dấu ⇒ biến thể 'nodia' sẽ trùng câu gốc, phải bị từ chối."""
        self.assertIsNone(pb._g2_nodiac("Mechanic Huy Hoang"))
        self.assertIsNone(pb._g2_nodiac("ABC Hotel"))
        self.assertEqual(pb._g2_nodiac("Bệnh Viện Bạch Mai"), "benh vien bach mai")

    def test_queries_are_case_insensitively_unique(self):
        out = Path(self.temp.name) / "unique"
        self.build(out)
        queries = json.loads((out / "queries.json").read_text(encoding="utf-8"))["queries"]
        folded = [q["query"].casefold() for q in queries]
        self.assertEqual(len(folded), len(set(folded)), "câu trùng nhau (không phân biệt hoa/thường)")


if __name__ == "__main__":
    unittest.main()
