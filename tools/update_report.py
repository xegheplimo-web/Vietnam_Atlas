#!/usr/bin/env python3
"""Report wrapper for the OSM update cycle (Round D - Data Freshness & Quality Engine v1).

Reads the JSON object printed by tools/sync_osm_into_atlas.py (dry-run preview or
apply result), tolerating surrounding console noise, and emits:

  reports/update-<stamp>.json  machine report (exact sync-tool count key names)
  reports/update-<stamp>.md     Vietnamese operator report

Stdlib only; Python 3.10+.
"""

from __future__ import annotations

import argparse
import codecs
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
COUNT_KEYS = ("would_insert", "would_update", "would_retire", "protected", "existing")
PREVIEW_MARKERS = ("mode", *COUNT_KEYS)
PROVENANCE_KEYS = ("source_url", "last_modified", "sha256", "size_bytes")
VERDICT_CHANGE = "change"
VERDICT_IDEMPOTENT = "idempotent"
VERDICT_LABELS_VI = {
    VERDICT_CHANGE: "CÓ thay đổi — xem xét trước khi chạy -Apply",
    VERDICT_IDEMPOTENT: "Không thay đổi — chu kỳ lặp idempotent",
}

# E1 (Round E): quality + retirement-candidate plumbing. All optional inputs are
# read with .get() so lanes that land later (would_restore, position_jumps,
# apply_lifecycle, import skip_reasons) stay backward compatible.
CHANGE_KEYS = (
    "would_insert",
    "would_update",
    "would_retire",
    "would_restore",
    "restore_blocked_manual",
    "changed_in_source",
    "protected",
)
MODE_APPLY_PREFIX = "APPLY"
MODE_DRY_RUN = "DRY_RUN"
STAGE_PREVIEW = "preview"
STAGE_APPLY = "apply"
# Vietnam bbox used to spot staging rows that fall outside the country extent.
VN_BBOX = (8.0, 23.6, 102.0, 110.7)  # lat_min, lat_max, lon_min, lon_max
OUTSIDE_REGION_SAMPLE_LIMIT = 10
RETIRE_SAMPLE_LIMIT = 15
RETIRE_READY_DISTINCT_SHAS = 2
RETIRE_READY_MIN_DAYS = 7
RETIRE_CANCELLED_KEEP = 1000
# Both cancellation kinds share the retention cap and the "fresh window" restart rule.
CANCELLED_STATUSES = ("cancelled_reappeared", "cancelled_protected")
RETIRE_STATE_SCHEMA = 1
# Atlas statuses are looked up per staging sid in bounded chunks (spec F: "query
# theo sid, chia chunk") so a national-size inactive set cannot blow up one SQL
# statement's host-parameter limit.
ATLAS_STATUS_CHUNK = 500
ROOT = Path(__file__).resolve().parents[1]
RETIRE_STATE_PATH = ROOT / "reports" / "_state" / "retire_candidates.json"
DEFAULT_PBF = ROOT / "data" / "vietnam-latest.osm.pbf"
DEFAULT_STAGING_DB = ROOT / "data" / "osm_vietnam.sqlite3"
DEFAULT_ATLAS_DB = ROOT / "data" / "atlas.sqlite3"


def read_text_any_bom(path) -> str:
    """Read a UTF-8 or UTF-16 (BOM) text file.

    PowerShell 5.1 Tee-Object writes UTF-16LE with BOM, so the _raw captures it
    produces must not be read as plain UTF-8.
    """
    # Path đến từ CLI flag của tool chạy cục bộ bởi người vận hành (không phải input từ xa).
    data = Path(path).read_bytes()  # NOSONAR(S8707)
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8")


def _string_scan_state(char, escaped):
    """Advance the JSON-string state machine one char: returns (in_string, escaped)."""
    if escaped:
        return True, False
    if char == "\\":
        return True, True
    if char == '"':
        return False, False
    return True, False


