#!/usr/bin/env python3
"""Fail-closed approval guard before an OSM snapshot write into Vietnam Atlas.

An `-Apply` run must be bound to a preview report a human already reviewed:
the PBF on disk and the latest full staging snapshot must still match what the
approved report recorded. Any drift means the report is stale — refuse to write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
# Change counters re-checked against the approved report right before apply.
RECHECK_KEYS = ("would_insert", "would_update", "would_retire", "would_restore")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    # Đường dẫn đến từ cờ CLI của chính công cụ cục bộ (người vận hành chạy).
    with path.open("rb") as fh:  # NOSONAR(S8707)
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_full_snapshot(con):
    """Return {id, file_sha256} of the newest FULL-scope ingestion snapshot."""
    cur = con.execute("SELECT id, file_sha256 FROM ingestion_snapshots WHERE mode LIKE '%:full:%' ORDER BY imported_at DESC, rowid DESC LIMIT 1")
    row = cur.fetchone()
    return {"id": row[0], "file_sha256": row[1]} if row else None


def check_recheck(report, recheck_path: Path) -> int:
    """Compare a fresh pre-apply preview with the approved report.

    Returns 0 when the four change counters match, 1 when the recheck file is
    unreadable, and 2 on drift (options or Atlas/staging changed since approval).
    """
    try:
        # Đường dẫn đến từ cờ CLI của chính công cụ cục bộ (người vận hành chạy).
        recheck_text = recheck_path.read_text(encoding="utf-8")  # NOSONAR(S8707)
        recheck = json.loads(recheck_text)
        if not isinstance(recheck, dict):
            raise ValueError("recheck JSON is not an object")
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read recheck preview {recheck_path}: {exc}", file=sys.stderr)
        return 1
    approved_changes = report.get("changes") or {}
    for key in RECHECK_KEYS:
        current = int(recheck.get(key) or 0)
        approved = int(approved_changes.get(key) or 0)
        if current != approved:
            print(
                f"LECH PREVIEW: {key} hiện tại {current} != report đã duyệt {approved} "
                "(options/Atlas/staging đã đổi kể từ lúc duyệt — chạy lại preview, duyệt lại report rồi mới apply).",
                file=sys.stderr,
            )
            return 2
    return 0


def read_report(path: Path) -> tuple[dict | None, int]:
    """Load the approved report JSON; returns (report, 0) or (None, exit code)."""
    try:
        # Đường dẫn đến từ cờ CLI của chính công cụ cục bộ (người vận hành chạy).
        report = json.loads(path.read_text(encoding="utf-8"))  # NOSONAR(S8707)
        if not isinstance(report, dict):
            raise ValueError("report JSON is not an object")
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read approved report {path}: {exc}", file=sys.stderr)
        return None, 1
    return report, 0


def check_report_stage(report) -> int:
    """0 when the report is a preview (the only stage that may be applied)."""
    if report.get("stage") != "preview":
        print(
            f"ERROR: report stage must be 'preview', got {report.get('stage')!r} — only an approved PREVIEW report may be applied.",
            file=sys.stderr,
        )
        return 2
    return 0


def check_pbf(report, pbf: Path) -> tuple[str | None, int]:
    """Verify the current PBF sha256 still matches the approved report."""
    expected = (report.get("snapshot") or {}).get("sha256")
    if not expected:
        print("ERROR: report is missing snapshot.sha256; cannot bind approval to a source file.", file=sys.stderr)
        return None, 1
    if not pbf.is_file():
        print(f"ERROR: PBF not found: {pbf}", file=sys.stderr)
        return None, 1
    actual = sha256_file(pbf)
    if actual != str(expected).lower():
        print(
            f"LECH SHA: current PBF {actual} != report.snapshot.sha256 {expected} (PBF replaced after approval).",
            file=sys.stderr,
        )
        return None, 2
    return actual, 0


def check_staging(report, staging_db: Path) -> tuple[dict | None, int]:
    """Verify the newest FULL staging snapshot still matches the report."""
    if not staging_db.is_file():
        print(f"ERROR: staging db not found: {staging_db}", file=sys.stderr)
        return None, 1
    con = sqlite3.connect(f"file:{staging_db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        latest = latest_full_snapshot(con)
    finally:
        con.close()
    if latest is None:
        print("ERROR: staging db has no FULL-scope snapshot; refusing to bind approval.", file=sys.stderr)
        return None, 2

    rep_latest = (report.get("staging") or {}).get("latest_snapshot") or {}
    if latest["id"] != rep_latest.get("id"):
        print(
            f"LECH SNAPSHOT: latest full staging snapshot id={latest['id']} != report.staging.latest_snapshot.id={rep_latest.get('id')!r}.",
            file=sys.stderr,
        )
        return None, 2
    if latest["file_sha256"] != rep_latest.get("file_sha256"):
        print(
            f"LECH FILE_SHA: latest full staging snapshot file_sha256={latest['file_sha256']} != "
            f"report.staging.latest_snapshot.file_sha256={rep_latest.get('file_sha256')!r}.",
            file=sys.stderr,
        )
        return None, 2
    return latest, 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Verify an approved preview report still matches the PBF and staging snapshot before -Apply.")
    p.add_argument("--report", type=Path, required=True, help="approved reports/update-<stamp>.json (must be a preview report)")
    p.add_argument("--pbf", type=Path, default=ROOT / "data" / "vietnam-latest.osm.pbf")
    p.add_argument("--staging-db", type=Path, default=ROOT / "data" / "osm_vietnam.sqlite3")
    p.add_argument(
        "--recheck",
        type=Path,
        default=None,
        help="fresh dry-run preview JSON captured right before apply; its change counters must equal the approved report's",
    )
    a = p.parse_args(argv)

    report, code = read_report(a.report)
    if code:
        return code
    code = check_report_stage(report)
    if code:
        return code
    actual_sha, code = check_pbf(report, a.pbf)
    if code:
        return code
    latest, code = check_staging(report, a.staging_db)
    if code:
        return code

    # Bind the apply to the APPROVED preview: re-run the current preview and
    # compare its change counters with the approved report (drift fails closed).
    if a.recheck is not None:
        code = check_recheck(report, a.recheck)
        if code:
            return code

    print(json.dumps({"ok": True, "pbf_sha256": actual_sha, "snapshot": latest}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
