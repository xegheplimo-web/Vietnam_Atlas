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
import sys
from datetime import datetime, timezone
from pathlib import Path

STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
COUNT_KEYS = ("would_insert", "would_update", "would_retire", "protected", "existing")
PROVENANCE_KEYS = ("source_url", "last_modified", "sha256", "size_bytes")

VERDICT_CHANGE = "change"
VERDICT_IDEMPOTENT = "idempotent"
VERDICT_LABELS_VI = {
    VERDICT_CHANGE: "CÓ thay đổi — xem xét trước khi chạy -Apply",
    VERDICT_IDEMPOTENT: "Không thay đổi — chu kỳ lặp idempotent",
}


def read_text_any_bom(path) -> str:
    """Read a UTF-8 or UTF-16 (BOM) text file.

    PowerShell 5.1 Tee-Object writes UTF-16LE with BOM, so the _raw captures it
    produces must not be read as plain UTF-8.
    """
    data = Path(path).read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8")


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
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
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
    """Return the last parseable top-level JSON object inside *preview_text*."""
    spans = list(iter_json_object_spans(preview_text))
    for start, end in reversed(spans):
        try:
            candidate = json.loads(preview_text[start:end])
        except ValueError:
            continue
        if isinstance(candidate, dict):
            return candidate
    raise ValueError("no parseable top-level JSON object found in preview text")


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


def _format_size(size_bytes):
    if size_bytes is None:
        return "—"
    return f"{int(size_bytes)} bytes ({int(size_bytes) / 2**20:.1f} MiB)"


def render_markdown(report, stamp, raw_paths=None):
    """Render the Vietnamese operator report from the machine report dict."""
    provenance = report.get("provenance") or {}
    raw_paths = raw_paths or {}
    sha256 = provenance.get("sha256") or "—"
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
        "## Kết luận",
        "",
        f"**{VERDICT_LABELS_VI[report['verdict']]}**",
        "",
        f"- Dữ liệu thô preview: `{raw_paths.get('preview') or '—'}`",
        "",
    ]
    return "\n".join(lines)


def build_report(preview_text, provenance=None, stats_text=None, verify_text=None, *, stamp="", out_dir="reports", raw_paths=None):
    """Build (machine report dict, markdown text) from captured pipeline outputs.

    preview_text: raw content of the captured sync preview (wrapper text tolerated).
    provenance: parsed provenance dict or raw JSON text (only a subset is kept).
    stats_text / verify_text: captured staging-stats and verify console text
        (kept for pipeline completeness; the markdown references their raw paths).
    raw_paths: optional {"preview": ..., "stats": ..., "verify": ...} raw capture
        file paths referenced under "Staging/verify" and "Kết luận".
    """
    preview = extract_preview_dict(preview_text)
    counts = {key: int(preview.get(key) or 0) for key in COUNT_KEYS}
    has_changes = any(counts[key] > 0 for key in ("would_insert", "would_update", "would_retire"))
    verdict = VERDICT_CHANGE if has_changes else VERDICT_IDEMPOTENT
    prov = provenance_subset(provenance)
    report = {
        "stamp": stamp,
        "mode": preview.get("mode") or "",
        "verdict": verdict,
        **counts,
        "provenance": prov,
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
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"update-{stamp}.json"
    md_path = out / f"update-{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
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
        stats_text = read_text_any_bom(args.staging_stats) if args.staging_stats and args.staging_stats.is_file() else None
        verify_text = read_text_any_bom(args.verify) if args.verify and args.verify.is_file() else None
    except (OSError, UnicodeError) as exc:
        print(f"ERROR: cannot read captured input: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        report, markdown = build_report(
            preview_text,
            provenance_text,
            stats_text,
            verify_text,
            stamp=stamp,
            out_dir=args.out_dir,
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