def iter_json_object_spans(text):
    """Yield (start, end) char spans of every balanced top-level {...} block.

    A small state machine (not a regex): braces inside JSON strings are ignored,
    so a raw console capture that wraps the sync JSON in other text still yields
    the JSON object cleanly.
    """
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            in_string, escaped = _string_scan_state(char, escaped)
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                yield start, index + 1
                start = None


def extract_preview_dict(preview_text):
    """Return the last top-level JSON object that looks like a sync-tool result.

    A candidate must carry at least one known preview key, so an unrelated
    trailing JSON object cannot silently become a zero-count report.
    """
    for start, end in reversed(list(iter_json_object_spans(preview_text))):
        try:
            candidate = json.loads(preview_text[start:end])
        except ValueError:
            continue
        if isinstance(candidate, dict) and any(marker in candidate for marker in PREVIEW_MARKERS):
            return candidate
    raise ValueError(f"no sync preview JSON object found (expected keys: {', '.join(PREVIEW_MARKERS)})")


def provenance_subset(provenance):
    """Keep only the provenance keys the report needs; tolerant of dict/str/bytes/None."""
    if not provenance:
        return {}
    if isinstance(provenance, (str, bytes, bytearray)):
        try:
            provenance = json.loads(provenance)
        except ValueError:
            return {}
    if not isinstance(provenance, dict):
        return {}
    return {key: provenance[key] for key in PROVENANCE_KEYS if key in provenance}


def _load_json_text(text):
    """Parse a captured JSON document (tolerating console noise around it); None when absent."""
    if not text:
        return None
    for start, end in reversed(list(iter_json_object_spans(text))):
        try:
            candidate = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _read_captured_json(path):
    """Read a tee-captured file (BOM tolerant) as JSON; None when missing/unparseable."""
    if path is None:
        return None
    path = Path(path)  # NOSONAR(S8707)
    if not path.is_file():
        return None
    try:
        return _load_json_text(read_text_any_bom(path))
    except (OSError, UnicodeError):
        return None


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp leniently (naive values assumed UTC); None when absent/unparseable."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_size(size_bytes):
    if size_bytes is None:
        return "—"
    return f"{int(size_bytes)} bytes ({int(size_bytes) / 2**20:.1f} MiB)"


def _outside_region(con):
    """Count active staging rows outside the Vietnam bbox; keep a small sample."""
    lat_min, lat_max, lon_min, lon_max = VN_BBOX
    where = "is_active=1 AND NOT (lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)"
    args = (lat_min, lat_max, lon_min, lon_max)
    count = con.execute(f"SELECT count(*) FROM osm_places WHERE {where}", args).fetchone()[0]
    rows = con.execute(f"SELECT osm_type,osm_id,name,lat,lon FROM osm_places WHERE {where} LIMIT {OUTSIDE_REGION_SAMPLE_LIMIT}", args).fetchall()
    samples = [{"sid": f"{r[0]}/{r[1]}", "name": r[2], "lat": r[3], "lon": r[4]} for r in rows]
    return {"count": count, "samples": samples}


