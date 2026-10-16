"""Tests for tools/verify_approval.py (T-E2 Round E - apply approval binding).

Hermetic: all fixtures (report JSON, fake PBF, staging sqlite) live in a
TemporaryDirectory. The module is loaded by path (importlib) like the sibling
test_update_report.py so no sys.path mutation and no E402.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location("verify_approval", REPO_ROOT / "tools" / "verify_approval.py")
verify_approval = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(verify_approval)

_CM_SPEC = importlib.util.spec_from_file_location("change_manifest", REPO_ROOT / "tools" / "change_manifest.py")
change_manifest = importlib.util.module_from_spec(_CM_SPEC)
assert _CM_SPEC.loader is not None
_CM_SPEC.loader.exec_module(change_manifest)

PBF_BYTES = b"pbf content for sha"
SNAPSHOT_ID = "snap-full"
SNAPSHOT_SHA = "sha-full"
MANIFEST_CONTEXT = {
    "snapshot_sha": SNAPSHOT_SHA,
    "schema_version": "ddl-sha256:test",
    "update_existing": False,
    "apply_lifecycle": False,
}


def _make_staging_db(path, snapshots):
    """Minimal staging fixture: only the columns verify_approval.py queries."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE ingestion_snapshots (id TEXT PRIMARY KEY, file_sha256 TEXT NOT NULL, mode TEXT NOT NULL, imported_at TEXT NOT NULL)"
        )
        con.executemany(
            "INSERT INTO ingestion_snapshots(id,file_sha256,mode,imported_at) VALUES(?,?,?,?)",
            snapshots,
        )
        con.commit()
    finally:
        con.close()
    return path


