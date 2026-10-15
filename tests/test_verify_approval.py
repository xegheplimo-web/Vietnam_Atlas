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

PBF_BYTES = b"pbf content for sha"
SNAPSHOT_ID = "snap-full"
SNAPSHOT_SHA = "sha-full"


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

    def write_report(self, **overrides):
        payload = {
            "stage": "preview",
            "snapshot": {"sha256": self.sha},
            "staging": {"latest_snapshot": {"id": SNAPSHOT_ID, "file_sha256": SNAPSHOT_SHA}},
        }
        payload.update(overrides)
        self.report.write_text(json.dumps(payload), encoding="utf-8")

    def run_tool(self, report=None, pbf=None, staging=None):
        argv = [
            "--report",
            str(report or self.report),
            "--pbf",
            str(pbf or self.pbf),
            "--staging-db",
            str(staging or self.staging),
        ]
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


if __name__ == "__main__":
    unittest.main()