def _atlas_status_by_sid(atlas_db, sids):
    """Return {sid: status} for the given staging sids with source='OpenStreetMap'.

    sid format is "<osm_type>/<osm_id>" — the same key tools/sync_osm_into_atlas.py
    writes into places.source_id. Queried in bounded chunks (spec F: query theo
    sid, chia chunk) instead of one unbounded IN list; a missing or unreadable
    atlas db yields an empty map so the report stays backward compatible.
    """
    status_by_sid = {}
    if not atlas_db.is_file() or not sids:
        return status_by_sid
    con = sqlite3.connect(f"file:{atlas_db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        ordered = sorted(sids)
        for start in range(0, len(ordered), ATLAS_STATUS_CHUNK):
            chunk = ordered[start : start + ATLAS_STATUS_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT source_id,status FROM places WHERE source='OpenStreetMap' AND source_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for source_id, status in rows:
                status_by_sid[source_id] = status
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return status_by_sid


def _load_retire_state(state_path):
    """Load reports/_state/retire_candidates.json; fresh schema-1 state on any problem."""
    fresh = {"schema": RETIRE_STATE_SCHEMA, "updated_at": None, "candidates": {}}
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if not isinstance(state, dict) or state.get("schema") != RETIRE_STATE_SCHEMA or not isinstance(state.get("candidates"), dict):
        return fresh
    return state


def _atomic_write_json(state_path, payload):
    """Write JSON atomically (tmp file + rename in the same directory)."""
    state_path = Path(state_path)  # NOSONAR(S8707)
    state_path.parent.mkdir(parents=True, exist_ok=True)  # NOSONAR(S8707)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")  # NOSONAR(S8707)
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")  # NOSONAR
    tmp.replace(state_path)  # NOSONAR(S8707)


def update_retire_candidates(inactive_rows, atlas_status_by_sid, state_path, now_iso=None):  # NOSONAR(S3776)
    """Merge one run's inactive staging rows into the retire-candidate state.

    inactive_rows: iterable of (sid, name, absent_snapshot_sha) — the sha of the
    full ingestion snapshot the row is missing from (NOT its last-present sha;
    an inactive row keeps its old last_snapshot forever, so that value could
    never accumulate a second distinct absence). Rows whose atlas OSM record is
    not 'unverified', or that have no atlas row, are ineligible: counted in
    protected_missing, and any active candidacy of theirs is closed. Returns the
    report entry {total, new_this_run, ready_for_review, cancelled_reappeared,
    cancelled_protected, protected_missing, samples}; the state file is updated
    atomically.
    """
    now = _parse_iso(now_iso) or datetime.now(timezone.utc)
    state = _load_retire_state(state_path)
    protected_missing = 0
    new_this_run = 0
    cancelled_this_run = 0
    cancelled_protected_this_run = 0
    rows = list(inactive_rows)
    eligible = set()

    for sid, name, sha in rows:
        status = atlas_status_by_sid.get(sid)
        if status is None:
            continue  # no atlas OSM row: not a retire candidate
        if status != "unverified":
            protected_missing += 1
            continue
        eligible.add(sid)
        entry = state["candidates"].get(sid)
        if entry is None or entry.get("status") in CANCELLED_STATUSES:
            # Brand-new candidate, or absent again after a cancellation (fresh window).
            entry = {
                "first_missing_sha": sha,
                "first_missing_at": now.isoformat(),
                "snapshots_absent": [sha] if sha else [],
                "last_absent_at": now.isoformat(),
                "status": "candidate",
                "name": name,
            }
            if state["candidates"].get(sid) is None:
                new_this_run += 1
        else:
            if sha and sha not in entry["snapshots_absent"]:
                entry["snapshots_absent"].append(sha)
            entry["last_absent_at"] = now.isoformat()
        if entry["status"] == "candidate":
            first = _parse_iso(entry["first_missing_at"])
            distinct = len({s for s in entry["snapshots_absent"] if s})
            if distinct >= RETIRE_READY_DISTINCT_SHAS and first is not None and (now - first).days >= RETIRE_READY_MIN_DAYS:
                entry["status"] = "ready_for_review"
        state["candidates"][sid] = entry

    # Active candidacies that this run no longer supports must close:
    # still-inactive-but-ineligible rows (atlas row protected or gone) become
    # cancelled_protected; rows that left the inactive feed (reappeared) become
    # cancelled_reappeared.
    inactive_sids = {sid for sid, _n, _sh in rows}
    for sid, entry in state["candidates"].items():
        if entry.get("status") not in ("candidate", "ready_for_review") or sid in eligible:
            continue
        entry["cancelled_at"] = now.isoformat()
        if sid in inactive_sids:
            entry["status"] = "cancelled_protected"
            cancelled_protected_this_run += 1
        else:
            entry["status"] = "cancelled_reappeared"
            cancelled_this_run += 1

    # Keep all active candidates plus the newest cancelled entries.
    cancelled = [e for e in state["candidates"].values() if e.get("status") in CANCELLED_STATUSES]
    cancelled.sort(key=lambda e: e.get("cancelled_at") or e.get("last_absent_at") or "", reverse=True)
    keep_ids = {id(e) for e in cancelled[:RETIRE_CANCELLED_KEEP]}
    state["candidates"] = {sid: e for sid, e in state["candidates"].items() if e.get("status") not in CANCELLED_STATUSES or id(e) in keep_ids}
    state["updated_at"] = now.isoformat()
    _atomic_write_json(Path(state_path), state)

    active = [e for e in state["candidates"].values() if e.get("status") in ("candidate", "ready_for_review")]
    samples = [
        {"sid": sid, "status": e["status"], "snapshots_absent": e["snapshots_absent"], "name": e.get("name")}
        for sid, e in sorted(state["candidates"].items(), key=lambda kv: kv[1].get("last_absent_at") or "", reverse=True)
        if e.get("status") in ("candidate", "ready_for_review")
    ][:RETIRE_SAMPLE_LIMIT]
    return {
        "total": len(active),
        "new_this_run": new_this_run,
        "ready_for_review": sum(1 for e in active if e.get("status") == "ready_for_review"),
        "cancelled_reappeared": cancelled_this_run,
        "cancelled_protected": cancelled_protected_this_run,
        "protected_missing": protected_missing,
        "samples": samples,
    }


def _md_fmt(value):
    """Format a report value for markdown; None renders as an em dash."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "có" if value else "không"
    return str(value)


def _md_skip_reasons(skip_reasons):
    """Render the import skip_reasons block (dict, list or scalar) as text."""
    if not skip_reasons:
        return "—"
    if isinstance(skip_reasons, dict):
        return "; ".join(f"{key}: {_md_fmt(val)}" for key, val in skip_reasons.items())
    if isinstance(skip_reasons, (list, tuple)):
        return ", ".join(str(item) for item in skip_reasons)
    return str(skip_reasons)


def render_markdown(report, stamp, raw_paths=None):  # NOSONAR(S3776)
    """Render the Vietnamese operator report from the machine report dict."""
    provenance = report.get("provenance") or {}
    raw_paths = raw_paths or {}
    sha256 = provenance.get("sha256") or "—"
    snapshot = report.get("snapshot") or {}
    staging = report.get("staging") or {}
    latest_snapshot = staging.get("latest_snapshot") or {}
    changes = report.get("changes") or {}
    quality = report.get("quality") or {}
    outside = quality.get("outside_region") or {}
    retire = report.get("retire_candidates")
    post_apply = report.get("post_apply")
    stage = report.get("stage")
    lines = [
        f"# Báo cáo cập nhật OSM — {stamp}",
        "",
        f"Thời điểm: `{stamp}` (UTC)",
        "",
        "## Nguồn OSM",
        "",
        f"- Nguồn: `{provenance.get('source_url') or '—'}`",
        f"- Last-Modified: `{provenance.get('last_modified') or '—'}`",
        f"- SHA-256 (12 ký tự đầu): `{sha256[:12]}`",
        f"- Kích thước: {_format_size(provenance.get('size_bytes'))}",
        "",
        "## Kết quả so sánh với Atlas",
        "",
        f"- Mới (would_insert): {report['would_insert']}",
        f"- Cập nhật (would_update): {report['would_update']}",
        f"- Ngừng xuất hiện (would_retire): {report['would_retire']}",
        f"- Được bảo vệ (protected): {report['protected']}",
        f"- Đã có (existing): {report['existing']}",
        "",
        "## Staging/verify",
        "",
        f"- Thống kê staging: `{raw_paths.get('stats') or '—'}`",
        f"- Kiểm chứng snapshot: `{raw_paths.get('verify') or '—'}`",
        "",
        "## Snapshot & ràng buộc phê duyệt",
        "",
        f"- Giai đoạn (stage): {_md_fmt(stage)} (`{report.get('mode') or '—'}`)",
        f"- SHA-256 sidecar: `{_md_fmt(snapshot.get('sha256'))[:12]}`",
        f"- File nguồn: `{snapshot.get('filename') or '—'}` — {_format_size(snapshot.get('bytes'))}",
        f"- Source date: `{snapshot.get('source_date') or '—'}`",
        f"- Snapshot staging mới nhất: `{latest_snapshot.get('id') or '—'}` (mode `{latest_snapshot.get('mode') or '—'}`, "
        f"file_sha256 `{_md_fmt(latest_snapshot.get('file_sha256'))[:12]}`, imported `{latest_snapshot.get('imported_at') or '—'}`)",
        f"- Số snapshot full-scope trong staging: {_md_fmt(staging.get('full_snapshots'))}",
        "- Chỉ báo cáo `stage=preview` mới được phép `-Apply` (tools/verify_approval.py đối chiếu PBF + snapshot trước khi ghi).",
        "",
        "## Chất lượng dữ liệu",
        "",
        f"- Tọa độ không hợp lệ (invalid_coordinates): {_md_fmt(quality.get('invalid_coordinates'))}",
        f"- Dòng active ngoài bbox Việt Nam (outside_region): {_md_fmt(outside.get('count'))}",
    ]
    for sample in outside.get("samples") or []:
        lines.append(f"  - `{sample.get('sid') or '—'}` {sample.get('name') or '—'} ({sample.get('lat')}, {sample.get('lon')})")
    lines += [
        f"- Lý do bị bỏ qua khi import (skip_reasons): {_md_skip_reasons(quality.get('skip_reasons'))}",
        f"- Nhảy tọa độ (position_jumps): {_md_fmt(quality.get('position_jumps'))}",
        "",
        "## Ứng viên retire (staging-only)",
        "",
    ]
    if retire is None:
        lines.append("— Không đọc được staging DB — bỏ qua theo dõi ứng viên retire trong lần chạy này.")
    else:
        lines += [
            f"- Tổng ứng viên đang theo dõi: {retire.get('total', 0)}",
            f"- Mới trong lần chạy này: {retire.get('new_this_run', 0)}",
            f"- Sẵn sàng xem xét (ready_for_review): {retire.get('ready_for_review', 0)}",
            f"- Tái xuất hiện (cancelled_reappeared): {retire.get('cancelled_reappeared', 0)}",
            f"- Đóng do không còn đủ điều kiện (cancelled_protected): {retire.get('cancelled_protected', 0)}",
            f"- Có dòng Atlas không unverified — không retire (protected_missing): {retire.get('protected_missing', 0)}",
        ]
        if not retire.get("total") and not retire.get("cancelled_reappeared"):
            lines.append("- Staging hiện không có dòng inactive nào — hoặc chưa nhập với `--mark-missing`, hoặc không có POI nào biến mất.")
        for sample in retire.get("samples") or []:
            lines.append(
                f"  - `{sample.get('sid') or '—'}` {sample.get('name') or '—'}: {sample.get('status') or '—'}"
                f" (snapshots_absent: {len(sample.get('snapshots_absent') or [])})"
            )
        lines.append(
            "- E1 chỉ ghi nhận ứng viên: cờ `--apply-lifecycle` (thực retire/restore) do lane khác phát triển;"
            " cần ≥2 snapshot khác nhau mất tích + ≥7 ngày để vào ready_for_review."
        )
    lines += [
        "",
        "## POI tái xuất hiện (would_restore)",
        "",
        f"- POI tái xuất hiện trong nguồn (would_restore): {_md_fmt(changes.get('would_restore'))}",
        f"- Bị chặn chờ duyệt thủ công (restore_blocked_manual): {_md_fmt(changes.get('restore_blocked_manual'))}",
        f"- Thay đổi trong nguồn (changed_in_source): {_md_fmt(changes.get('changed_in_source'))}",
        "- Report-only ở E1: `would_restore`/`would_retire` không tính vào cảnh báo kiểm tra sau apply.",
        "",
        "## Kiểm tra sau apply",
        "",
    ]
    if post_apply is None:
        lines.append("— Chưa có (báo cáo preview; chạy lại dry-run sau apply để lấy bằng chứng idempotency).")
    else:
        lines += [
            f"- would_insert còn lại: {post_apply.get('would_insert', 0)}",
            f"- would_update còn lại: {post_apply.get('would_update', 0)}",
        ]
        if post_apply.get("ok"):
            lines.append("- OK: không còn thay đổi ghi được — chu kỳ lặp idempotent.")
        else:
            lines.append("- ⚠ CẢNH BÁO: sau apply vẫn còn thay đổi ghi được — kiểm tra sync trước khi chạy chu kỳ tiếp.")
    lines += [
        "",
        "## Kết luận",
        "",
        f"**{VERDICT_LABELS_VI[report['verdict']]}**",
        "",
        f"- Dữ liệu thô preview: `{raw_paths.get('preview') or '—'}`",
        "",
    ]
    return "\n".join(lines)


def build_report(  # NOSONAR(S3776)
    preview_text,
    provenance=None,
    *,
    stamp="",
    out_dir="reports",
    raw_paths=None,
    now=None,
    import_json_text=None,
    post_apply_text=None,
    pbf_path=None,
    staging_db=None,
    atlas_db=None,
    retire_state_path=None,
):
    """Build (machine report dict, markdown text) from captured pipeline outputs.

    preview_text: raw content of the captured sync preview (wrapper text tolerated).
    provenance: parsed provenance dict or raw JSON text (only a subset is kept).
    raw_paths: optional {"preview": ..., "stats": ..., "verify": ...} raw capture
        file paths referenced in the markdown report. The staging-stats and verify
        captures matter only as raw file paths; their text content is not part of
        the report payload.
    now: optional ISO timestamp for deterministic retire-candidate evaluation.
    import_json_text: optional captured vn_osm_dataset.py import JSON (skip_reasons).
    post_apply_text: optional captured post-apply dry-run JSON (idempotency check).
    pbf_path / staging_db / atlas_db: optional overrides for the E1 snapshot,
    quality and retire-candidate blocks; every new input is optional and a
    missing one degrades quietly (None), keeping backward compatibility.
    retire_state_path: optional override for the retire-candidate state file
        (tests isolate it; CLI runs keep reports/_state/retire_candidates.json).
    """
    preview = extract_preview_dict(preview_text)
    counts = {key: int(preview.get(key) or 0) for key in COUNT_KEYS}
    has_changes = any(counts[key] > 0 for key in ("would_insert", "would_update", "would_retire"))
    verdict = VERDICT_CHANGE if has_changes else VERDICT_IDEMPOTENT
    prov = provenance_subset(provenance)

    # E1: pipeline stage (preview vs apply) derived from the sync tool's mode.
    mode = preview.get("mode") or ""
    if mode == MODE_DRY_RUN:
        stage = STAGE_PREVIEW
    elif mode.startswith(MODE_APPLY_PREFIX):
        stage = STAGE_APPLY
    else:
        stage = None
    changes = {key: preview.get(key) for key in CHANGE_KEYS}

    # E1: snapshot binding block (sidecar metadata + current PBF bytes).
    pbf = Path(pbf_path) if pbf_path else DEFAULT_PBF
    snapshot = {
        "sha256": prov.get("sha256"),
        "source_url": prov.get("source_url"),
        "filename": pbf.name,
        "bytes": pbf.stat().st_size if pbf.is_file() else None,
        "source_date": preview.get("source_date"),
    }

    # E1: staging snapshot pointers (latest + full count), quality metrics and the
    # inactive-staging row feed for retire candidates.
    staging = None
    quality = {"invalid_coordinates": None, "outside_region": None, "skip_reasons": None, "position_jumps": None}
    sdb = Path(staging_db) if staging_db else DEFAULT_STAGING_DB
    # None = staging unreadable (skip retire tracking entirely); a list, possibly
    # empty, = real inactive set so reappeared sids can be cancelled this run.
    inactive_rows = None
    if sdb.is_file():
        try:
            con = sqlite3.connect(f"file:{sdb.resolve().as_posix()}?mode=ro", uri=True)
            try:
                cur = con.execute(
                    "SELECT id,mode,file_sha256,source_date,imported_at FROM ingestion_snapshots ORDER BY imported_at DESC, rowid DESC LIMIT 1"
                )
                row = cur.fetchone()
                latest = {"id": row[0], "mode": row[1], "file_sha256": row[2], "source_date": row[3], "imported_at": row[4]} if row else None
                full = con.execute("SELECT count(*) FROM ingestion_snapshots WHERE mode LIKE '%:full:%'").fetchone()[0]
                staging = {"latest_snapshot": latest, "full_snapshots": full}
                quality["outside_region"] = _outside_region(con)
                # Absence evidence = the CURRENT full snapshot, not the row's stale
                # last_snapshot (the importer only flips is_active on missing rows).
                full_row = con.execute(
                    "SELECT id FROM ingestion_snapshots WHERE mode LIKE '%:full:%' ORDER BY imported_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
                evidence_sha = full_row[0] if full_row else None
                inactive_rows = [
                    (f"{r[0]}/{r[1]}", r[2], evidence_sha) for r in con.execute("SELECT osm_type,osm_id,name FROM osm_places WHERE is_active=0")
                ]
            finally:
                con.close()
        except sqlite3.Error:
            staging = None
            quality["outside_region"] = None
            inactive_rows = None
    if not snapshot.get("source_date"):
        latest_snap = (staging or {}).get("latest_snapshot") or {}
        snapshot["source_date"] = latest_snap.get("source_date") or prov.get("last_modified")

    verify_obj = _read_captured_json((raw_paths or {}).get("verify"))
    if verify_obj is not None:
        quality["invalid_coordinates"] = verify_obj.get("invalid_coordinates")
    import_obj = _load_json_text(import_json_text) if import_json_text is not None else None
    if import_obj is not None:
        quality["skip_reasons"] = import_obj.get("skip_reasons")
    quality["position_jumps"] = preview.get("position_jumps")

    retire_candidates = None
    adb = Path(atlas_db) if atlas_db else DEFAULT_ATLAS_DB
    if inactive_rows is not None:
        try:
            retire_candidates = update_retire_candidates(
                inactive_rows,
                _atlas_status_by_sid(adb, {sid for sid, _name, _sha in inactive_rows}),
                Path(retire_state_path) if retire_state_path else RETIRE_STATE_PATH,
                now_iso=now,
            )
        except (OSError, ValueError):
            retire_candidates = None

    post_apply = None
    if post_apply_text:
        pa = _load_json_text(post_apply_text) or {}
        would_insert = int(pa.get("would_insert") or 0)
        would_update = int(pa.get("would_update") or 0)
        post_apply = {"would_insert": would_insert, "would_update": would_update, "ok": would_insert == 0 and would_update == 0}

    report = {
        "stamp": stamp,
        "mode": mode,
        "stage": stage,
        "verdict": verdict,
        **counts,
        "provenance": prov,
        "snapshot": snapshot,
        "staging": staging,
        "changes": changes,
        "quality": quality,
        "retire_candidates": retire_candidates,
        "post_apply": post_apply,
    }
    if stamp:
        report["artifacts"] = {
            "json": Path(out_dir, f"update-{stamp}.json").as_posix(),
            "markdown": Path(out_dir, f"update-{stamp}.md").as_posix(),
        }
    markdown = render_markdown(report, stamp or "(chưa đặt)", raw_paths)
    return report, markdown


def write_reports(report, markdown, out_dir, stamp):
    """Write reports/update-<stamp>.{json,md}; returns the artifact paths written."""
    # out_dir/stamp đến từ CLI flag của tool chạy cục bộ bởi người vận hành (không phải input từ xa).
    out = Path(out_dir)  # NOSONAR(S8707)
    out.mkdir(parents=True, exist_ok=True)  # NOSONAR(S8707)
    json_path = out / f"update-{stamp}.json"  # NOSONAR(S8707)
    md_path = out / f"update-{stamp}.md"  # NOSONAR(S8707)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")  # NOSONAR(S8707)
    md_path.write_text(markdown, encoding="utf-8")  # NOSONAR(S8707)
    report["artifacts"] = {"json": json_path.as_posix(), "markdown": md_path.as_posix()}
    return report["artifacts"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the OSM update-cycle report (JSON + Vietnamese markdown) from captured pipeline outputs.")
    parser.add_argument(
        "--preview",
        required=True,
        type=Path,
        help="captured sync_osm_into_atlas.py preview JSON (raw console capture; wrapper text tolerated)",
    )
    parser.add_argument("--provenance", type=Path, default=None, help="data/vietnam-latest.osm.pbf.provenance.json")
    parser.add_argument("--staging-stats", type=Path, default=None, help="captured vn_osm_dataset.py --stats output")
    parser.add_argument("--verify", type=Path, default=None, help="captured verify_osm_snapshot.py output")
    parser.add_argument("--staging-db", type=Path, default=DEFAULT_STAGING_DB, help="OSM staging SQLite (default: data/osm_vietnam.sqlite3)")
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF, help="current OSM PBF for the snapshot block (default: data/vietnam-latest.osm.pbf)")
    parser.add_argument(
        "--atlas-db", type=Path, default=DEFAULT_ATLAS_DB, help="Atlas SQLite for retire-candidate status lookup (default: data/atlas.sqlite3)"
    )
    parser.add_argument(
        "--import-json", type=Path, default=None, help="captured vn_osm_dataset.py import JSON (skip_reasons); missing file is skipped quietly"
    )
    parser.add_argument("--post-apply", type=Path, default=None, help="captured post-apply dry-run JSON for the idempotency check")
    parser.add_argument("--now", default=None, help="ISO-8601 timestamp overriding 'now' (deterministic retire-candidate tests)")
    parser.add_argument("--out-dir", type=Path, default=Path("reports"), help="report output directory (default: reports)")
    parser.add_argument("--stamp", default=None, help="report stamp YYYYMMDDTHHMMSSZ (default: current UTC)")
    args = parser.parse_args(argv)

    if not args.preview.is_file():
        print(f"ERROR: preview file not found: {args.preview}", file=sys.stderr)
        raise SystemExit(2)
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not STAMP_RE.match(stamp):
        print(f"ERROR: --stamp must look like YYYYMMDDTHHMMSSZ, got: {stamp}", file=sys.stderr)
        raise SystemExit(2)
    try:
        preview_text = read_text_any_bom(args.preview)
        provenance_text = read_text_any_bom(args.provenance) if args.provenance and args.provenance.is_file() else None
        import_text = read_text_any_bom(args.import_json) if args.import_json and args.import_json.is_file() else None
        post_apply_text = read_text_any_bom(args.post_apply) if args.post_apply and args.post_apply.is_file() else None
    except (OSError, UnicodeError) as exc:
        print(f"ERROR: cannot read captured input: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        report, markdown = build_report(
            preview_text,
            provenance_text,
            stamp=stamp,
            out_dir=args.out_dir,
            now=args.now,
            import_json_text=import_text,
            post_apply_text=post_apply_text,
            pbf_path=args.pbf,
            staging_db=args.staging_db,
            atlas_db=args.atlas_db,
            raw_paths={
                "preview": args.preview.as_posix(),
                "stats": args.staging_stats.as_posix() if args.staging_stats else None,
                "verify": args.verify.as_posix() if args.verify else None,
            },
        )
    except ValueError as exc:
        print(f"ERROR: cannot build report from {args.preview}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    artifacts = write_reports(report, markdown, args.out_dir, stamp)
    print(f"report: {artifacts['json']}")
    print(f"report: {artifacts['markdown']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
