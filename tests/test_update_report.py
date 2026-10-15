"""Tests for tools/update_report.py (T-102 Round D - update-cycle report).

Hermetic: all outputs go to tempfile.TemporaryDirectory; fixtures are fabricated
inline. The module is loaded by path (importlib) so no sys.path mutation and no
E402 (pyproject per-file-ignores only covers test_atlas_data_refresh.py).
"""

import contextlib
import importlib.util
import io
import json
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


if __name__ == "__main__":
    unittest.main()