class TestVerifyApproval(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pbf = self.tmp / "vietnam-latest.osm.pbf"
        self.pbf.write_bytes(PBF_BYTES)
        self.sha = hashlib.sha256(PBF_BYTES).hexdigest()
        self.staging = _make_staging_db(
            self.tmp / "staging.sqlite3",
            [
                ("snap-limit", "sha-x", "pbf:limit=10", "2025-12-01T00:00:00+00:00"),
                (SNAPSHOT_ID, SNAPSHOT_SHA, "pbf:full:retire=1", "2026-01-01T00:00:00+00:00"),
            ],
        )
        self.report = self.tmp / "report.json"
        self.write_report(stage="preview")
        self.manifest = self.tmp / "approved-manifest.json"
        # Sidecar recheck mặc định nằm cạnh recheck.json (do _recheck_file sinh ra).
        self.recheck_manifest = self.tmp / verify_approval.RECHECK_MANIFEST_NAME

    def write_report(self, **overrides):
        payload = {
            "stage": "preview",
            "snapshot": {"sha256": self.sha},
            "staging": {"latest_snapshot": {"id": SNAPSHOT_ID, "file_sha256": SNAPSHOT_SHA}},
        }
        payload.update(overrides)
        self.report.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def entry(source_id, action="insert", after=None, before=None):
        """One manifest entry built by the real builder (realistic fixtures)."""
        return change_manifest.make_entry("OpenStreetMap", source_id, action, before, after if after is not None else {"name": "x"})

    def write_manifest(self, path, entries, **header):
        context = dict(MANIFEST_CONTEXT)
        context.update(header)
        path.write_text(json.dumps(change_manifest.build_manifest(entries, **context), ensure_ascii=False), encoding="utf-8")
        return path

    def run_tool(self, report=None, pbf=None, staging=None, recheck=None, manifest=None, recheck_manifest=None):
        argv = [
            "--report",
            str(report or self.report),
            "--pbf",
            str(pbf or self.pbf),
            "--staging-db",
            str(staging or self.staging),
        ]
        if recheck is not None:
            argv += ["--recheck", str(recheck)]
        if manifest is not None:
            argv += ["--manifest", str(manifest)]
        if recheck_manifest is not None:
            argv += ["--recheck-manifest", str(recheck_manifest)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = verify_approval.main(argv)
        return code, stderr.getvalue(), stdout.getvalue()

    def test_all_match_returns_zero(self):
        code, err, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(json.loads(out)["ok"])

    def test_pbf_sha_mismatch_returns_2(self):
        self.pbf.write_bytes(b"tampered after approval")
        code, err, _ = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("sha256", err.lower())

    def test_snapshot_id_mismatch_returns_2(self):
        # A newer full snapshot landed in staging after the report was approved.
        staging = _make_staging_db(
            self.tmp / "staging2.sqlite3",
            [("snap-full-new", "sha-new", "pbf:full:retire=1", "2026-01-02T00:00:00+00:00")],
        )
        code, err, _ = self.run_tool(staging=staging)
        self.assertEqual(code, 2)
        self.assertIn("snap-full-new", err)

    def test_apply_stage_report_rejected(self):
        self.write_report(stage="apply")
        code, err, _ = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("preview", err)

    def test_missing_report_returns_1(self):
        code, _, _ = self.run_tool(report=self.tmp / "nope.json")
        self.assertEqual(code, 1)

    def test_missing_pbf_returns_1(self):
        code, _, _ = self.run_tool(pbf=self.tmp / "nope.pbf")
        self.assertEqual(code, 1)

    def test_no_full_snapshot_returns_2(self):
        staging = _make_staging_db(
            self.tmp / "staging3.sqlite3",
            [("snap-limit", "sha-x", "pbf:limit=10", "2026-01-02T00:00:00+00:00")],
        )
        code, err, _ = self.run_tool(staging=staging)
        self.assertEqual(code, 2)
        self.assertIn("full", err.lower())

    def _recheck_file(self, encoding="utf-8", **counts):
        path = self.tmp / "recheck.json"
        text = json.dumps(counts)
        if encoding == "utf-16":
            # PowerShell 5.1 Tee-Object capture: UTF-16LE with BOM.
            path.write_bytes(text.encode("utf-16"))
        else:
            path.write_text(text, encoding=encoding)
        return path

    def test_recheck_match_returns_zero(self):
        self.write_report(changes={"would_insert": 5, "would_update": 0, "would_retire": 2, "would_restore": 1})
        recheck = self._recheck_file(would_insert=5, would_update=0, would_retire=2, would_restore=1)
        entries = [self.entry("node/5"), self.entry("node/2", "retire", before={"status": "unverified"}, after={"status": "inactive"})]
        self.write_manifest(self.manifest, entries)
        self.write_manifest(self.recheck_manifest, entries)
        code, err, out = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["manifest_hash"], change_manifest.manifest_hash(entries))

    def test_recheck_counter_mismatch_returns_2(self):
        # e.g. preview approved without --update-existing, then apply ran with it.
        self.write_report(changes={"would_insert": 0, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=0, would_update=34, would_retire=0, would_restore=0)
        code, err, _ = self.run_tool(recheck=recheck)
        self.assertEqual(code, 2)
        self.assertIn("would_update", err)

    def test_recheck_missing_file_returns_1(self):
        code, _, _ = self.run_tool(recheck=self.tmp / "nope-recheck.json")
        self.assertEqual(code, 1)

    def test_recheck_absent_keeps_backward_compatible_checks(self):
        code, err, _ = self.run_tool()
        self.assertEqual(code, 0, err)

    def test_recheck_utf16_teeobject_capture_accepted(self):
        # RUN_UPDATE_WINDOWS.ps1 writes recheck.json through Tee-Object (UTF-16LE
        # with BOM); the guard must read that capture, not fail as not-UTF-8.
        self.write_report(changes={"would_insert": 319, "would_update": 0, "would_retire": 60, "would_restore": 0})
        recheck = self._recheck_file(encoding="utf-16", would_insert=319, would_update=0, would_retire=60, would_restore=0)
        entries = [self.entry("node/1")]
        self.write_manifest(self.manifest, entries)
        self.write_manifest(self.recheck_manifest, entries)
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 0, err)

    def test_report_utf16_accepted(self):
        # Same BOM tolerance applies to the approved report file itself.
        payload = {
            "stage": "preview",
            "snapshot": {"sha256": self.sha},
            "staging": {"latest_snapshot": {"id": SNAPSHOT_ID, "file_sha256": SNAPSHOT_SHA}},
        }
        self.report.write_bytes(json.dumps(payload).encode("utf-16"))
        code, err, _ = self.run_tool()
        self.assertEqual(code, 0, err)

    # --- Round E hardening: change-manifest fingerprint -----------------------

    def test_same_counters_different_source_id_rejected(self):
        # Regression theo spec vòng E: hai diff cùng 4 counter nhưng khác source_id
        # phải bị TỪ CHỐI (trước hardening chỉ so counter nên lọt qua gate).
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.manifest, [self.entry("node/1")])
        self.write_manifest(self.recheck_manifest, [self.entry("node/2")])
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 2)
        self.assertIn("LECH MANIFEST", err)
        self.assertIn("node/2", err)

    def test_same_source_id_changed_content_rejected(self):
        self.write_report(changes={"would_insert": 0, "would_update": 1, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=0, would_update=1, would_retire=0, would_restore=0)
        self.write_manifest(self.manifest, [self.entry("node/7", "update", before={"name": "a"}, after={"name": "b"})])
        self.write_manifest(self.recheck_manifest, [self.entry("node/7", "update", before={"name": "a"}, after={"name": "c"})])
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 2)
        self.assertIn("node/7", err)

    def test_manifest_hash_stable_and_order_independent(self):
        first = change_manifest.build_manifest([self.entry("node/2"), self.entry("node/1")], **MANIFEST_CONTEXT)
        second = change_manifest.build_manifest([self.entry("node/1"), self.entry("node/2")], **MANIFEST_CONTEXT)
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        self.assertEqual([e["source_id"] for e in first["entries"]], ["node/1", "node/2"])
        self.assertIsNone(change_manifest.validate_manifest(first))

    def test_manifest_hash_changes_when_one_field_changes(self):
        base = change_manifest.build_manifest([self.entry("node/1", after={"name": "a"})], **MANIFEST_CONTEXT)
        changed = change_manifest.build_manifest([self.entry("node/1", after={"name": "b"})], **MANIFEST_CONTEXT)
        self.assertNotEqual(base["manifest_hash"], changed["manifest_hash"])
        self.assertNotEqual(base["entries"][0]["entry_hash"], changed["entries"][0]["entry_hash"])

    def test_manifest_context_snapshot_sha_mismatch_rejected(self):
        entries = [self.entry("node/1")]
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.manifest, entries, snapshot_sha=SNAPSHOT_SHA)
        self.write_manifest(self.recheck_manifest, entries, snapshot_sha="sha-khac")
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 2)
        self.assertIn("snapshot_sha", err)

    def test_manifest_context_update_existing_mismatch_rejected(self):
        entries = [self.entry("node/1")]
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.manifest, entries, update_existing=False)
        self.write_manifest(self.recheck_manifest, entries, update_existing=True)
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 2)
        self.assertIn("update_existing", err)

    def test_manifest_approved_snapshot_sha_must_match_report_staging(self):
        entries = [self.entry("node/1")]
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        # Hai manifest khớp nhau nhưng không khớp staging sha của report đã duyệt.
        self.write_manifest(self.manifest, entries, snapshot_sha="sha-other")
        self.write_manifest(self.recheck_manifest, entries, snapshot_sha="sha-other")
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 2)
        self.assertIn("MANIFEST SNAPSHOT", err)

    def test_manifest_missing_rejected(self):
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.recheck_manifest, [self.entry("node/1")])
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.tmp / "nope-manifest.json", recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 1)
        self.assertIn("manifest", err)

    def test_recheck_manifest_missing_rejected(self):
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.manifest, [self.entry("node/1")])
        code, _, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.tmp / "nope-recheck-manifest.json")
        self.assertEqual(code, 1)

    def test_manifest_underivable_report_name_rejected(self):
        # report.json không theo mẫu update-<stamp>.json và không truyền --manifest -> từ chối.
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.recheck_manifest, [self.entry("node/1")])
        code, err, _ = self.run_tool(recheck=recheck, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 1)
        self.assertIn("--manifest", err)

    def test_manifest_invalid_json_rejected(self):
        bad = self.tmp / "bad-manifest.json"
        bad.write_text("{not json", encoding="utf-8")
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.recheck_manifest, [self.entry("node/1")])
        code, _, _ = self.run_tool(recheck=recheck, manifest=bad, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 1)

    def test_manifest_tampered_entries_rejected(self):
        # Sửa tay source_id trong manifest đã duyệt mà không cập nhật hash -> không hợp lệ.
        self.write_report(changes={"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0})
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        tampered = change_manifest.build_manifest([self.entry("node/1")], **MANIFEST_CONTEXT)
        tampered["entries"][0]["source_id"] = "node/999"
        self.manifest.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        self.write_manifest(self.recheck_manifest, [self.entry("node/999")])
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 1)
        self.assertIn("entry_hash", err)

    def test_manifest_derived_from_approved_report_path(self):
        # reports/update-<stamp>.json -> reports/change-manifest-<stamp>.json (không cần --manifest).
        reports_dir = self.tmp / "reports"
        reports_dir.mkdir()
        report = reports_dir / "update-20260101T000000Z.json"
        payload = {
            "stage": "preview",
            "snapshot": {"sha256": self.sha},
            "staging": {"latest_snapshot": {"id": SNAPSHOT_ID, "file_sha256": SNAPSHOT_SHA}},
            "changes": {"would_insert": 1, "would_update": 0, "would_retire": 0, "would_restore": 0},
        }
        report.write_text(json.dumps(payload), encoding="utf-8")
        entries = [self.entry("node/1")]
        self.write_manifest(reports_dir / "change-manifest-20260101T000000Z.json", entries)
        recheck = self._recheck_file(would_insert=1, would_update=0, would_retire=0, would_restore=0)
        self.write_manifest(self.recheck_manifest, entries)
        code, err, _ = self.run_tool(report=report, recheck=recheck, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 0, err)

    def test_integration_preview_manifest_pass_then_one_row_off_fails(self):
        # preview -> manifest -> verify PASS trên fixture đồng bộ; lệch 1 dòng -> FAIL.
        self.write_report(changes={"would_insert": 2, "would_update": 1, "would_retire": 1, "would_restore": 0})
        recheck = self._recheck_file(would_insert=2, would_update=1, would_retire=1, would_restore=0)
        approved = [
            self.entry("node/10"),
            self.entry("node/11"),
            self.entry("node/12", "update", before={"name": "a"}, after={"name": "b"}),
            self.entry("node/13", "retire", before={"status": "unverified"}, after={"status": "inactive"}),
        ]
        self.write_manifest(self.manifest, approved)
        self.write_manifest(self.recheck_manifest, approved)
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 0, err)
        drifted = [
            *approved[:-1],
            self.entry("node/14", "retire", before={"status": "unverified"}, after={"status": "inactive"}),
        ]
        self.write_manifest(self.recheck_manifest, drifted)
        code, err, _ = self.run_tool(recheck=recheck, manifest=self.manifest, recheck_manifest=self.recheck_manifest)
        self.assertEqual(code, 2)
        self.assertIn("LECH MANIFEST", err)
        self.assertIn("node/14", err)


if __name__ == "__main__":
    unittest.main()
