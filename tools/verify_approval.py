#!/usr/bin/env python3
"""Fail-closed approval guard before an OSM snapshot write into Vietnam Atlas.

An `-Apply` run must be bound to a preview report a human already reviewed:
the PBF on disk and the latest full staging snapshot must still match what the
approved report recorded. Any drift means the report is stale — refuse to write.

Round E hardening: the four change counters alone cannot bind an apply to the
change set a human approved (two different change sets can share all four
counters). When a pre-apply `--recheck` is supplied, this guard also requires and
compares the change manifests (see tools/change_manifest.py): same
`manifest_hash`, same context (`snapshot_sha`/`schema_version`/`update_existing`/
`apply_lifecycle`). A mismatch names the first differing source_id and fails
closed.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from change_manifest import CONTEXT_KEYS, first_difference, validate_manifest  # noqa: E402

SCHEMA = 1
# Change counters re-checked against the approved report right before apply.
RECHECK_KEYS = ("would_insert", "would_update", "would_retire", "would_restore")
# Tên sidecar manifest recheck mặc định trong cùng thư mục với recheck.json.
RECHECK_MANIFEST_NAME = "change-manifest-recheck.json"


def read_text_any_bom(path) -> str:
    """Read a UTF-8 or UTF-16 (BOM) text file.

    RUN_UPDATE_WINDOWS.ps1 captures the recheck preview via PowerShell 5.1
    Tee-Object, which writes UTF-16LE with BOM; the guard must not read those
    captures as plain UTF-8 (live apply failed on this before the fix).
    """
    # Path đến từ CLI flag của tool chạy cục bộ bởi người vận hành (không phải input từ xa).
    data = Path(path).read_bytes()  # NOSONAR(S8707)
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8")


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
        recheck_text = read_text_any_bom(recheck_path)
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


def default_manifest_path(report_path: Path) -> Path | None:
    """Sidecar manifest của report đã duyệt: `reports/update-<stamp>.json` -> `reports/change-manifest-<stamp>.json`."""
    name = report_path.name
    if not (name.startswith("update-") and name.endswith(".json")):
        return None
    stamp = name[len("update-") : -len(".json")]
    if not stamp:
        return None
    return report_path.with_name(f"change-manifest-{stamp}.json")


def default_recheck_manifest_path(recheck_path: Path) -> Path:
    """Sidecar manifest của lần recheck: cùng thư mục, tên `change-manifest-recheck.json`."""
    return recheck_path.with_name(RECHECK_MANIFEST_NAME)


def load_manifest(path: Path, label: str) -> tuple[dict | None, int]:
    """Load + structurally validate a manifest sidecar; (manifest, 0) or (None, exit code).

    Fail-closed: a missing file, unparseable JSON or any structural problem
    (missing key, wrong schema, tampered entry/manifest hash) is a refusal, never
    a warning.
    """
    try:
        manifest = json.loads(read_text_any_bom(path))
    except (OSError, ValueError) as exc:
        print(f"ERROR: không đọc được manifest {label} {path}: {exc}", file=sys.stderr)
        return None, 1
    problem = validate_manifest(manifest)
    if problem:
        print(f"ERROR: manifest {label} {path} không hợp lệ: {problem}", file=sys.stderr)
        return None, 1
    return manifest, 0


def check_manifest(report, approved_path: Path, recheck_path: Path) -> tuple[int, str | None]:
    """Bind the apply to the exact change set that was approved (not just counters).

    Returns `(exit code, approved manifest_hash)`. Exit 1 = manifest unusable
    (missing/unreadable/invalid), 2 = manifest drift (context or change set),
    0 = the recheck change set re-hashes to the approved fingerprint.
    """
    approved, code = load_manifest(approved_path, "đã duyệt")
    if code:
        return code, None
    current, code = load_manifest(recheck_path, "recheck")
    if code:
        return code, None
    for key in CONTEXT_KEYS:
        if approved.get(key) != current.get(key):
            print(
                f"LECH MANIFEST CONTEXT: {key} recheck {current.get(key)!r} != manifest đã duyệt {approved.get(key)!r} "
                "(snapshot/options/schema Atlas đã đổi kể từ lúc duyệt — chạy lại preview, duyệt lại report rồi mới apply).",
                file=sys.stderr,
            )
            return 2, None
    rep_file_sha = ((report.get("staging") or {}).get("latest_snapshot") or {}).get("file_sha256")
    if rep_file_sha and approved.get("snapshot_sha") != rep_file_sha:
        print(
            f"LECH MANIFEST SNAPSHOT: manifest đã duyệt snapshot_sha={approved.get('snapshot_sha')!r} != "
            f"report.staging.latest_snapshot.file_sha256={rep_file_sha!r}.",
            file=sys.stderr,
        )
        return 2, None
    if approved.get("manifest_hash") == current.get("manifest_hash"):
        return 0, approved.get("manifest_hash")
    diff = first_difference(approved.get("entries") or [], current.get("entries") or [])
    print(
        f"LECH MANIFEST: tập thay đổi khác report đã duyệt (manifest_hash {current.get('manifest_hash')} != "
        f"{approved.get('manifest_hash')}). Khác đầu tiên: {diff or 'không xác định'}. "
        "Chạy lại preview, duyệt lại report rồi mới apply.",
        file=sys.stderr,
    )
    return 2, None


def read_report(path: Path) -> tuple[dict | None, int]:
    """Load the approved report JSON; returns (report, 0) or (None, exit code)."""
    try:
        report = json.loads(read_text_any_bom(path))
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
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest thay đổi đã duyệt; mặc định suy từ --report (reports/change-manifest-<stamp>.json)",
    )
    p.add_argument(
        "--recheck-manifest",
        type=Path,
        default=None,
        help=f"manifest do lần recheck sinh; mặc định <thư mục recheck>/{RECHECK_MANIFEST_NAME}",
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
    # compare its change counters with the approved report (drift fails closed),
    # then re-hash the change manifest so the exact change set must match too.
    manifest_sha = None
    if a.recheck is not None:
        code = check_recheck(report, a.recheck)
        if code:
            return code
        approved_path = a.manifest or default_manifest_path(a.report)
        if approved_path is None:
            print(
                f"ERROR: không suy được manifest đã duyệt từ tên report {a.report.name!r}; truyền --manifest <path> "
                "(report phải tên reports/update-<stamp>.json để khớp reports/change-manifest-<stamp>.json).",
                file=sys.stderr,
            )
            return 1
        recheck_manifest = a.recheck_manifest or default_recheck_manifest_path(a.recheck)
        code, manifest_sha = check_manifest(report, approved_path, recheck_manifest)
        if code:
            return code

    out = {"ok": True, "pbf_sha256": actual_sha, "snapshot": latest}
    if manifest_sha is not None:
        out["manifest_hash"] = manifest_sha
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
