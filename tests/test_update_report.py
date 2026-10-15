"""Tests for tools/update_report.py (T-102 Round D - update-cycle report).

Hermetic: all outputs go to tempfile.TemporaryDirectory; fixtures are fabricated
inline. The module is loaded by path (importlib) so no sys.path mutation and no
E402 (pyproject per-file-ignores only covers test_atlas_data_refresh.py).
"""

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location("update_report", REPO_ROOT / "tools" / "update_report.py")
update_report = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(update_report)

PREVIEW = {
    "mode": "DRY_RUN",
    "would_insert": 12,
    "existing": 8901,
    "would_update": 34,
    "protected": 67,
    "would_retire": 5,
    "retired": 0,
    "retire_skipped_limit": False,
    "inserted": 0,
    "updated": 0,
    "skipped_invalid": 0,
    "backup": None,
}

PROVENANCE = {
    "source_url": "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf",
    "content_length": 330248138,
    "last_modified": "Fri, 09 Oct 2026 22:46:01 GMT",
    "etag": '"13af2fca-65d701cd97cad"',
    "downloaded_utc": "2026-10-11T01:10:25.554691+00:00",
    "size_bytes": 330248138,
    "sha256": "2fbd2ee6b4dc151eecf04fa6823cddf584b47423e190118b10fce8b6115e9d8a",
    "status": "downloaded",
}

STAMP = "20260101T000000Z"

MD_SECTIONS = ("Nguồn OSM", "Kết quả so sánh với Atlas", "Staging/verify", "Kết luận")
E1_MD_SECTIONS = (
    "Snapshot & ràng buộc phê duyệt",
    "Chất lượng dữ liệu",
    "Ứng viên retire (staging-only)",
    "POI tái xuất hiện (would_restore)",
    "Kiểm tra sau apply",
)


def _make_staging_db(path, places=(), snapshots=()):
    """Minimal staging fixture: only the columns update_report.py actually queries."""
    con = sqlite3.connect(path)
    try:
        con.executescript("""
            CREATE TABLE osm_places (
                osm_type TEXT NOT NULL,
                osm_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                last_snapshot TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(osm_type, osm_id)
            );
            CREATE TABLE ingestion_snapshots (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                mode TEXT NOT NULL,
                source_date TEXT,
                imported_at TEXT NOT NULL
            );
            """)
        con.executemany(
            "INSERT INTO osm_places(osm_type,osm_id,name,lat,lon,last_snapshot,is_active) VALUES(?,?,?,?,?,?,?)",
            places,
        )
        con.executemany(
            "INSERT INTO ingestion_snapshots(id,filename,file_sha256,mode,source_date,imported_at) VALUES(?,?,?,?,?,?)",
            snapshots,
        )
        con.commit()
    finally:
        con.close()
    return path


