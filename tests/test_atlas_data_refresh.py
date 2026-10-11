"""Hermetic data ingestion regression checks; all test data are fabricated."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
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
        osm.create_snapshot(self.xml, self.osm_db)
        sync.sync(self.osm_db, self.atlas_db, apply=True)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            c.execute("UPDATE places SET status='verified' WHERE source_id='node/1001'")
            c.commit()
        data = self.xml.read_text(encoding="utf-8").replace('    <tag k="name" v="Nhà hàng THỬ NGHIỆM"/>', '    <tag k="name" v=""/>')
        changed = self.d / "changed.osm"
        changed.write_text(data, encoding="utf-8")
        osm.create_snapshot(changed, self.osm_db, mark_missing=True)
        preview = sync.sync(self.osm_db, self.atlas_db)
        self.assertEqual(preview["would_retire"], 1)
        self.assertEqual(preview["retired"], 0)
        result = sync.sync(self.osm_db, self.atlas_db, apply=True)
        self.assertEqual(result["retired"], 1)
        with closing(sqlite3.connect(self.atlas_db)) as c:
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='relation/3001'").fetchone()[0], "inactive")
            self.assertEqual(c.execute("SELECT status FROM places WHERE source_id='node/1001'").fetchone()[0], "verified")
            self.assertEqual(c.execute("SELECT count(*) FROM places WHERE status='inactive'").fetchone()[0], 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
