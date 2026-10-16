#!/usr/bin/env python3
"""Canonical change-manifest fingerprint for the OSM -> Atlas apply gate.

A preview report records four change counters (`would_insert`, `would_update`,
`would_retire`, `would_restore`). Two *different* change sets can share those
counters (one POI inserted here, another POI inserted there), so the counters
alone cannot bind an apply to the change set a human actually approved.

This module builds the missing fingerprint: one entry per changed row
(`{source, source_id, action, before, after, entry_hash}`), sorted stably by
`(source, source_id, action)`, plus a header that pins the context the change set
was computed in (`snapshot_sha`, `schema_version`, `update_existing`,
`apply_lifecycle`) and a `manifest_hash` over the canonical JSON of the entries.

Canonical JSON = sorted keys, fixed separators, one consistent `ensure_ascii`
policy, so the hash is stable across rebuilds and independent of input order.

`schema_version` choice: the Atlas database never sets `PRAGMA user_version`, so
this lane fingerprints the schema with a hash of its normalized DDL
(`"ddl-sha256:<hex>"`, or `"absent"` when the Atlas database does not exist yet).

Stdlib only; Python 3.10+.
"""

from __future__ import annotations

import hashlib
import json

MANIFEST_SCHEMA = "change-manifest/1"
MANIFEST_SCHEMA_VERSION = 1
ACTIONS = ("insert", "update", "retire", "restore")
# Context the approved change set is bound to: a drift in any of these means the
# approval no longer describes the write that is about to happen.
CONTEXT_KEYS = ("snapshot_sha", "schema_version", "update_existing", "apply_lifecycle")
REQUIRED_HEADER_KEYS = (
    "schema",
    "manifest_schema_version",
    "snapshot_sha",
    "schema_version",
    "update_existing",
    "apply_lifecycle",
    "entry_count",
    "manifest_hash",
)
ENTRY_KEYS = ("source", "source_id", "action", "before", "after", "entry_hash")


def canonical_json(payload) -> str:
    """Deterministic JSON text: sorted keys, fixed separators, one ASCII policy."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    """sha256 hex digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ddl_fingerprint(rows) -> str:
    """`"ddl-sha256:<hex>"` over normalized `(type, name, sql)` sqlite_master rows.

    Returns `"absent"` when there is no schema yet (Atlas database not created),
    which is itself a stable, comparable value.
    """
    if not rows:
        return "absent"
    return "ddl-sha256:" + sha256_text(canonical_json([[row[0], row[1], row[2] or ""] for row in rows]))


def entry_hash(entry: dict) -> str:
    """Canonical hash of one entry's writable before/after state (no self-hash)."""
    payload = {key: entry.get(key) for key in ENTRY_KEYS if key != "entry_hash"}
    return sha256_text(canonical_json(payload))


def make_entry(source, source_id, action, before, after) -> dict:
    """Build one manifest entry with its canonical `entry_hash`."""
    entry = {"source": str(source), "source_id": str(source_id), "action": str(action), "before": before, "after": after}
    entry["entry_hash"] = entry_hash(entry)
    return entry


def sort_entries(entries) -> list:
    """Stable order by `(source, source_id, action)`, so input order never matters."""
    return sorted(entries, key=lambda e: (str(e.get("source", "")), str(e.get("source_id", "")), str(e.get("action", ""))))


def manifest_hash(entries) -> str:
    """sha256 over the canonical JSON of the ordered entry list."""
    return sha256_text(canonical_json(sort_entries(entries)))


def build_manifest(entries, *, snapshot_sha, schema_version, update_existing, apply_lifecycle) -> dict:
    """Assemble the full manifest document (header + sorted entries + manifest_hash)."""
    ordered = sort_entries(entries)
    return {
        "schema": MANIFEST_SCHEMA,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_sha": snapshot_sha,
        "schema_version": schema_version,
        "update_existing": bool(update_existing),
        "apply_lifecycle": bool(apply_lifecycle),
        "entry_count": len(ordered),
        "manifest_hash": manifest_hash(ordered),
        "entries": ordered,
    }


def _validate_entry(entry, index: int) -> str | None:
    """Return an error message when one entry is unusable, else None."""
    if not isinstance(entry, dict):
        return f"entry #{index} không phải object"
    missing = [key for key in ENTRY_KEYS if key not in entry]
    if missing:
        return f"entry #{index} thiếu khóa: {', '.join(missing)}"
    if entry["action"] not in ACTIONS:
        return f"entry #{index} action {entry['action']!r} không hợp lệ"
    if not isinstance(entry["source_id"], str) or not entry["source_id"]:
        return f"entry #{index} source_id không hợp lệ"
    if entry_hash(entry) != entry["entry_hash"]:
        return f"entry #{index} entry_hash không khớp nội dung"
    return None


def validate_manifest(manifest) -> str | None:
    """Return an error message when the manifest is unusable, else None (fail-closed)."""
    if not isinstance(manifest, dict):
        return "manifest JSON không phải object"
    missing = [key for key in REQUIRED_HEADER_KEYS if key not in manifest]
    if missing:
        return f"manifest thiếu khóa header: {', '.join(missing)}"
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return f"manifest schema {manifest.get('schema')!r} != {MANIFEST_SCHEMA!r}"
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        return f"manifest_schema_version {manifest.get('manifest_schema_version')!r} không được hỗ trợ"
    for key in ("snapshot_sha", "schema_version"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            return f"manifest.{key} phải là chuỗi không rỗng"
    for key in ("update_existing", "apply_lifecycle"):
        if not isinstance(manifest.get(key), bool):
            return f"manifest.{key} phải là boolean"
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return "manifest.entries phải là list"
    if manifest.get("entry_count") != len(entries):
        return f"manifest.entry_count {manifest.get('entry_count')!r} != số entry {len(entries)}"
    for index, entry in enumerate(entries):
        problem = _validate_entry(entry, index)
        if problem:
            return problem
    if manifest.get("manifest_hash") != manifest_hash(entries):
        return "manifest.manifest_hash không khớp nội dung entries (file bị sửa?)"
    return None


def first_difference(approved_entries, current_entries) -> str | None:
    """Describe the first entry (sorted order) that differs between two change sets."""
    approved = {(str(e.get("source")), str(e.get("source_id")), str(e.get("action"))): e for e in sort_entries(approved_entries)}
    for entry in sort_entries(current_entries):
        key = (str(entry.get("source")), str(entry.get("source_id")), str(entry.get("action")))
        other = approved.pop(key, None)
        if other is None:
            return f"recheck có thêm {key[2]} {key[0]} {key[1]} không có trong manifest đã duyệt"
        if entry_hash(other) != entry_hash(entry):
            return f"nội dung đổi ở {key[2]} {key[0]} {key[1]}"
    if approved:
        key = min(approved)
        return f"manifest đã duyệt có {key[2]} {key[0]} {key[1]} nhưng recheck không còn"
    return None
