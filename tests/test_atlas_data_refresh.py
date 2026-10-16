"""Hermetic data ingestion regression checks; all test data are fabricated."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import change_manifest
import export_osm_csv_gz as exporter
import fetch_vn_geofabrik as downloader
import import_authorized_registry as reg
import package_osm_dataset_zip as packer
import sync_osm_into_atlas as sync
import verify_osm_snapshot as verifier
import vn_osm_dataset as osm


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.xml = ROOT / "examples/small_osm_synthetic.osm"
        self.osm_db = self.d / "osm.sqlite3"
        self.atlas_db = self.d / "atlas.sqlite3"

    def test_xml_import_nodes_ways_relations(self):
        result = osm.create_snapshot(self.xml, self.osm_db)
        self.assertEqual(result["inserted"], 4)
        self.assertEqual(result["active"], 4)
        self.assertEqual(result["licence"], "ODbL-1.0")
        self.assertEqual(osm.stats(self.osm_db)["node"], 2)
        self.assertEqual(osm.stats(self.osm_db)["way"], 1)
        self.assertEqual(osm.stats(self.osm_db)["relation"], 1)
        with closing(sqlite3.connect(self.osm_db)) as c:
            rows = c.execute("SELECT osm_type,osm_id,lat,lon,source_licence FROM osm_places").fetchall()
            self.assertEqual({row[0] for row in rows}, {"node", "way", "relation"})
            self.assertTrue(all(row[4] == "ODbL-1.0" for row in rows))
            self.assertTrue(all(7 < row[2] < 24 and 100 < row[3] < 111 for row in rows))

    def test_same_snapshot_second_run_is_noop(self):
        osm.create_snapshot(self.xml, self.osm_db)
        result = osm.create_snapshot(self.xml, self.osm_db)
        self.assertEqual(result["status"], "unchanged_snapshot_skipped")
        self.assertEqual(osm.stats(self.osm_db)["snapshots"], 1)
        third = osm.create_snapshot(self.xml, self.osm_db, force=True)
        self.assertEqual(third["inserted"], 0)
        self.assertEqual(third["updated"], 0)
        self.assertEqual(third["unchanged"], 4)
        self.assertEqual(osm.stats(self.osm_db)["active"], 4)

    def test_no_retirement_on_bounded_pilot(self):
        osm.create_snapshot(self.xml, self.osm_db)
        self.assertRaises(ValueError, osm.create_snapshot, self.xml, self.osm_db, max_poi=2, mark_missing=True)
        result = osm.create_snapshot(self.xml, self.osm_db, max_poi=2)
        self.assertEqual(result["active"], 4)
        self.assertEqual(result["retired"], 0)

    def test_deleted_osm_object_marked_inactive_only_if_explicit(self):
        osm.create_snapshot(self.xml, self.osm_db)
        data = self.xml.read_text(encoding="utf-8").replace('    <tag k="name" v="Nhà hàng THỬ NGHIỆM"/>', '    <tag k="name" v=""/>')
        changed = self.d / "changed.osm"
        changed.write_text(data, encoding="utf-8")
        first = osm.create_snapshot(changed, self.osm_db)
        self.assertEqual(first["active"], 4)
        second = osm.create_snapshot(changed, self.osm_db, mark_missing=True)
        self.assertEqual(second["retired"], 1)
        self.assertEqual(osm.stats(self.osm_db)["inactive"], 1)

    def test_failure_rolls_back_snapshot_transaction(self):
        osm.create_snapshot(self.xml, self.osm_db)
        broken = self.d / "broken.osm"
        broken.write_text('<osm><node id="1" lat="12" lon="106"><tag k="name" v="broken"/></notvalid>', encoding="utf-8")
        with self.assertRaises(ET.ParseError):
            osm.create_snapshot(broken, self.osm_db)
        self.assertEqual(osm.stats(self.osm_db)["snapshots"], 1)
        self.assertEqual(osm.stats(self.osm_db)["active"], 4)

    def test_geometry_validation(self):
        self.assertIsNone(osm.record("node", 2, {"name": "A", "shop": "yes"}, (float("nan"), 106)))
        self.assertIsNone(osm.record("node", 2, {"name": "A"}, (10, 106)))
        p = osm.centroid([(105, 20), (106, 20), (106, 21), (105, 21), (105, 20)])
        self.assertAlmostEqual(p[0], 20.5, places=3)
        self.assertAlmostEqual(p[1], 105.5, places=3)

    def test_qa_and_odbl_export(self):
        osm.create_snapshot(self.xml, self.osm_db)
        check = verifier.verify(self.osm_db, min_active=4)
        self.assertTrue(check["pass"])
        self.assertFalse(verifier.verify(self.osm_db, min_active=99999)["pass"])
        out = self.d / "backup" / "osm.csv.gz"
        result = exporter.export(self.osm_db, out)
        self.assertEqual(result["records"], 4)
        self.assertTrue(Path(result["rights_file"]).is_file())
        with gzip.open(out, "rt", encoding="utf-8") as file:
            lines = list(csv.DictReader(file))
        self.assertEqual(len(lines), 4)
        self.assertEqual({r["source_licence"] for r in lines}, {"ODbL-1.0"})

    def test_no_google_content_or_enterprise_claim(self):
        osm.create_snapshot(self.xml, self.osm_db)
        with closing(sqlite3.connect(self.osm_db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM osm_places WHERE source_licence != 'ODbL-1.0'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM osm_places WHERE source_url NOT LIKE '%openstreetmap.org%'").fetchone()[0], 0)

    def test_safe_zip_backup_contains_only_odbl_data(self):
        import json
        import zipfile

        osm.create_snapshot(self.xml, self.osm_db)
        out = self.d / "backups" / "osm-data.zip"
        result = packer.package(self.osm_db, out)
        self.assertEqual(result["active_poi_records"], 4)
        with zipfile.ZipFile(out) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(sorted(z.namelist()), ["DATA_RIGHTS.txt", "data/osm_vietnam.sqlite3", "manifest.json"])
            rights = z.read("DATA_RIGHTS.txt").decode("utf-8")
            self.assertIn("ODbL", rights)
            self.assertNotIn("Google Maps reviews", rights)
            self.assertEqual(json.loads(z.read("manifest.json"))["license"], "ODbL-1.0")

    def test_download_and_checksum_with_mock_official_http(self):
        class FakeResponse(io.BytesIO):
            status = 200

            def geturl(self):
                return downloader.URL

        body = b"fake-PBF-TEST-only-" * 70000
        expected = hashlib.sha256(body).hexdigest()
        dest = self.d / "latest.osm.pbf"
        meta = {"source_url": downloader.URL, "content_length": len(body), "last_modified": "Sun, 11 Oct 2026 00:00:00 GMT", "etag": '"testfixture"'}
        with (
            patch.object(downloader, "source_meta", return_value=meta),
            patch.object(downloader.urllib.request, "urlopen", side_effect=lambda *_a, **_kw: FakeResponse(body)) as request,
        ):
            result = downloader.download(dest)
            self.assertEqual(result["sha256"], expected)
            self.assertEqual(dest.stat().st_size, len(body))
            second = downloader.download(dest)
            self.assertEqual(second["status"], "downloaded")  # persisted provenance includes previous status
            self.assertEqual(request.call_count, 1, "same upstream hash/Last-Modified should reuse the cache")

    def test_registry_source_rights_required_and_idempotent(self):
        src = ROOT / "examples/registry_authorized_synthetic.csv"
        with self.assertRaises(ValueError):
            reg.import_file(src, self.d / "r.sqlite3", "test", "https://example.org/license", "")
        db = self.d / "r.sqlite3"
        first = reg.import_file(src, db, "test", "https://example.org/license", "Synthetic fixture only")
        self.assertEqual(first["created"], 2)
        again = reg.import_file(src, db, "test", "https://example.org/license", "Synthetic fixture only")
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["unchanged"], 2)
        corrected = reg.import_file(src, db, "test", "https://example.org/license-v2", "Synthetic fixture only")
        self.assertEqual(corrected["changed"], 2)
        self.assertEqual(corrected["unchanged"], 0)
        with closing(sqlite3.connect(db)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM legal_entities").fetchone()[0], 2)
            self.assertEqual({r[0] for r in c.execute("SELECT source_url FROM legal_entities")}, {"https://example.org/license-v2"})

    def test_atlas_dry_run_is_safe(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        osm.create_snapshot(self.xml, self.osm_db)
        preview = sync.sync(self.osm_db, self.atlas_db)
        self.assertEqual(preview["would_insert"], 4)
        self.assertFalse(self.atlas_db.exists(), "dry run must not create/modify live DB")
        result = sync.sync(self.osm_db, self.atlas_db, apply=True)
        self.assertEqual(result["inserted"], 4)
        again = sync.sync(self.osm_db, self.atlas_db, apply=True)
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["existing"], 4)
        self.assertIsNone(again["backup"])  # repeated unchanged sync creates no backup
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM places").fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT count(*) FROM places WHERE source='OpenStreetMap'").fetchone()[0], 4)

    def test_verified_user_edits_never_overwritten(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='verified',notes='User-verified note' WHERE source_id='node/1001'")
            c.commit()
        data = self.xml.read_text(encoding="utf-8").replace("Quán cà phê THỬ NGHIỆM", "Quán cà phê MỚI THỬ NGHIỆM")
        changed = self.d / "changed.osm"
        changed.write_text(data, encoding="utf-8")
        osm.create_snapshot(changed, self.osm_db)
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, update_existing=True)
        self.assertEqual(result["protected"], 1)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            row = c.execute("SELECT name,notes FROM places WHERE source_id='node/1001'").fetchone()
            self.assertEqual(row[0], "Quán cà phê THỬ NGHIỆM")
            self.assertEqual(row[1], "User-verified note")

    def test_retired_staging_records_mark_unverified_atlas_rows_inactive(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        state = self.d / "retired_by_sync.json"
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='verified' WHERE source_id='node/1001'")
            c.commit()
        data = self.xml.read_text(encoding="utf-8").replace('    <tag k="name" v="Nhà hàng THỬ NGHIỆM"/>', '    <tag k="name" v=""/>')
        changed = self.d / "changed.osm"
        changed.write_text(data, encoding="utf-8")
        osm.create_snapshot(changed, self.osm_db, mark_missing=True)
        preview = sync.sync(self.osm_db, self.atlas_db, retired_state=state)
        self.assertEqual(preview["would_retire"], 1)
        self.assertEqual(preview["retired"], 0)
        blocked = sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        self.assertEqual(blocked["retired"], 0)  # lifecycle writes need --apply-lifecycle
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='relation/3001'").fetchone()[0], "unverified")
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        self.assertEqual(result["retired"], 1)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='relation/3001'").fetchone()[0], "inactive")
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1001'").fetchone()[0], "verified")
            self.assertEqual(c.execute("SELECT count(*) FROM places WHERE status='inactive'").fetchone()[0], 1)
        self.assertIn("relation/3001", json.loads(state.read_text(encoding="utf-8"))["sids"])  # sổ retire ghi lại nguồn gốc

    def test_reappeared_poi_restore_gated_and_preserves_manual_fields(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        state = self.d / "retired_by_sync.json"
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET notes='giữ-nguyên',province='TT' WHERE source_id='relation/3001'")
            c.commit()
        missing = self.d / "missing.osm"
        missing.write_text(
            self.xml.read_text(encoding="utf-8").replace('    <tag k="name" v="Nhà hàng THỬ NGHIỆM"/>', '    <tag k="name" v=""/>'), encoding="utf-8"
        )
        osm.create_snapshot(missing, self.osm_db, mark_missing=True)
        sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        back = self.d / "back.osm"
        back.write_text(self.xml.read_text(encoding="utf-8").replace("Nhà hàng THỬ NGHIỆM", "Nhà hàng TÁI XUẤT"), encoding="utf-8")
        osm.create_snapshot(back, self.osm_db)
        preview = sync.sync(self.osm_db, self.atlas_db, retired_state=state)
        self.assertEqual(preview["would_restore"], 1)
        self.assertEqual(preview["restored"], 0)
        self.assertEqual(preview["restore_samples"], [{"source_id": "relation/3001", "name": "Nhà hàng TÁI XUẤT"}])
        self.assertEqual(preview["changed_in_source"], 1)  # inactive row with renamed source also counts as evidence
        blocked = sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        self.assertEqual(blocked["restored"], 0)  # apply without --apply-lifecycle must not write
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='relation/3001'").fetchone()[0], "inactive")
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        self.assertTrue(result["apply_lifecycle"])
        self.assertEqual(result["restored"], 1)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            row = c.execute("SELECT name,status,notes,province FROM places WHERE source_id='relation/3001'").fetchone()
            self.assertEqual(tuple(row), ("Nhà hàng TÁI XUẤT", "unverified", "giữ-nguyên", "TT"))
        self.assertNotIn("relation/3001", json.loads(state.read_text(encoding="utf-8"))["sids"])  # khôi phục xong thì xoá khỏi sổ

    def test_verified_and_closed_rows_never_retire_or_restore(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        state = self.d / "retired_by_sync.json"
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='verified' WHERE source_id='node/1001'")
            c.execute("UPDATE places SET status='closed' WHERE source_id='node/1002'")
            c.commit()
        data = self.xml.read_text(encoding="utf-8")
        for name in ("Quán cà phê THỬ NGHIỆM", "Cửa hàng THỬ NGHIỆM", "Nhà hàng THỬ NGHIỆM"):
            data = data.replace(f'    <tag k="name" v="{name}"/>', '    <tag k="name" v=""/>')
        missing = self.d / "missing.osm"
        missing.write_text(data, encoding="utf-8")
        osm.create_snapshot(missing, self.osm_db, mark_missing=True)
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        self.assertEqual(result["would_retire"], 1)  # only the unverified relation row is a candidate
        self.assertEqual(result["retired"], 1)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1001'").fetchone()[0], "verified")
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1002'").fetchone()[0], "closed")
        osm.create_snapshot(self.xml, self.osm_db, force=True)  # same file+scope was already imported once
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        self.assertEqual(result["restored"], 1)  # relation/3001 only; verified/closed stay untouched
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1001'").fetchone()[0], "verified")
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1002'").fetchone()[0], "closed")

    def test_changed_in_source_counts_regardless_of_update_flag(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='verified' WHERE source_id='node/1002'")
            c.commit()
        renamed = self.d / "renamed.osm"
        renamed.write_text(
            self.xml.read_text(encoding="utf-8")
            .replace("Quán cà phê THỬ NGHIỆM", "Quán cà phê ĐỔI TÊN")
            .replace("Cửa hàng THỬ NGHIỆM", "Cửa hàng ĐỔI TÊN"),
            encoding="utf-8",
        )
        osm.create_snapshot(renamed, self.osm_db)
        dry = sync.sync(self.osm_db, self.atlas_db)
        self.assertEqual(dry["changed_in_source"], 1)  # unverified node/1001; verified node/1002 stays protected
        self.assertEqual(dry["would_update"], 0)
        upd = sync.sync(self.osm_db, self.atlas_db, update_existing=True)
        self.assertEqual(upd["changed_in_source"], 1)
        self.assertEqual(upd["would_update"], 1)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT name FROM places WHERE source_id='node/1001'").fetchone()[0], "Quán cà phê THỬ NGHIỆM")
        sync.sync(self.osm_db, self.atlas_db, apply=True, update_existing=True)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT name FROM places WHERE source_id='node/1001'").fetchone()[0], "Quán cà phê ĐỔI TÊN")
            self.assertEqual(c.execute("SELECT name FROM places WHERE source_id='node/1002'").fetchone()[0], "Cửa hàng THỬ NGHIỆM")

    def test_position_jump_over_50km_reported_only(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True)
        moved = self.d / "moved.osm"
        moved.write_text(
            self.xml.read_text(encoding="utf-8")
            .replace('lat="21.0325" lon="105.8510"', 'lat="22.5000" lon="105.8510"')  # ~163 km north
            .replace('lat="21.0326" lon="105.8520"', 'lat="21.0326" lon="106.3000"'),  # ~46 km east, under the threshold
            encoding="utf-8",
        )
        osm.create_snapshot(moved, self.osm_db)
        dry = sync.sync(self.osm_db, self.atlas_db)
        self.assertEqual(dry["changed_in_source"], 2)
        self.assertEqual(dry["position_jumps_total"], 1)
        self.assertEqual(len(dry["position_jumps"]), 1)
        jump = dry["position_jumps"][0]
        self.assertEqual(jump["source_id"], "node/1001")
        self.assertEqual(jump["from"], [21.0325, 105.851])
        self.assertEqual(jump["to"], [22.5, 105.851])
        self.assertGreater(jump["km"], 50)
        with closing(sqlite3.connect(self.atlas_db)) as c:  # report-only: the atlas row is not touched
            self.assertEqual(c.execute("SELECT lat FROM places WHERE source_id='node/1001'").fetchone()[0], 21.0325)

    def test_limit_skips_lifecycle_evaluation(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        state = self.d / "retired_by_sync.json"
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='inactive' WHERE source_id='node/1001'")  # restore candidate
            c.commit()
        missing = self.d / "missing.osm"
        missing.write_text(
            self.xml.read_text(encoding="utf-8").replace('    <tag k="name" v="Nhà hàng THỬ NGHIỆM"/>', '    <tag k="name" v=""/>'), encoding="utf-8"
        )
        osm.create_snapshot(missing, self.osm_db, mark_missing=True)  # retire candidate
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, limit=1, retired_state=state)
        self.assertTrue(result["retire_skipped_limit"])
        self.assertTrue(result["restore_skipped_limit"])
        self.assertEqual(result["would_restore"], 0)
        self.assertEqual(result["would_retire"], 0)
        self.assertEqual(result["restored"], 0)
        self.assertEqual(result["retired"], 0)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1001'").fetchone()[0], "inactive")
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='relation/3001'").fetchone()[0], "unverified")

    def test_restore_never_overrides_operator_inactive_rows(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        state = self.d / "retired_by_sync.json"
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        # Người vận hành tự đặt inactive cho một POI vẫn còn active trong staging (không có trong sổ sync).
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='inactive',notes='do-operator' WHERE source_id='relation/3001'")
            c.commit()
        dry = sync.sync(self.osm_db, self.atlas_db, retired_state=state)
        self.assertEqual(dry["would_restore"], 0)
        self.assertEqual(dry["restore_blocked_manual"], 1)
        applied = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        self.assertEqual(applied["restored"], 0)  # không ghi đè quyết định của người vận hành
        self.assertTrue(applied["apply_lifecycle"])  # APPLY_NO_CHANGES vẫn giữ đúng cờ vòng đời
        with closing(sqlite3.connect(self.atlas_db)) as c:
            row = c.execute("SELECT status,notes FROM places WHERE source_id='relation/3001'").fetchone()
            self.assertEqual(tuple(row), ("inactive", "do-operator"))

    def test_pilot_reactivation_is_not_restore_evidence(self):
        if not (ROOT / "server/store.py").is_file():
            self.skipTest("Run after copying toolkit into a Vietnam_Atlas checkout")
        state = self.d / "retired_by_sync.json"
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True, retired_state=state)
        missing = self.d / "missing.osm"
        missing.write_text(
            self.xml.read_text(encoding="utf-8").replace('    <tag k="name" v="Nhà hàng THỬ NGHIỆM"/>', '    <tag k="name" v=""/>'), encoding="utf-8"
        )
        osm.create_snapshot(missing, self.osm_db, mark_missing=True)
        retired = sync.sync(self.osm_db, self.atlas_db, apply=True, apply_lifecycle=True, retired_state=state)
        self.assertEqual(retired["retired"], 1)  # sổ đã ghi relation/3001
        back = self.d / "back.osm"
        back.write_text(self.xml.read_text(encoding="utf-8").replace("Nhà hàng THỬ NGHIỆM", "Nhà hàng TÁI XUẤT"), encoding="utf-8")
        osm.create_snapshot(back, self.osm_db, max_poi=99)  # pilot import kích hoạt lại dòng staging
        dry = sync.sync(self.osm_db, self.atlas_db, retired_state=state)
        self.assertEqual(dry["would_restore"], 0)  # tái kích hoạt bởi pilot KHÔNG đủ bằng chứng
        self.assertEqual(dry["restore_blocked_manual"], 0)
        osm.create_snapshot(back, self.osm_db)  # import full xác nhận lại sự tái xuất hiện
        dry2 = sync.sync(self.osm_db, self.atlas_db, retired_state=state)
        self.assertEqual(dry2["would_restore"], 1)

    def test_import_result_reports_skip_reasons(self):
        result = osm.create_snapshot(self.xml, self.osm_db)
        self.assertEqual(result["skip_reasons"], {"missing_name": 10})
        self.assertEqual(result["skipped"], sum(result["skip_reasons"].values()))  # hai bộ đếm luôn khớp
        mini = self.d / "mini.osm"
        mini.write_text(
            "<osm>"
            '<node id="1" lon="106"><tag k="name" v="x"/></node>'
            '<node id="2" lat="10" lon="106"><tag k="amenity" v="cafe"/></node>'
            '<node id="3" lat="10.5" lon="106"><tag k="name" v="y"/></node>'
            "</osm>",
            encoding="utf-8",
        )
        res = osm.create_snapshot(mini, self.d / "mini.sqlite3")
        self.assertEqual(res["skip_reasons"], {"missing_or_invalid_coordinates": 1, "missing_name": 1, "unsupported_category": 1})
        self.assertEqual(res["skipped"], 3)
        # Sau khi pilot đầy quota, các đối tượng bị bỏ tiếp theo nằm ngoài phạm vi và không được đếm.
        pilot = self.d / "pilot.osm"
        pilot.write_text(
            "<osm>"
            '<node id="1" lon="106"><tag k="name" v="x"/></node>'
            '<node id="2" lat="10" lon="106"><tag k="name" v="ok"/><tag k="amenity" v="cafe"/></node>'
            '<node id="3" lat="11" lon="106"><tag k="amenity" v="cafe"/></node>'
            "</osm>",
            encoding="utf-8",
        )
        sized = osm.create_snapshot(pilot, self.d / "pilot.sqlite3", max_poi=1)
        self.assertEqual(sized["inserted"], 1)
        self.assertEqual(sized["skip_reasons"], {"missing_or_invalid_coordinates": 1})  # chỉ bản ghi trước quota
        self.assertEqual(sized["skipped"], 1)

    def test_verify_require_full_evidence(self):
        # A pilot scope (limit) is a valid snapshot but cannot certify full coverage.
        osm.create_snapshot(self.xml, self.osm_db, max_poi=2)
        pilot = verifier.verify(self.osm_db)
        self.assertEqual(pilot["full_snapshots"], 0)
        self.assertTrue(pilot["pass"])
        self.assertFalse(verifier.verify(self.osm_db, require_full=True)["pass"])
        # A full scope (no max_poi) certifies it and is reported with its own counters.
        osm.create_snapshot(self.xml, self.osm_db, force=True)
        full = verifier.verify(self.osm_db, require_full=True)
        self.assertEqual(full["full_snapshots"], 1)
        self.assertTrue(full["pass"])
        self.assertTrue(full["latest_snapshot_full"])
        self.assertEqual(full["latest_snapshot"]["mode"], "xml:full:retire=0")
        self.assertEqual(full["latest_snapshot"]["unchanged"], 2)
        # A later bounded pilot invalidates the certification: the latest import must be full.
        osm.create_snapshot(self.xml, self.osm_db, max_poi=2, force=True)
        stale = verifier.verify(self.osm_db, require_full=True)
        self.assertEqual(stale["full_snapshots"], 1)  # history keeps the old full import
        self.assertFalse(stale["latest_snapshot_full"])
        self.assertFalse(stale["pass"])

    def test_sync_coverage_report_shape(self):
        osm.create_snapshot(self.xml, self.osm_db)
        result = sync.sync(self.osm_db, self.atlas_db, apply=True, coverage=True)
        cov = result["coverage"]
        self.assertEqual(cov["by_osm_type"], {"node": 2, "way": 1, "relation": 1})
        self.assertEqual(cov["cells_with_data"], 1)  # fixture POIs share a single 1° cell
        self.assertEqual(sum(c["count"] for c in cov["grid_1deg_top_cells"]), 4)
        self.assertEqual(sum(c["count"] for c in cov["top_categories"]), 4)
        self.assertNotIn("coverage", sync.sync(self.osm_db, self.atlas_db))  # opt-in flag only
        # Repeat apply on an unchanged atlas: the no-change path must still carry the report.
        again = sync.sync(self.osm_db, self.atlas_db, apply=True, coverage=True)
        self.assertEqual(again["mode"], "APPLY_NO_CHANGES")
        self.assertEqual(again["coverage"]["cells_with_data"], 1)

    def test_sync_emits_change_manifest_sidecar(self):
        """--manifest ghi sidecar fingerprint từng source_id; console JSON không đổi."""
        osm.create_snapshot(self.xml, self.osm_db)
        manifest = self.d / "nested" / "change-manifest.json"
        result = sync.sync(self.osm_db, self.atlas_db, manifest_path=manifest)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertIsNone(change_manifest.validate_manifest(payload))
        self.assertEqual(payload["entry_count"], result["would_insert"])
        self.assertEqual(payload["entry_count"], 4)
        self.assertFalse(payload["update_existing"])
        self.assertFalse(payload["apply_lifecycle"])
        self.assertEqual(payload["manifest_hash"], change_manifest.manifest_hash(payload["entries"]))
        self.assertEqual({e["action"] for e in payload["entries"]}, {"insert"})
        source_ids = [e["source_id"] for e in payload["entries"]]
        self.assertEqual(source_ids, sorted(source_ids))
        # Atlas chưa tồn tại -> fingerprint schema "absent" (giá trị ổn định, vẫn so sánh được).
        self.assertEqual(payload["schema_version"], "absent")
        with closing(sqlite3.connect(self.osm_db)) as con:
            expected_sha = con.execute("SELECT file_sha256 FROM ingestion_snapshots ORDER BY imported_at DESC, rowid DESC LIMIT 1").fetchone()[0]
        self.assertEqual(payload["snapshot_sha"], expected_sha)
        # Cùng input -> cùng manifest_hash (hash ổn định, không phụ thuộc thứ tự xử lý).
        again = self.d / "change-manifest-2.json"
        sync.sync(self.osm_db, self.atlas_db, manifest_path=again)
        self.assertEqual(json.loads(again.read_text(encoding="utf-8"))["manifest_hash"], payload["manifest_hash"])
        # Sau apply không còn thay đổi -> manifest rỗng; schema Atlas giờ có fingerprint DDL.
        sync.sync(self.osm_db, self.atlas_db, apply=True)
        empty = self.d / "change-manifest-3.json"
        sync.sync(self.osm_db, self.atlas_db, update_existing=True, manifest_path=empty)
        empty_payload = json.loads(empty.read_text(encoding="utf-8"))
        self.assertEqual(empty_payload["entry_count"], 0)
        self.assertTrue(empty_payload["update_existing"])
        self.assertTrue(empty_payload["schema_version"].startswith("ddl-sha256:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