def _make_atlas_db(path, places=()):
    """Minimal atlas fixture: only source/source_id/status are read."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE places ("
            "id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'user', "
            "source_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unverified')"
        )
        con.executemany("INSERT INTO places(id,source,source_id,status) VALUES(?,?,?,?)", places)
        con.commit()
    finally:
        con.close()
    return path


class TestBuildReport(unittest.TestCase):
    def test_normal_counts_and_change_verdict(self):
        report, md = update_report.build_report(json.dumps(PREVIEW), PROVENANCE, stamp=STAMP)
        self.assertEqual(report["stamp"], STAMP)
        self.assertEqual(report["mode"], "DRY_RUN")
        self.assertEqual(report["would_insert"], 12)
        self.assertEqual(report["would_update"], 34)
        self.assertEqual(report["would_retire"], 5)
        self.assertEqual(report["protected"], 67)
        self.assertEqual(report["existing"], 8901)
        self.assertEqual(report["verdict"], "change")
        self.assertEqual(report["provenance"]["source_url"], PROVENANCE["source_url"])
        self.assertEqual(report["provenance"]["sha256"], PROVENANCE["sha256"])
        self.assertEqual(report["artifacts"]["markdown"], f"reports/update-{STAMP}.md")
        for section in MD_SECTIONS:
            self.assertIn(section, md)
        self.assertIn("CÓ thay đổi — xem xét trước khi chạy -Apply", md)
        self.assertIn(PROVENANCE["sha256"][:12], md)
        self.assertIn(str(PROVENANCE["size_bytes"]), md)

    def test_zero_change_idempotent(self):
        zero = dict(PREVIEW, would_insert=0, would_update=0, would_retire=0)
        report, md = update_report.build_report(json.dumps(zero), PROVENANCE, stamp=STAMP)
        self.assertEqual(report["verdict"], "idempotent")
        self.assertIn("Không thay đổi — chu kỳ lặp idempotent", md)

    def test_noisy_wrapper_text_around_json(self):
        noisy = "[2026-01-01 00:00:00] bat dau sync {not json}\n" + json.dumps(PREVIEW, indent=2, ensure_ascii=False) + "\nhoan tat {x}\n"
        report, _ = update_report.build_report(noisy, stamp=STAMP)
        self.assertEqual(report["would_insert"], 12)
        self.assertEqual(report["would_retire"], 5)
        self.assertEqual(report["existing"], 8901)
        self.assertEqual(report["verdict"], "change")

    def test_braces_inside_json_strings_are_ignored(self):
        payload = dict(PREVIEW, would_insert=1, would_update=0, would_retire=0, note="dấu } và { trong chuỗi")
        report, _ = update_report.build_report(json.dumps(payload, ensure_ascii=False), stamp=STAMP)
        self.assertEqual(report["would_insert"], 1)
        self.assertEqual(report["verdict"], "change")

    def test_trailing_unrelated_json_object_is_ignored(self):
        text = json.dumps(PREVIEW) + "\n" + json.dumps({"event": "cleanup", "ok": True}) + "\n"
        report, _ = update_report.build_report(text, stamp=STAMP)
        self.assertEqual(report["would_insert"], 12)
        self.assertEqual(report["verdict"], "change")

    def test_only_unrelated_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            update_report.build_report(json.dumps({"event": "cleanup", "ok": True}), stamp=STAMP)

    def test_unparseable_text_raises_value_error(self):
        with self.assertRaises(ValueError):
            update_report.build_report("khong co JSON o day {", stamp=STAMP)


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.out_dir = self.tmp / "reports"

    def write_preview(self, text, encoding="utf-8", bom=None):
        path = self.tmp / "preview.json"
        data = text.encode(encoding)
        path.write_bytes((bom or b"") + data)
        return path

    def run_cli(self, preview_path, extra=()):
        argv = [
            "update_report.py",
            "--preview",
            str(preview_path),
            "--out-dir",
            str(self.out_dir),
            "--stamp",
            STAMP,
            *extra,
        ]
        with mock.patch.object(sys, "argv", argv):
            update_report.main()

    def read_report(self):
        return json.loads((self.out_dir / f"update-{STAMP}.json").read_text(encoding="utf-8"))

    def test_cli_writes_both_artifacts_with_counts(self):
        prov = self.tmp / "prov.json"
        prov.write_text(json.dumps(PROVENANCE), encoding="utf-8")
        stats = self.tmp / "stats.txt"
        stats.write_text("staging stats fixture", encoding="utf-8")
        verify = self.tmp / "verify.txt"
        verify.write_text("verify ok", encoding="utf-8")
        preview = self.write_preview(json.dumps(PREVIEW, indent=2))
        self.run_cli(preview, ["--provenance", str(prov), "--staging-stats", str(stats), "--verify", str(verify)])
        json_path = self.out_dir / f"update-{STAMP}.json"
        md_path = self.out_dir / f"update-{STAMP}.md"
        self.assertTrue(json_path.is_file())
        self.assertTrue(md_path.is_file())
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(report["stamp"], STAMP)
        self.assertEqual(report["would_insert"], 12)
        self.assertEqual(report["would_update"], 34)
        self.assertEqual(report["would_retire"], 5)
        self.assertEqual(report["protected"], 67)
        self.assertEqual(report["existing"], 8901)
        self.assertEqual(report["verdict"], "change")
        self.assertEqual(report["provenance"]["source_url"], PROVENANCE["source_url"])
        self.assertEqual(report["provenance"]["last_modified"], PROVENANCE["last_modified"])
        self.assertEqual(report["provenance"]["sha256"], PROVENANCE["sha256"])
        self.assertEqual(report["provenance"]["size_bytes"], PROVENANCE["size_bytes"])
        md = md_path.read_text(encoding="utf-8")
        for section in MD_SECTIONS:
            self.assertIn(section, md)
        self.assertIn("CÓ thay đổi — xem xét trước khi chạy -Apply", md)
        self.assertIn(PROVENANCE["sha256"][:12], md)
        self.assertIn(stats.as_posix(), md)
        self.assertIn(verify.as_posix(), md)
        self.assertIn(preview.as_posix(), md)

    def test_cli_zero_change_idempotent(self):
        zero = dict(PREVIEW, would_insert=0, would_update=0, would_retire=0)
        preview = self.write_preview(json.dumps(zero))
        self.run_cli(preview)
        self.assertEqual(self.read_report()["verdict"], "idempotent")
        md = (self.out_dir / f"update-{STAMP}.md").read_text(encoding="utf-8")
        self.assertIn("Không thay đổi — chu kỳ lặp idempotent", md)

    def test_cli_noisy_preview_still_parses(self):
        noisy = "noise {brace}\n" + json.dumps(PREVIEW) + "\nmore } noise\n"
        preview = self.write_preview(noisy)
        self.run_cli(preview)
        self.assertEqual(self.read_report()["would_retire"], 5)

    def test_cli_reads_utf16_bom_capture_like_tee_object(self):
        # PowerShell 5.1 Tee-Object writes UTF-16LE with BOM; the reader must cope.
        preview = self.write_preview(json.dumps(PREVIEW, indent=2, ensure_ascii=False), encoding="utf-16-le", bom=b"\xff\xfe")
        self.run_cli(preview)
        self.assertEqual(self.read_report()["would_insert"], 12)

    def test_cli_missing_preview_file_exits_nonzero(self):
        argv = [
            "update_report.py",
            "--preview",
            str(self.tmp / "nope.json"),
            "--out-dir",
            str(self.out_dir),
            "--stamp",
            STAMP,
        ]
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                update_report.main()
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("preview", stderr.getvalue())

    def test_cli_unparseable_preview_exits_nonzero(self):
        preview = self.write_preview("no json here at all {")
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", self.cli_argv(preview)), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                update_report.main()
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("preview", stderr.getvalue())

    def test_cli_e1_optional_args_wired(self):
        import_json = self.tmp / "import.json"
        import_json.write_text(json.dumps({"status": "imported", "skip_reasons": {"no_name": 3}}), encoding="utf-8")
        post_apply = self.tmp / "postapply.json"
        post_apply.write_text(json.dumps({"would_insert": 0, "would_update": 1}), encoding="utf-8")
        pbf = self.tmp / "vietnam-latest.osm.pbf"
        pbf.write_bytes(b"pbf")
        preview = self.write_preview(json.dumps(PREVIEW))
        self.run_cli(
            preview,
            [
                "--import-json",
                str(import_json),
                "--post-apply",
                str(post_apply),
                "--pbf",
                str(pbf),
                "--staging-db",
                str(self.tmp / "no-staging.sqlite3"),
                "--atlas-db",
                str(self.tmp / "no-atlas.sqlite3"),
                "--now",
                "2026-01-01T00:00:00+00:00",
            ],
        )
        report = self.read_report()
        self.assertEqual(report["quality"]["skip_reasons"], {"no_name": 3})
        self.assertEqual(report["post_apply"], {"would_insert": 0, "would_update": 1, "ok": False})
        self.assertEqual(report["snapshot"]["bytes"], 3)
        self.assertEqual(report["stage"], "preview")
        self.assertIsNone(report["staging"])  # staging db absent -> skipped quietly

    def cli_argv(self, preview_path):
        return [
            "update_report.py",
            "--preview",
            str(preview_path),
            "--out-dir",
            str(self.out_dir),
            "--stamp",
            STAMP,
        ]


class TestRetireCandidates(unittest.TestCase):
    """Direct state-machine tests for update_retire_candidates (spec F)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "_state" / "retire_candidates.json"

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_new_sid_becomes_candidate(self):
        result = update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-a")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["new_this_run"], 1)
        self.assertEqual(result["ready_for_review"], 0)
        entry = self.read_state()["candidates"]["node/1"]
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(entry["first_missing_sha"], "sha-a")
        self.assertEqual(entry["snapshots_absent"], ["sha-a"])
        self.assertEqual(entry["first_missing_at"], "2026-01-01T00:00:00+00:00")

    def test_ready_needs_two_distinct_shas_and_seven_days(self):
        update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-a")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-01T00:00:00+00:00",
        )
        # Second distinct sha but only 3 days elapsed: still a candidate.
        mid = update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-b")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-04T00:00:00+00:00",
        )
        self.assertEqual(mid["ready_for_review"], 0)
        self.assertEqual(self.read_state()["candidates"]["node/1"]["status"], "candidate")
        # Same (already-seen) sha at day 8: 2 distinct shas + >=7 days -> ready.
        done = update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-b")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-09T00:00:00+00:00",
        )
        self.assertEqual(done["ready_for_review"], 1)
        entry = self.read_state()["candidates"]["node/1"]
        self.assertEqual(entry["status"], "ready_for_review")
        self.assertEqual(entry["snapshots_absent"], ["sha-a", "sha-b"])

    def test_same_sha_repeated_never_ready(self):
        for day in ("2026-01-01T00:00:00+00:00", "2026-01-15T00:00:00+00:00"):
            result = update_report.update_retire_candidates(
                [("node/1", "Quan A", "sha-a")],
                {"node/1": "unverified"},
                self.state_path,
                now_iso=day,
            )
        self.assertEqual(result["ready_for_review"], 0)  # only 1 distinct snapshot sha
        self.assertEqual(self.read_state()["candidates"]["node/1"]["snapshots_absent"], ["sha-a"])

    def test_reappeared_sid_is_cancelled(self):
        update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-a")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-01T00:00:00+00:00",
        )
        result = update_report.update_retire_candidates(
            [],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-02T00:00:00+00:00",
        )
        self.assertEqual(result["cancelled_reappeared"], 1)
        self.assertEqual(result["total"], 0)
        entry = self.read_state()["candidates"]["node/1"]
        self.assertEqual(entry["status"], "cancelled_reappeared")
        self.assertEqual(entry["cancelled_at"], "2026-01-02T00:00:00+00:00")

    def test_protected_missing_and_no_atlas_row_excluded(self):
        result = update_report.update_retire_candidates(
            [("node/1", "Verified", "sha-a"), ("node/2", "NoAtlas", "sha-a")],
            {"node/1": "verified"},  # node/2 has no OpenStreetMap row in atlas
            self.state_path,
            now_iso="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(result["protected_missing"], 1)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["new_this_run"], 0)
        self.assertEqual(self.read_state()["candidates"], {})

    def test_protected_while_missing_cancels_candidacy(self):
        update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-a")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-01T00:00:00+00:00",
        )
        # Still missing on day 9, but the atlas row became verified: close it.
        result = update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-b")],
            {"node/1": "verified"},
            self.state_path,
            now_iso="2026-01-10T00:00:00+00:00",
        )
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["ready_for_review"], 0)
        self.assertEqual(result["cancelled_protected"], 1)
        self.assertEqual(result["protected_missing"], 1)
        entry = self.read_state()["candidates"]["node/1"]
        self.assertEqual(entry["status"], "cancelled_protected")
        self.assertEqual(entry["cancelled_at"], "2026-01-10T00:00:00+00:00")

    def test_vanished_atlas_row_also_cancels_candidacy(self):
        update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-a")],
            {"node/1": "unverified"},
            self.state_path,
            now_iso="2026-01-01T00:00:00+00:00",
        )
        # Atlas row deleted while staging row stays inactive: close it too.
        result = update_report.update_retire_candidates(
            [("node/1", "Quan A", "sha-b")],
            {},
            self.state_path,
            now_iso="2026-01-10T00:00:00+00:00",
        )
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["cancelled_protected"], 1)
        self.assertEqual(self.read_state()["candidates"]["node/1"]["status"], "cancelled_protected")

    def test_state_keeps_active_and_caps_cancelled_at_1000(self):
        cancelled = {
            f"node/{i}": {
                "first_missing_sha": "sha",
                "first_missing_at": "2026-01-01T00:00:00+00:00",
                "snapshots_absent": ["sha"],
                "last_absent_at": "2026-01-01T00:00:00+00:00",
                "status": "cancelled_reappeared",
                "cancelled_at": f"2026-02-{(i % 28) + 1:02d}T00:00:00+00:00",
                "name": f"Old {i}",
            }
            for i in range(1005)
        }
        cancelled["node/keep"] = {
            "first_missing_sha": "sha-k",
            "first_missing_at": "2026-01-01T00:00:00+00:00",
            "snapshots_absent": ["sha-k"],
            "last_absent_at": "2026-01-01T00:00:00+00:00",
            "status": "candidate",
            "name": "Keep",
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"schema": 1, "updated_at": None, "candidates": cancelled}), encoding="utf-8")
        result = update_report.update_retire_candidates(
            [("node/keep", "Keep", "sha-k2")],
            {"node/keep": "unverified"},
            self.state_path,
            now_iso="2026-01-02T00:00:00+00:00",
        )
        kept = self.read_state()["candidates"]
        active = [e for e in kept.values() if e["status"] != "cancelled_reappeared"]
        cancelled_left = [e for e in kept.values() if e["status"] == "cancelled_reappeared"]
        self.assertEqual(len(active), 1)
        self.assertEqual(kept["node/keep"]["status"], "candidate")
        self.assertEqual(kept["node/keep"]["snapshots_absent"], ["sha-k", "sha-k2"])
        self.assertEqual(len(cancelled_left), 1000)
        self.assertEqual(result["total"], 1)


class TestE1ReportBlocks(unittest.TestCase):
    """Report-level coverage of the new JSON blocks with temp sqlite fixtures."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.staging_db = _make_staging_db(
            self.tmp / "staging.sqlite3",
            places=[
                ("node", 1, "Quan trong vung", 16.0, 106.0, "sha-a", 1),
                ("node", 2, "Outside bbox", 50.0, 0.0, "sha-a", 1),
                ("node", 3, "Da bien mat", 15.0, 108.0, "sha-old", 0),
                ("node", 4, "Bien mat nhung verified", 15.1, 108.1, "sha-old", 0),
            ],
            snapshots=[
                ("snap-limit", "pilot.osm", "sha-x", "pbf:limit=100", "", "2025-12-31T00:00:00+00:00"),
                ("snap-full-1", "vietnam-latest.osm.pbf", "sha-full", "pbf:full:retire=1", "2026-10-09", "2026-01-01T00:00:00+00:00"),
            ],
        )
        self.atlas_db = _make_atlas_db(
            self.tmp / "atlas.sqlite3",
            places=[
                ("p1", "OpenStreetMap", "node/3", "unverified"),
                ("p2", "OpenStreetMap", "node/4", "verified"),
            ],
        )
        self.pbf = self.tmp / "vietnam-latest.osm.pbf"
        self.pbf.write_bytes(b"fake pbf bytes")
        self.state_path = self.tmp / "reports" / "_state" / "retire_candidates.json"

    def build(self, preview=None, **kwargs):
        kwargs.setdefault("pbf_path", self.pbf)
        kwargs.setdefault("staging_db", self.staging_db)
        kwargs.setdefault("atlas_db", self.atlas_db)
        kwargs.setdefault("retire_state_path", self.state_path)
        return update_report.build_report(json.dumps(preview or PREVIEW), PROVENANCE, stamp=STAMP, **kwargs)

    def test_new_blocks_populated(self):
        preview = dict(
            PREVIEW,
            would_restore=3,
            restore_blocked_manual=1,
            changed_in_source=9,
            position_jumps=[{"sid": "node/9"}],
        )
        verify_txt = self.tmp / "verify.txt"
        verify_txt.write_text('noise prefix\n{"invalid_coordinates": 7, "pass": true}\n', encoding="utf-8")
        report, md = self.build(
            preview,
            raw_paths={"verify": verify_txt.as_posix()},
            now="2026-01-01T00:00:00+00:00",
            import_json_text=json.dumps({"status": "imported", "skip_reasons": {"no_name": 5, "bad_coords": 2}}),
            post_apply_text='{"would_insert": 0, "would_update": 0}',
        )
        self.assertEqual(report["stage"], "preview")
        self.assertEqual(report["snapshot"]["sha256"], PROVENANCE["sha256"])
        self.assertEqual(report["snapshot"]["filename"], "vietnam-latest.osm.pbf")
        self.assertEqual(report["snapshot"]["bytes"], len(b"fake pbf bytes"))
        self.assertEqual(report["snapshot"]["source_date"], "2026-10-09")
        self.assertEqual(report["staging"]["latest_snapshot"]["id"], "snap-full-1")
        self.assertEqual(report["staging"]["latest_snapshot"]["file_sha256"], "sha-full")
        self.assertEqual(report["staging"]["full_snapshots"], 1)
        self.assertEqual(report["changes"]["would_restore"], 3)
        self.assertEqual(report["changes"]["restore_blocked_manual"], 1)
        self.assertEqual(report["changes"]["changed_in_source"], 9)
        self.assertEqual(report["quality"]["invalid_coordinates"], 7)
        self.assertEqual(report["quality"]["outside_region"]["count"], 1)
        self.assertEqual(report["quality"]["outside_region"]["samples"][0]["sid"], "node/2")
        self.assertEqual(report["quality"]["skip_reasons"], {"no_name": 5, "bad_coords": 2})
        self.assertEqual(report["quality"]["position_jumps"], [{"sid": "node/9"}])
        self.assertEqual(report["retire_candidates"]["total"], 1)  # node/3 only
        self.assertEqual(report["retire_candidates"]["new_this_run"], 1)
        self.assertEqual(report["retire_candidates"]["protected_missing"], 1)  # node/4 verified
        self.assertEqual(report["retire_candidates"]["cancelled_protected"], 0)
        # Evidence sha = CURRENT full snapshot, never the row's stale last_snapshot.
        self.assertEqual(report["retire_candidates"]["samples"][0]["snapshots_absent"], ["snap-full-1"])
        self.assertEqual(report["post_apply"], {"would_insert": 0, "would_update": 0, "ok": True})
        for section in E1_MD_SECTIONS:
            self.assertIn(section, md)
        self.assertIn("restore_blocked_manual", md)
        self.assertIn("snap-full-1", md)

    def test_stage_apply_and_post_apply_warning(self):
        preview = dict(PREVIEW, mode="APPLY", would_insert=0, would_update=0, would_retire=0)
        report, md = self.build(preview, post_apply_text=json.dumps({"would_insert": 2, "would_update": 0}))
        self.assertEqual(report["stage"], "apply")
        self.assertFalse(report["post_apply"]["ok"])
        self.assertIn("CẢNH BÁO", md)

    def test_optional_inputs_missing_degrade_quietly(self):
        report, md = self.build(
            pbf_path=self.tmp / "nope.pbf",
            staging_db=self.tmp / "nope.sqlite3",
            atlas_db=self.tmp / "nope2.sqlite3",
        )
        self.assertEqual(report["stage"], "preview")
        self.assertIsNone(report["snapshot"]["bytes"])
        self.assertIsNone(report["staging"])
        self.assertIsNone(report["retire_candidates"])
        self.assertIsNone(report["post_apply"])
        self.assertIsNone(report["changes"]["would_restore"])
        self.assertIsNone(report["changes"]["restore_blocked_manual"])
        self.assertIsNone(report["quality"]["invalid_coordinates"])
        for section in E1_MD_SECTIONS:
            self.assertIn(section, md)


if __name__ == "__main__":
    unittest.main()
