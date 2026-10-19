#!/usr/bin/env python3
"""E3a independent search-benchmark harness (card C2 — contract contracts/E3A_CONTRACT.md).

Python stdlib only — no runtime dependencies. Six subcommands:

    validate      schema + semantic checks on queries / qrels / evidence / run / manifest files
    run           execute a query set against one adapter on a READ-ONLY corpus (G8 guard)
    judge-import  normalize a human/LLM judgment JSONL into a qrels JSONL
    score         metrics + primary_outcome from a run file + qrels
    compare       per-group deltas + C5 guardrails between two score reports
    report        human-readable rendering of a score report

Corpus isolation (B1): the frozen master passed via --db is opened read-only
(<uri>?mode=ro, query_only=ON) and is never written. The HTTP server runs against a
WORKING COPY made with the SQLite Online Backup API (sqlite3.Connection.backup) —
never VACUUM INTO (FTS5 external-content tracks places.rowid). SHA-256 of every
master is taken before and after the run; a change fails the run (G8). The harness
only issues GET requests (POST/PUT/DELETE are refused) and only binds 127.0.0.1.

Latency protocol (B2 — constants, not tunable): 10 unscored warm-up requests,
5 repeats per query per adapter, concurrency 1, request timeout 2 s. A timeout is a
run_error and stays inside the gate. `--quick` selects the smoke profile
(0 warm-up, 1 repeat) and marks the report protocol.official=false.

No network or LLM calls exist in `run`/`score`: `run` only talks to the loopback
server it spawned itself; `score`/`judge-import`/`validate`/`compare`/`report` only
read local files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import parse, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "benchmarks" / "e3a" / "schema_v1.json"

# ---------------------------------------------------------------------------
# Frozen protocol constants (GPT B2 — deliberately NOT CLI-tunable).
# --quick swaps (warmup, repeats) for a smoke profile and marks the run unofficial.
# ---------------------------------------------------------------------------
WARMUP_REQUESTS = 10
REPEATS_PER_QUERY = 5
TIMEOUT_S = 2.0
CONCURRENCY = 1
TOP_LIMIT = 10
P95_BUDGET_MS = 200.0
HEALTH_DEADLINE_S = 20.0
# Guardrails (B6): fixed thresholds evaluated by `compare`.
GUARDRAILS = {
    "regression150_lost_hits_max": 0,
    "exact_name_lost_hits_max": 0,
    "dev_positive_false_rejection_max": 0.01,
    "critical_errors_max": 0,
    "http_p95_ms_max_each_adapter": P95_BUDGET_MS,
}

GROUPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
POSITIVE_GROUPS = frozenset(GROUPS) - {"G7"}
ADAPTERS = ("legacy-http", "admin-http", "inprocess")
HTTP_ADAPTERS = ("legacy-http", "admin-http")
JUDGMENT_STATUSES = ("pending", "verified", "rejected", "needs_review")
OUTCOMES = ("hit", "retrieval_failure", "coverage_gap", "correct_no_match", "false_positive", "unjudged", "invalid_query", "run_error")

ERROR_LABEL_GROUPS = {
    "ground_truth": ["evidence_missing", "evidence_stale", "label_conflict", "insufficient_judgments", "ambiguous_intent"],
    "data": [
        "poi_missing_in_db",
        "gazetteer_gap",
        "historical_alias_missing",
        "incorrect_coordinates",
        "incorrect_category_data",
        "stale_poi",
        "duplicate_entity",
    ],
    "admin": ["admin_unresolved", "admin_ambiguous", "admin_wrong", "historical_mapping_wrong", "wrong_province", "wrong_ward"],
    "text_ranking": [
        "normalization_failure",
        "typo_handling_failure",
        "alias_match_failure",
        "category_match_failure",
        "wrong_entity",
        "rank_below_10",
        "candidate_generation_failure",
        "relevance_ranking_failure",
        "false_positive_no_match",
    ],
    "spatial": ["outside_radius", "incorrect_distance", "incorrect_distance_order", "radius_filter_unsupported", "spatial_evidence_uncertain"],
    "runtime": ["api_error", "sqlite_error", "query_timeout", "non_deterministic_results", "latency_budget_exceeded"],
}
ERROR_LABELS = {label for labels in ERROR_LABEL_GROUPS.values() for label in labels}
# Labels the scorer may attach itself (deterministic from run data). Severity here is
# the default; judges can raise it on real judgments — never the other way round.
AUTO_LABELS = {
    "query_timeout": "major",
    "api_error": "major",
    "sqlite_error": "major",
    "non_deterministic_results": "major",
    "rank_below_10": "minor",
    "false_positive_no_match": "minor",
    "outside_radius": "info",
    "incorrect_distance": "info",
    "latency_budget_exceeded": "minor",
}

ALLOWED_HTTP_METHODS = ("GET",)


class BenchmarkError(Exception):
    """Fatal harness error (usage validated separately by argparse)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha(obj) -> str:
    return sha256_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    rows = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            rows.append((lineno, json.loads(line)))
        except json.JSONDecodeError as e:
            raise BenchmarkError(f"{path}:{lineno}: JSON không hợp lệ ({e})") from None
    return rows


def load_queries(path) -> list:
    """Accept {version, queries:[...]} (.json) or a plain JSONL of query objects."""
    path = Path(path)
    if path.suffix == ".jsonl":
        return [row for _ln, row in read_jsonl(path)]
    data = read_json(path)
    if isinstance(data, dict) and isinstance(data.get("queries"), list):
        return data["queries"]
    if isinstance(data, list):
        return data
    raise BenchmarkError(f"{path}: không nhận dạng được file queries (cần {{'queries':[...]}} hoặc JSONL)")


def load_schema() -> dict:
    return read_json(SCHEMA_PATH)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def is_iso8601_tz(value) -> bool:
    """True when `value` parses as ISO-8601 AND carries a timezone offset (B5)."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def normalize_snippet(snippet: str) -> str:
    """Canonical snippet form hashed into snippet_sha256: NFC + collapsed whitespace."""
    return " ".join(unicodedata.normalize("NFC", str(snippet or "")).split())


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Same great-circle formula as server.store._DISTANCE_SQL (unrounded)."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((rlat1 - rlat2) / 2.0) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(math.radians(lon1 - lon2) / 2.0) ** 2
    return 12742.0 * math.asin(min(1.0, math.sqrt(a)))


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validator (stdlib). Supports the subset used by
# schema_v1.json: type, required, properties, items, enum, const, pattern,
# minimum/maximum/exclusiveMinimum, minLength, anyOf, $ref to #/$defs/*.
# `required` lists and `enum` sets in schema_v1.json are the single source of truth.
# ---------------------------------------------------------------------------
def _type_ok(node, t) -> bool:
    return {
        "object": isinstance(node, dict),
        "array": isinstance(node, list),
        "string": isinstance(node, str),
        "integer": type(node) is int,
        "number": type(node) in (int, float),
        "boolean": type(node) is bool,
        "null": node is None,
    }.get(t, True)


def _check(node, schema: dict, path: str, errors: list, root: dict):
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].rsplit("/", 1)[1]]
    if "anyOf" in schema:
        if not any(not _collect(node, sub, path, root) for sub in schema["anyOf"]):
            errors.append(f"{path}: không khớp anyOf")
        return
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(node, one) for one in types):
            errors.append(f"{path}: kiểu sai (cần {'/'.join(types)})")
            return
    if "const" in schema and node != schema["const"]:
        errors.append(f"{path}: phải bằng {schema['const']!r}")
    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path}: giá trị {node!r} không thuộc {schema['enum']}")
    if isinstance(node, dict):
        for req in schema.get("required", []):
            if req not in node:
                errors.append(f"{path}: thiếu trường bắt buộc '{req}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in node:
                _check(node[key], sub, f"{path}.{key}", errors, root)
    if isinstance(node, list) and "items" in schema:
        for i, item in enumerate(node):
            _check(item, schema["items"], f"{path}[{i}]", errors, root)
    if isinstance(node, str):
        if "minLength" in schema and len(node) < schema["minLength"]:
            errors.append(f"{path}: chuỗi ngắn hơn minLength={schema['minLength']}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], node):
            errors.append(f"{path}: không khớp pattern {schema['pattern']}")
    if type(node) in (int, float):
        for key, cmp_ in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b), ("exclusiveMinimum", lambda a, b: a > b)):
            if key in schema and not cmp_(node, schema[key]):
                errors.append(f"{path}: vi phạm {key}={schema[key]}")


def _collect(node, schema: dict, path: str, root: dict) -> list:
    errs: list = []
    _check(node, schema, path, errs, root)
    return errs


def check_schema(node, def_name: str, path: str, errors: list):
    _check(node, {"$ref": f"#/$defs/{def_name}"}, path, errors, load_schema())


# ---------------------------------------------------------------------------
# Semantic validators (rules beyond what the schema file expresses)
# ---------------------------------------------------------------------------
def validate_query_set(queries: list) -> list:
    errors = []
    seen = set()
    for i, q in enumerate(queries):
        p = f"queries[{i}]"
        check_schema(q, "query", p, errors)
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        if qid in seen:
            errors.append(f"{p}.id: trùng '{qid}'")
        seen.add(qid)
        if q.get("group") == "G5" and "near" not in q:
            errors.append(f"{p}: query G5 bắt buộc có 'near' (lat/lon/radius_km/crs/coordinate_source/evidence_id/accuracy_m)")
    return errors


def _validate_error_labels(record: dict, path: str, errors: list):
    for i, lab in enumerate(record.get("error_labels") or []):
        name = lab.get("label") if isinstance(lab, dict) else lab
        if name not in ERROR_LABELS:
            errors.append(f"{path}.error_labels[{i}]: nhãn '{name}' không thuộc 37 nhãn rubric_v1")
        if isinstance(lab, dict):
            check_schema(lab, "error_label", f"{path}.error_labels[{i}]", errors)


def validate_qrels(records, query_ids: set | None) -> list:
    errors = []
    seen = set()
    for lineno, rec in records:
        p = f"qrels:{lineno}"
        if not isinstance(rec, dict):
            errors.append(f"{p}: bản ghi phải là object")
            continue
        kind = "coverage_audit" if "coverage_audit" in rec else "qrel"
        check_schema(rec, kind, p, errors)
        qid = rec.get("query_id")
        if query_ids is not None and isinstance(qid, str) and qid not in query_ids:
            errors.append(f"{p}.query_id: '{qid}' không tồn tại trong queries")
        key = (qid, rec.get("place_id"), kind)
        if kind == "qrel" and key in seen:
            errors.append(f"{p}: trùng judgment ({qid}, {rec.get('place_id')})")
        seen.add(key)
        _validate_error_labels(rec, p, errors)
    return errors


def validate_evidence(records, queries: list) -> list:
    """Contract §5.5 / B5 rules; `queries` is required (foreign key + G5 coords)."""
    errors = []
    qgroup = {q.get("id"): q.get("group") for q in queries if isinstance(q, dict)}
    ids = set()
    for lineno, rec in records:
        p = f"evidence:{lineno}"
        if not isinstance(rec, dict):
            errors.append(f"{p}: bản ghi phải là object")
            continue
        check_schema(rec, "evidence", p, errors)
        eid = rec.get("evidence_id")
        if eid in ids:
            errors.append(f"{p}.evidence_id: trùng '{eid}'")
        ids.add(eid)
        qid = rec.get("query_id")
        if isinstance(qid, str) and qid not in qgroup:
            errors.append(f"{p}.query_id: '{qid}' không tồn tại trong queries")
        if "accessed_at" in rec and not is_iso8601_tz(rec["accessed_at"]):
            errors.append(f"{p}.accessed_at: '{rec['accessed_at']}' không phải ISO-8601 có timezone")
        if isinstance(rec.get("snippet"), str) and isinstance(rec.get("snippet_sha256"), str):
            if sha256_text(normalize_snippet(rec["snippet"])) != rec["snippet_sha256"]:
                errors.append(f"{p}.snippet_sha256: không khớp sha256(snippet đã chuẩn hoá)")
        if qgroup.get(qid) == "G5":
            geo_fields = ("lat", "lon", "crs", "accuracy_m", "coordinate_method")
            present = [f for f in geo_fields if f in rec]
            if 0 < len(present) < len(geo_fields):
                for f in geo_fields:
                    if f not in rec:
                        errors.append(f"{p}: evidence tọa độ của query G5 thiếu '{f}' (contract §5.5)")
        content = rec.get("content_sha256")
        if content is not None and (not isinstance(content, str) or not re.fullmatch(r"[0-9a-f]{64}", content)):
            errors.append(f"{p}.content_sha256: phải là hex sha256 hoặc null (chỉ khi có bytes nguồn)")
    geo_fields = ("lat", "lon", "crs", "accuracy_m", "coordinate_method")
    g5_queries = {qid for qid, g in qgroup.items() if g == "G5"}
    g5_covered = {rec.get("query_id") for _ln, rec in records if isinstance(rec, dict) and all(f in rec for f in geo_fields)}
    for qid in sorted(g5_queries - g5_covered):
        errors.append(f"evidence: query G5 '{qid}' không có evidence tọa độ (lat/lon/crs/accuracy_m/coordinate_method) — contract §5.5")
    return errors


def validate_run_file(data) -> list:
    errors = []
    check_schema(data, "run", "run", errors)
    return errors


def validate_manifest(data) -> list:
    errors = []
    check_schema(data, "manifest", "manifest", errors)
    for banned in ("hit@10", "mrr@10", "ndcg@10", "recall@10", "metrics"):
        if banned in (data or {}):
            errors.append(f"manifest.{banned}: metrics thuộc baseline_summary.json, không nằm trong manifest (contract §7)")
    return errors


# ---------------------------------------------------------------------------
# Corpus isolation + adapters (G8 / B1 / B2)
# ---------------------------------------------------------------------------
def open_readonly(path):
    """Open a frozen master read-only: file:URI?mode=ro + query_only (B1).

    Mirrors server.store.connect (row_factory + unicode_lower) so the in-process
    adapter exercises the exact production code path — but nothing can write.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    con.create_function("unicode_lower", 1, lambda s: s.lower() if s is not None else None)
    return con


def make_working_copy(master, dest, expect_atlas: bool = True) -> Path:
    """Online Backup API copy of `master` into `dest` (B1 — never VACUUM INTO).

    Verifies PRAGMA integrity_check; atlas copies must also hold places + places_fts.
    """
    master = Path(master)
    dest = Path(dest)
    src = sqlite3.connect(master.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect(dest.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BenchmarkError(f"working copy lỗi integrity_check: {dest}")
        if expect_atlas:
            rows = check.execute("SELECT COUNT(*) FROM places").fetchone()[0]
            fts = check.execute("SELECT COUNT(*) FROM places_fts").fetchone() is not None
            if rows < 1 or not fts:
                raise BenchmarkError(f"working copy thiếu dữ liệu/FTS: {dest} (places={rows})")
    except sqlite3.Error as e:
        raise BenchmarkError(f"working copy không đọc được: {dest} ({e})") from None
    finally:
        check.close()
    return dest


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, params: dict | None = None, timeout: float = TIMEOUT_S, method: str = "GET"):
    """GET-only HTTP client. Any other method is refused — the harness never writes.

    Query string is built with urllib.parse.urlencode (UTF-8 percent-encoding); never
    hand-concatenated (git-bash/Windows can otherwise send the wrong codepage while
    still returning HTTP 200).
    """
    if method not in ALLOWED_HTTP_METHODS:
        raise ValueError(f"harness chỉ cho phép {ALLOWED_HTTP_METHODS}; từ chối {method}")
    if params:
        url = url + "?" + parse.urlencode(params)
    started = time.perf_counter()
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body), (time.perf_counter() - started) * 1000.0, None
    except urlerror.HTTPError as e:
        return None, (time.perf_counter() - started) * 1000.0, f"http_{e.code}"
    except TimeoutError:
        return None, TIMEOUT_S * 1000.0, "timeout"
    except urlerror.URLError as e:
        reason = getattr(e, "reason", e)
        err = "timeout" if isinstance(reason, TimeoutError) else f"transport:{reason}"
        return None, (time.perf_counter() - started) * 1000.0, err
    except (OSError, json.JSONDecodeError) as e:
        return None, (time.perf_counter() - started) * 1000.0, f"error:{type(e).__name__}"


def spawn_server(work_db: Path, gaz_db: Path | None):
    """Spawn `python -m server.app` on the working copy, bound to 127.0.0.1 only."""
    port = free_port()
    env = dict(os.environ)
    if gaz_db is not None:
        env["ATLAS_GAZETTEER_DB"] = str(gaz_db)
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.app", "--host", "127.0.0.1", "--port", str(port), "--db", str(work_db)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + HEALTH_DEADLINE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise BenchmarkError(f"server benchmark thoát sớm (code {proc.returncode}): {out.strip()[:500]}")
        data, _ms, err = http_json(base + "/api/health", timeout=1.0)
        if err is None and isinstance(data, dict) and data.get("ok"):
            return proc, base
        time.sleep(0.15)
    proc.terminate()
    raise BenchmarkError(f"server benchmark không sẵn sàng sau {HEALTH_DEADLINE_S:.0f}s")


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _row_brief(item: dict) -> dict:
    """Minimal per-row snapshot stored in the run file (enough for scoring/audit)."""
    out = {k: item.get(k) for k in ("id", "name", "category", "province", "province_code", "lat", "lon", "distance_km", "status") if k in item}
    admin = item.get("admin")
    if isinstance(admin, dict):
        out["admin"] = {
            "code": admin.get("code"),
            "name": admin.get("name"),
            "confidence": admin.get("confidence"),
            "filtered": admin.get("filtered"),
        }
    return out


def make_adapter(name: str, base: str | None, con):
    """Return query_fn(query_str, near_tuple_or_none) -> (rows, err)."""
    if name == "legacy-http":

        def legacy(query, near):
            params = {"search": query, "limit": TOP_LIMIT}
            if near is not None:
                params["near"] = f"{near[0]},{near[1]}"
            data, ms, err = http_json(base + "/api/places", params=params)
            if err is not None:
                return None, err, ms
            return (data or {}).get("items") or [], None, ms

        return legacy
    if name == "admin-http":

        def admin(query, near):
            params = {"search": query, "admin": "1", "limit": TOP_LIMIT}
            if near is not None:
                params["near"] = f"{near[0]},{near[1]}"
            data, ms, err = http_json(base + "/api/places", params=params)
            if err is not None:
                return None, err, ms
            return (data or {}).get("items") or [], None, ms

        return admin
    if name == "inprocess":
        from server.store import list_places

        def inproc(query, near):
            started = time.perf_counter()
            try:
                rows, _count = list_places(con, search=query, limit=TOP_LIMIT, near=near)
            except sqlite3.Error:
                return None, "sqlite_error", (time.perf_counter() - started) * 1000.0
            return rows, None, (time.perf_counter() - started) * 1000.0

        return inproc
    raise BenchmarkError(f"adapter không biết: {name}")


def _near_tuple(query: dict):
    near = query.get("near")
    if isinstance(near, dict) and isinstance(near.get("lat"), (int, float)) and isinstance(near.get("lon"), (int, float)):
        return (float(near["lat"]), float(near["lon"]))
    return None


def run_adapter(adapter_fn, queries: list, warmup: int, repeats: int):
    """Execute one adapter over the query set; returns (results, warmup_ms, stats).

    Warm-up requests are fired first and never scored. Every query then runs
    `repeats` times at concurrency 1; a timeout/error marks the query run_error and
    is kept inside the gate (B2). All repeats are still attempted so
    non-determinism between repeats stays visible to the scorer.
    """
    warmup_ms = []
    if warmup and queries:
        for i in range(warmup):
            q = queries[i % len(queries)]
            _rows, _err, ms = adapter_fn(q["query"], _near_tuple(q))
            warmup_ms.append(round(ms, 3))
    results = []
    lat_all = []
    errors = 0
    for q in queries:
        near = _near_tuple(q)
        latencies = []
        ids_by_repeat = []
        top_rows = []
        first_err = None
        for _ in range(repeats):
            rows, err, ms = adapter_fn(q["query"], near)
            latencies.append(round(ms, 3))
            if err is not None:
                errors += 1
                if first_err is None:
                    first_err = err
                ids_by_repeat.append(None)
                continue
            ids_by_repeat.append([r.get("id") for r in rows])
            top_rows = rows
        results.append(
            {
                "query_id": q["id"],
                "top_ids": ids_by_repeat[-1] if ids_by_repeat else [],
                "top_rows": [_row_brief(r) for r in top_rows][:TOP_LIMIT],
                "latencies_ms": latencies,
                "ids_by_repeat": ids_by_repeat,
                "error": first_err,
            }
        )
        lat_all.extend(latencies)
    stats = {"latency_ms_p50": round(percentile(lat_all, 0.50), 2), "latency_ms_p95": round(percentile(lat_all, 0.95), 2)}
    return results, warmup_ms, {"errors_total": errors, "requests_total": warmup + len(queries) * repeats, **stats}


def environment_fingerprint() -> dict:
    return {
        "os": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }


def cmd_run(a) -> int:
    queries = load_queries(a.queries)
    if not queries:
        raise BenchmarkError("file queries rỗng")
    q_errors = validate_query_set(queries)
    if q_errors:
        for e in q_errors:
            print("query lỗi:", e, file=sys.stderr)
        raise BenchmarkError(f"{len(q_errors)} lỗi trong file queries — sửa trước khi run")
    master = Path(a.db)
    if not master.is_file():
        raise BenchmarkError(f"master DB không tồn tại: {master}")
    gaz_master = Path(a.gazetteer_db) if a.gazetteer_db else None
    if gaz_master is not None and not gaz_master.is_file():
        raise BenchmarkError(f"gazetteer DB không tồn tại: {gaz_master}")

    adapters = [*HTTP_ADAPTERS, "inprocess"] if a.adapter == "all" else [a.adapter]
    warmup, repeats = (0, 1) if a.quick else (WARMUP_REQUESTS, REPEATS_PER_QUERY)
    official = warmup == WARMUP_REQUESTS and repeats == REPEATS_PER_QUERY and TIMEOUT_S == 2.0

    master_sha_before = sha256_file(master)
    gaz_sha_before = sha256_file(gaz_master) if gaz_master else None
    proc = None
    report = {
        "schema": "e3a-run/v1",
        "created_at": utcnow(),
        "git_sha": git_sha(),
        "corpus_path": str(master),
        "corpus_sha256": master_sha_before,
        "gazetteer_path": str(gaz_master) if gaz_master else None,
        "gazetteer_sha256": gaz_sha_before,
        "queries_sha256": sha256_file(a.queries),
        "environment": environment_fingerprint(),
        "protocol": {
            "mode": "official" if official else "smoke",
            "warmup": warmup,
            "repeats": repeats,
            "timeout_s": TIMEOUT_S,
            "concurrency": CONCURRENCY,
            "limit": TOP_LIMIT,
            "official": official,
        },
        "adapters": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="e3a-") as tdir:
            tmp = Path(tdir)
            con = None
            if "inprocess" in adapters:
                con = open_readonly(master)  # master itself — read-only (B1)
                try:
                    con.execute("SELECT COUNT(*) FROM places").fetchone()
                except sqlite3.Error as e:
                    raise BenchmarkError(f"master không phải Atlas DB hợp lệ: {e}") from None
            if any(x in HTTP_ADAPTERS for x in adapters):
                work_db = make_working_copy(master, tmp / "atlas-work.sqlite3")
                gaz_work = make_working_copy(gaz_master, tmp / "gazetteer-work.sqlite3", expect_atlas=False) if gaz_master else None
                proc, base = spawn_server(work_db, gaz_work)
            else:
                base = None
            for name in adapters:
                fn = make_adapter(name, base, con)
                results, warmup_ms, st = run_adapter(fn, queries, warmup, repeats)
                report["adapters"].append({"adapter": name, "results": results, "warmup_ms": warmup_ms, **st})
            if con is not None:
                con.close()
        if a.debug:
            report["debug"] = _debug_echo_check(master, queries, report)
    finally:
        stop_server(proc)

    master_sha_after = sha256_file(master)
    gaz_sha_after = sha256_file(gaz_master) if gaz_master else None
    report["master_sha_before"] = master_sha_before
    report["master_sha_after"] = master_sha_after
    report["gazetteer_sha_before"] = gaz_sha_before
    report["gazetteer_sha_after"] = gaz_sha_after
    report["master_unchanged"] = master_sha_before == master_sha_after and gaz_sha_before == gaz_sha_after
    report["run_sha256"] = canonical_sha(report["adapters"])
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["master_unchanged"]:
        print("G8 FAIL: SHA-256 master thay đổi sau run — hủy kết quả, KHÔNG dùng report này.", file=sys.stderr)
        return 1
    if a.debug and report["debug"].get("mismatches"):
        print(f"DEBUG: {len(report['debug']['mismatches'])} câu có kết quả HTTP != inprocess (xem report.debug)", file=sys.stderr)
        return 1
    print(f"run OK: {', '.join(x['adapter'] for x in report['adapters'])} — {len(queries)} câu — run_sha256 {report['run_sha256'][:16]}…")
    return 0


def _debug_echo_check(master: Path, queries: list, report: dict) -> dict:
    """--debug: re-answer every query in-process and diff top_ids vs legacy-http.

    Catches URL-encoding bugs (the "Ho"→HCM class): if the percent-encoded query the
    server received differed from the UTF-8 string we intended, result lists diverge.
    """
    block = next((b for b in report["adapters"] if b["adapter"] == "legacy-http"), None)
    if block is None:
        return {"skipped": "legacy-http không có trong run"}
    try:
        con = open_readonly(master)
    except sqlite3.Error as e:
        return {"skipped": f"không mở được master read-only: {e}"}
    try:
        from server.store import list_places

        mismatches = []
        by_id = {r["query_id"]: r for r in block["results"]}
        for q in queries:
            got = by_id.get(q["id"])
            if got is None or got["error"]:
                continue
            rows, _n = list_places(con, search=q["query"], limit=TOP_LIMIT, near=_near_tuple(q))
            ref_ids = [r["id"] for r in rows]
            if ref_ids != got["top_ids"]:
                mismatches.append({"query_id": q["id"], "http_top_ids": got["top_ids"], "inprocess_top_ids": ref_ids})
        return {"mismatches": mismatches, "checked": len(queries)}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# judge-import
# ---------------------------------------------------------------------------
def cmd_judge_import(a) -> int:
    queries = load_queries(a.queries) if a.queries else None
    query_ids = {q["id"] for q in queries} if queries else None
    records = read_jsonl(a.judgments)
    errors = validate_qrels(records, query_ids)
    if errors:
        for e in errors:
            print("judge-import:", e, file=sys.stderr)
        return 1
    warnings = 0
    out_lines = []
    for _ln, rec in records:
        # B5/contract §6.1: a 'verified' judgment without evidence pointers is
        # imported but will not be effective at score time (it stays unjudged).
        if rec.get("judgment_status") == "verified" and not rec.get("evidence_ids"):
            warnings += 1
            print(f"judge-import: cảnh báo {rec.get('query_id')} verified thiếu evidence_ids → unjudged khi score", file=sys.stderr)
        out_lines.append(json.dumps(rec, ensure_ascii=False, sort_keys=True))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8", newline="\n")
    print(f"judge-import OK: {len(out_lines)} judgments → {a.out} ({warnings} cảnh báo evidence)")
    return 0


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
def _label(name, severity=None, source="auto"):
    return {"label": name, "severity": severity or AUTO_LABELS.get(name, "info"), "source": source}


def _judged_labels(rec) -> list:
    out = []
    for lab in rec.get("error_labels") or []:
        if isinstance(lab, dict):
            out.append({"label": lab.get("label"), "severity": lab.get("severity", "minor"), "source": "judge"})
        else:
            out.append({"label": lab, "severity": "minor", "source": "judge"})
    return out


def load_qrels(path):
    """Return (grades, coverage, judge_labels, meta).

    grades[qid][pid] = grade — verified judgments WITH evidence_ids only
    (thiếu evidence ⇒ unjudged, contract §6.1). coverage[qid] = audit verdict.
    """
    grades: dict = {}
    coverage: dict = {}
    judge_labels: dict = {}
    counts = {"records": 0, "effective": 0, "unjudged": 0}
    for _ln, rec in read_jsonl(path):
        if not isinstance(rec, dict):
            continue
        counts["records"] += 1
        qid = rec.get("query_id")
        effective = rec.get("judgment_status") == "verified" and bool(rec.get("evidence_ids"))
        if "coverage_audit" in rec:
            if effective:
                coverage[qid] = rec["coverage_audit"]
                counts["effective"] += 1
            else:
                counts["unjudged"] += 1
            continue
        pid, grade = rec.get("place_id"), rec.get("grade")
        if not isinstance(qid, str) or not isinstance(pid, str):
            continue
        judge_labels.setdefault(qid, []).extend(_judged_labels(rec))
        if effective and type(grade) is int:
            grades.setdefault(qid, {})[pid] = max(grades.setdefault(qid, {}).get(pid, 0), grade)
            counts["effective"] += 1
        else:
            counts["unjudged"] += 1
    return grades, coverage, judge_labels, counts


def _dcg(gains) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


_SEV_ORDER = {"info": 0, "minor": 1, "major": 2, "critical": 3}


def _dedupe_labels(row: dict):
    """One entry per label name — keep the highest severity, judge source wins ties."""
    best: dict = {}
    for lab in row["error_labels"]:
        cur = best.get(lab["label"])
        if cur is None or (_SEV_ORDER.get(lab["severity"], 0), lab["source"] == "judge") > (
            _SEV_ORDER.get(cur["severity"], 0),
            cur["source"] == "judge",
        ):
            best[lab["label"]] = lab
    row["error_labels"] = list(best.values())


def score_query(query: dict, res: dict | None, grades: dict, coverage: dict, judge_labels: dict) -> dict:
    row = _score_query_row(query, res, grades, coverage, judge_labels)
    _dedupe_labels(row)
    return row


def _score_query_row(query: dict, res: dict | None, grades: dict, coverage: dict, judge_labels: dict) -> dict:
    """primary_outcome + per-query metrics for one (query, adapter result) pair."""
    qid = query["id"]
    labels = list(judge_labels.get(qid, []))
    row = {
        "query_id": qid,
        "group": query.get("group"),
        "panel": query.get("panel"),
        "regression150": bool(query.get("regression150")),
        "outcome": None,
        "hit": None,
        "first_relevant_rank": None,
        "rr": None,
        "recall@10": None,
        "ndcg@10": None,
        "judged_relevant": 0,
        "top10_unjudged": None,
        "judgment_incomplete": False,
        "latency_ms_mean": None,
        "latency_ms_max": None,
        "error_labels": labels,
        "error": None,
    }
    if not isinstance(query.get("group"), str) or query["group"] not in GROUPS:
        row["outcome"] = "invalid_query"
        return row
    if res is None:
        row["outcome"] = "run_error"
        labels.append(_label("api_error"))
        row["error"] = "missing_result"
        return row
    row["error"] = res.get("error")
    lats = res.get("latencies_ms") or []
    if lats:
        row["latency_ms_mean"] = round(sum(lats) / len(lats), 2)
        row["latency_ms_max"] = round(max(lats), 2)
    if res.get("error"):
        row["outcome"] = "run_error"
        err = res["error"]
        labels.append(_label("query_timeout" if err == "timeout" else "api_error"))
        return row
    ids = [x for x in (res.get("ids_by_repeat") or []) if x is not None]
    if len({json.dumps(x) for x in ids}) > 1:
        labels.append(_label("non_deterministic_results"))
    if any(m > P95_BUDGET_MS for m in lats):
        labels.append(_label("latency_budget_exceeded"))
    top_ids = res.get("top_ids") or []

    if query["group"] == "G7":
        row["outcome"] = "correct_no_match" if not top_ids else "false_positive"
        if top_ids:
            labels.append(_label("false_positive_no_match"))
        return row

    qg = grades.get(qid, {})
    relevant = {pid for pid, g in qg.items() if g >= 2}
    row["judged_relevant"] = len(relevant)
    unjudged_top = [pid for pid in top_ids if pid not in qg]
    row["top10_unjudged"] = len(unjudged_top)
    row["judgment_incomplete"] = bool(unjudged_top)
    hit_rank = next((i + 1 for i, pid in enumerate(top_ids) if qg.get(pid, 0) >= 2), None)

    # G5 spatial auto-labels (WGS84 gốc, không dùng distance_km đã làm tròn).
    near = _near_tuple(query)
    if query["group"] == "G5" and near is not None:
        radius_km = float(query["near"].get("radius_km", 0)) + float(query["near"].get("accuracy_m", 0)) / 1000.0
        for r in res.get("top_rows") or []:
            if isinstance(r.get("lat"), (int, float)) and isinstance(r.get("lon"), (int, float)):
                dist = haversine_km(near[0], near[1], r["lat"], r["lon"])
                if dist > radius_km:
                    labels.append(_label("outside_radius"))
                if isinstance(r.get("distance_km"), (int, float)) and abs(dist - r["distance_km"]) > 0.2:
                    labels.append(_label("incorrect_distance"))

    cov = coverage.get(qid)
    if hit_rank is not None:
        row["outcome"] = "hit"
        row["hit"] = True
        row["first_relevant_rank"] = hit_rank
        row["rr"] = round(1.0 / hit_rank, 4)
        gains = [qg.get(pid, 0) if pid in qg else 0 for pid in top_ids]
        ideal = sorted(qg.values(), reverse=True)
        idcg = _dcg(ideal[:TOP_LIMIT])
        row["ndcg@10"] = round(_dcg(gains) / idcg, 4) if idcg > 0 else None
        row["recall@10"] = round(len(relevant & set(top_ids)) / len(relevant), 4) if relevant else None
        return row
    row["hit"] = False
    if cov == "missing":
        row["outcome"] = "coverage_gap"
        return row
    if unjudged_top or (not top_ids and cov is None and not relevant):
        row["outcome"] = "unjudged"
        return row
    row["outcome"] = "retrieval_failure"
    row["rr"] = 0.0  # judged query without a relevant top-10 still enters MRR as 0
    if relevant:
        labels.append(_label("rank_below_10"))
        ideal = sorted(qg.values(), reverse=True)
        idcg = _dcg(ideal[:TOP_LIMIT])
        row["ndcg@10"] = 0.0 if idcg > 0 else None
        row["recall@10"] = 0.0
    return row


def summarize_adapter(block: dict, queries: list, grades: dict, coverage: dict, judge_labels: dict) -> dict:
    by_id = {r["query_id"]: r for r in block["results"]}
    rows = [score_query(q, by_id.get(q["id"]), grades, coverage, judge_labels) for q in queries]
    positive = [r for r in rows if r["group"] != "G7"]
    scored = [r for r in positive if r["outcome"] in ("hit", "retrieval_failure")]
    g7 = [r for r in rows if r["group"] == "G7"]

    def mean(key, rows_):
        vals = [r[key] for r in rows_ if r[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    groups = {}
    for g in GROUPS:
        sub = [r for r in rows if r["group"] == g]
        if not sub:
            continue
        gscored = [r for r in sub if r["outcome"] in ("hit", "retrieval_failure")]
        groups[g] = {
            "n": len(sub),
            "scored": len(gscored),
            "hit@10": round(sum(1 for r in gscored if r["hit"]) / len(gscored), 4) if gscored else None,
            "mrr@10": mean("rr", gscored),
            "recall@10": mean("recall@10", gscored),
            "ndcg@10": mean("ndcg@10", gscored),
            "outcomes": {o: sum(1 for r in sub if r["outcome"] == o) for o in OUTCOMES if any(r["outcome"] == o for r in sub)},
        }
    panels = {}
    for pname in sorted({r["panel"] for r in rows if r["panel"]}):
        sub = [r for r in rows if r["panel"] == pname]
        pscored = [r for r in sub if r["outcome"] in ("hit", "retrieval_failure")]
        panels[pname] = {
            "n": len(sub),
            "hit@10": round(sum(1 for r in pscored if r["hit"]) / len(pscored), 4) if pscored else None,
            "mrr@10": mean("rr", pscored),
            "outcomes": {o: sum(1 for r in sub if r["outcome"] == o) for o in OUTCOMES if any(r["outcome"] == o for r in sub)},
        }

    audited = [q for q in queries if q.get("group") != "G7" and coverage.get(q["id"]) in ("present", "missing")]
    missing = [q for q in queries if coverage.get(q["id"]) == "missing"]
    coverage_stats = {"positive_total": len(positive), "audited": len(audited), "proven_missing": len(missing)}
    if len(audited) == len(positive) and positive:
        coverage_stats["coverage_gap_rate"] = round(len(missing) / len(positive), 4)
    else:
        # B3: unjudged stays in the denominator — emit provisional only.
        coverage_stats["provisional_coverage_rate"] = round(len(missing) / len(audited), 4) if audited else None

    crit = sum(1 for r in rows if any(lab.get("severity") == "critical" for lab in r["error_labels"]))
    metrics = {
        "queries_total": len(rows),
        "positive_total": len(positive),
        "scored_positive": len(scored),
        "unjudged": sum(1 for r in rows if r["outcome"] == "unjudged"),
        "coverage_gap": sum(1 for r in rows if r["outcome"] == "coverage_gap"),
        "run_error": sum(1 for r in rows if r["outcome"] == "run_error"),
        "invalid_query": sum(1 for r in rows if r["outcome"] == "invalid_query"),
        "judgment_incomplete": sum(1 for r in rows if r["judgment_incomplete"]),
        "hit@10": round(sum(1 for r in scored if r["hit"]) / len(scored), 4) if scored else None,
        "mrr@10": mean("rr", scored),
        "recall@10": mean("recall@10", scored),
        "ndcg@10": mean("ndcg@10", scored),
        "no_match_queries": len(g7),
        "no_match_false_positive": round(sum(1 for r in g7 if r["outcome"] == "false_positive") / len(g7), 4) if g7 else None,
        "critical_errors": crit,
        "latency_ms_p50": block.get("latency_ms_p50"),
        "latency_ms_p95": block.get("latency_ms_p95"),
    }
    return {"adapter": block["adapter"], "metrics": metrics, "groups": groups, "panels": panels, "coverage": coverage_stats, "per_query": rows}


def cmd_score(a) -> int:
    run = read_json(a.run)
    errs = validate_run_file(run)
    if errs:
        for e in errs:
            print("run:", e, file=sys.stderr)
        return 1
    queries = load_queries(a.queries)
    errs = validate_query_set(queries)
    if errs:
        for e in errs:
            print("queries:", e, file=sys.stderr)
        return 1
    grades, coverage, judge_labels, qmeta = load_qrels(a.qrels)
    if a.evidence:
        ev_errs = validate_evidence(read_jsonl(a.evidence), queries)
        if ev_errs:
            for e in ev_errs:
                print("evidence:", e, file=sys.stderr)
            return 1
    out = {
        "schema": "e3a-score/v1",
        "created_at": utcnow(),
        "run_sha256": run["run_sha256"],
        "run_file_sha256": sha256_file(a.run),
        "corpus_sha256": run["corpus_sha256"],
        "gazetteer_sha256": run.get("gazetteer_sha256"),
        "queries_sha256": sha256_file(a.queries),
        "qrels_sha256": sha256_file(a.qrels),
        "protocol": run["protocol"],
        "master_unchanged": run["master_unchanged"],
        "judgments": qmeta,
        "adapters": [summarize_adapter(b, queries, grades, coverage, judge_labels) for b in run["adapters"]],
    }
    text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"score OK → {a.out}")
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# compare (C5 gate)
# ---------------------------------------------------------------------------
def _outcome_map(score: dict, adapter: str) -> dict:
    block = next((b for b in score["adapters"] if b["adapter"] == adapter), None)
    return {r["query_id"]: r for r in block["per_query"]} if block else {}


def compare_reports(base: dict, cand: dict) -> dict:
    """Per-group metric deltas + C5 guardrails. Returns the comparison report."""
    guardrails = []
    violations = []
    if base.get("corpus_sha256") != cand.get("corpus_sha256"):
        violations.append({"guardrail": "corpus_sha256_equal", "detail": "baseline/candidate chạy trên corpus khác nhau"})
    adapters = [
        x
        for x in (*HTTP_ADAPTERS, "inprocess")
        if any(b["adapter"] == x for b in base["adapters"]) and any(b["adapter"] == x for b in cand["adapters"])
    ]
    if not adapters:
        violations.append({"guardrail": "common_adapter", "detail": "không có adapter chung giữa baseline và candidate"})

    delta_groups: dict = {}
    for adapter in adapters:
        bm = {r["query_id"]: r for r in next(b for b in base["adapters"] if b["adapter"] == adapter)["per_query"]}
        cm = _outcome_map(cand, adapter)
        # --- guardrail: lost hits on locked subsets ---
        reg_lost = [qid for qid, r in bm.items() if r.get("regression150") and r["outcome"] == "hit" and cm.get(qid, {}).get("outcome") != "hit"]
        g1_lost = [qid for qid, r in bm.items() if r.get("group") == "G1" and r["outcome"] == "hit" and cm.get(qid, {}).get("outcome") != "hit"]
        pos_hits = [qid for qid, r in bm.items() if r["group"] != "G7" and r["outcome"] == "hit"]
        rejected = [qid for qid in pos_hits if cm.get(qid, {}).get("outcome") == "retrieval_failure"]
        false_rej = round(len(rejected) / len(pos_hits), 4) if pos_hits else 0.0
        bmet = next(b for b in base["adapters"] if b["adapter"] == adapter)["metrics"]
        cmet = next(b for b in cand["adapters"] if b["adapter"] == adapter)["metrics"]
        guardrails.extend(
            [
                {
                    "adapter": adapter,
                    "name": "regression150_lost_hits",
                    "value": len(reg_lost),
                    "max": GUARDRAILS["regression150_lost_hits_max"],
                    "ids": reg_lost,
                },
                {
                    "adapter": adapter,
                    "name": "exact_name_lost_hits",
                    "value": len(g1_lost),
                    "max": GUARDRAILS["exact_name_lost_hits_max"],
                    "ids": g1_lost,
                },
                {
                    "adapter": adapter,
                    "name": "dev_positive_false_rejection",
                    "value": false_rej,
                    "max": GUARDRAILS["dev_positive_false_rejection_max"],
                    "detail": f"{len(rejected)}/{len(pos_hits)} baseline-hit trở thành retrieval_failure",
                    "ids": rejected,
                },
                {"adapter": adapter, "name": "critical_errors", "value": cmet.get("critical_errors", 0), "max": GUARDRAILS["critical_errors_max"]},
            ]
        )
        if adapter in HTTP_ADAPTERS:
            p95 = next(b for b in cand["adapters"] if b["adapter"] == adapter).get("latency_ms_p95") or cmet.get("latency_ms_p95")
            guardrails.append({"adapter": adapter, "name": "http_p95_ms", "value": p95, "max": GUARDRAILS["http_p95_ms_max_each_adapter"]})
        for g, gmet in next(b for b in cand["adapters"] if b["adapter"] == adapter)["groups"].items():
            bgrp = next(b for b in base["adapters"] if b["adapter"] == adapter)["groups"].get(g, {})
            delta_groups.setdefault(adapter, {})[g] = {
                k: round((gmet.get(k) or 0) - (bgrp.get(k) or 0), 4) for k in ("hit@10", "mrr@10", "recall@10", "ndcg@10")
            }
            delta_groups[adapter][g]["hit@10_base"] = bgrp.get("hit@10")
            delta_groups[adapter][g]["hit@10_cand"] = gmet.get("hit@10")
        delta_groups[adapter]["__all__"] = {
            "hit@10_base": bmet.get("hit@10"),
            "hit@10_cand": cmet.get("hit@10"),
            **{
                k + "_delta": round((cmet.get(k) or 0) - (bmet.get(k) or 0), 4)
                for k in ("hit@10", "mrr@10", "recall@10", "ndcg@10")
                if bmet.get(k) is not None or cmet.get(k) is not None
            },
        }

    for g in guardrails:
        ok = g["value"] is not None and g["value"] <= g["max"]
        g["pass"] = ok
        if not ok:
            violations.append({"guardrail": g["name"], "adapter": g["adapter"], "value": g["value"], "max": g["max"], "ids": g.get("ids")})
    return {
        "schema": "e3a-compare/v1",
        "created_at": utcnow(),
        "baseline_run": base.get("run_sha256"),
        "candidate_run": cand.get("run_sha256"),
        "adapters_compared": adapters,
        "delta_by_group": delta_groups,
        "guardrails": guardrails,
        "violations": violations,
        "pass": not violations,
    }


def _fmt_delta(v):
    return "-" if v is None else f"{v:+.4f}"


def cmd_compare(a) -> int:
    base, cand = read_json(a.baseline), read_json(a.candidate)
    rep = compare_reports(base, cand)
    lines = ["# E3a compare — baseline vs candidate", ""]
    lines.append(f"- baseline run: `{str(rep['baseline_run'])[:16]}` · candidate run: `{str(rep['candidate_run'])[:16]}`")
    lines.append(f"- adapters: {', '.join(rep['adapters_compared']) or '(none)'}")
    lines.append("")
    for adapter, groups in rep["delta_by_group"].items():
        lines.append(f"## {adapter}")
        lines.append("")
        lines.append("| group | hit@10 base | hit@10 cand | Δhit | Δmrr | Δrecall | Δndcg |")
        lines.append("|---|---|---|---|---|---|---|")
        for g, d in groups.items():
            lines.append(
                f"| {g} | {d.get('hit@10_base')} | {d.get('hit@10_cand')} | {_fmt_delta(d.get('hit@10') or d.get('hit@10_delta'))} | {_fmt_delta(d.get('mrr@10') or d.get('mrr@10_delta'))} | {_fmt_delta(d.get('recall@10') or d.get('recall@10_delta'))} | {_fmt_delta(d.get('ndcg@10') or d.get('ndcg@10_delta'))} |"
            )
        lines.append("")
    lines.append("## Guardrails (C5)")
    lines.append("")
    lines.append("| adapter | guardrail | value | max | verdict |")
    lines.append("|---|---|---|---|---|")
    for g in rep["guardrails"]:
        lines.append(f"| {g['adapter']} | {g['name']} | {g['value']} | {g['max']} | {'PASS' if g['pass'] else 'FAIL'} |")
    lines.append("")
    lines.append(f"**Verdict: {'PASS' if rep['pass'] else 'FAIL'}**" + (f" — {len(rep['violations'])} violation(s)" if rep["violations"] else ""))
    text = "\n".join(lines)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8", newline="\n")
    print(text)
    return 0 if rep["pass"] else 1


# ---------------------------------------------------------------------------
# report — human-readable score rendering
# ---------------------------------------------------------------------------
def cmd_report(a) -> int:
    score = read_json(a.score)
    lines = ["# E3a score report", ""]
    lines.append(f"- run_sha256 `{score['run_sha256'][:16]}` · corpus `{score['corpus_sha256'][:16]}` · qrels `{score['qrels_sha256'][:16]}`")
    lines.append(
        f"- protocol: {score['protocol']['mode']} (warmup={score['protocol']['warmup']}, repeats={score['protocol']['repeats']}) · master_unchanged={score['master_unchanged']}"
    )
    lines.append("")
    for block in score["adapters"]:
        m = block["metrics"]
        lines.append(f"## {block['adapter']}")
        lines.append("")
        lines.append(f"- hit@10 **{m['hit@10']}** · mrr@10 {m['mrr@10']} · recall@10 {m['recall@10']} · ndcg@10 {m['ndcg@10']}")
        lines.append(
            f"- scored/judged: {m['scored_positive']}/{m['positive_total']} positive · unjudged {m['unjudged']} · coverage_gap {m['coverage_gap']} · run_error {m['run_error']} · incomplete {m['judgment_incomplete']}"
        )
        lines.append(f"- no-match: {m['no_match_false_positive']} FP over {m['no_match_queries']} G7 · critical_errors {m['critical_errors']}")
        lines.append(f"- latency p50 {m['latency_ms_p50']} ms · p95 {m['latency_ms_p95']} ms")
        cov = block["coverage"]
        cgr = cov.get("coverage_gap_rate")
        lines.append(
            f"- coverage: {cov['proven_missing']}/{cov['positive_total']} proven missing (audited {cov['audited']}) → "
            + (f"CGR {cgr}" if cgr is not None else f"provisional {cov.get('provisional_coverage_rate')}")
        )
        lines.append("")
        lines.append("| group | n | scored | hit@10 | mrr@10 | recall@10 | ndcg@10 | outcomes |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for g, gm in block["groups"].items():
            oc = ", ".join(f"{k}:{v}" for k, v in sorted(gm["outcomes"].items()))
            lines.append(f"| {g} | {gm['n']} | {gm['scored']} | {gm['hit@10']} | {gm['mrr@10']} | {gm['recall@10']} | {gm['ndcg@10']} | {oc} |")
        for pname, pm in block["panels"].items():
            lines.append(
                f"| {pname} | {pm['n']} | - | {pm['hit@10']} | {pm['mrr@10']} | - | - | "
                + ", ".join(f"{k}:{v}" for k, v in sorted(pm["outcomes"].items()))
                + " |"
            )
        lines.append("")
    text = "\n".join(lines)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8", newline="\n")
    print(text)
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def cmd_validate(a) -> int:
    if not any((a.queries, a.qrels, a.evidence, a.run, a.manifest)):
        raise BenchmarkError("validate cần ít nhất một trong --queries/--qrels/--evidence/--run/--manifest")
    queries = None
    failures = []
    if a.queries:
        queries = load_queries(a.queries)
        errs = validate_query_set(queries)
        print(f"queries {a.queries}: {'PASS' if not errs else 'FAIL'} ({len(queries)} records)")
        failures += errs
        for e in errs:
            print("  -", e)
    if a.qrels:
        qids = {q["id"] for q in queries} if queries else None
        errs = validate_qrels(read_jsonl(a.qrels), qids)
        print(f"qrels {a.qrels}: {'PASS' if not errs else 'FAIL'}")
        failures += errs
        for e in errs:
            print("  -", e)
    if a.evidence:
        if queries is None:
            failures.append("validate --evidence yêu cầu --queries (kiểm query_id + luật G5)")
        else:
            errs = validate_evidence(read_jsonl(a.evidence), queries)
            print(f"evidence {a.evidence}: {'PASS' if not errs else 'FAIL'}")
            failures += errs
            for e in errs:
                print("  -", e)
    if a.run:
        errs = validate_run_file(read_json(a.run))
        print(f"run {a.run}: {'PASS' if not errs else 'FAIL'}")
        failures += errs
        for e in errs:
            print("  -", e)
    if a.manifest:
        errs = validate_manifest(read_json(a.manifest))
        print(f"manifest {a.manifest}: {'PASS' if not errs else 'FAIL'}")
        failures += errs
        for e in errs:
            print("  -", e)
    print(f"validate: {'PASS' if not failures else f'FAIL — {len(failures)} lỗi'}")
    return 0 if not failures else 1


# ---------------------------------------------------------------------------
def _configure_stdio() -> None:
    """Console Windows mặc định cp1252 ⇒ print tiếng Việt/→ sẽ crash. Ép UTF-8, không bao giờ raise."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv=None) -> int:
    _configure_stdio()
    p = argparse.ArgumentParser(description="E3a benchmark harness — đo lường, không sửa search, không LLM trong đường chạy")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="schema + semantic checks")
    v.add_argument("--queries", type=Path)
    v.add_argument("--qrels", type=Path)
    v.add_argument("--evidence", type=Path)
    v.add_argument("--run", type=Path)
    v.add_argument("--manifest", type=Path)
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="chạy query set trên working copy read-only (G8)")
    r.add_argument("--adapter", choices=[*ADAPTERS, "all"], required=True)
    r.add_argument("--queries", type=Path, required=True)
    r.add_argument("--db", type=Path, required=True, help="master Atlas DB (mở read-only, SHA kiểm trước/sau)")
    r.add_argument("--gazetteer-db", type=Path, default=None, help="master gazetteer (admin-http cần; read-only)")
    r.add_argument("--out", type=Path, default=None, help="ghi run JSON ra file (mặc định stdout)")
    r.add_argument("--quick", action="store_true", help="smoke profile: 0 warmup, 1 repeat — report đánh dấu official=false")
    r.add_argument("--debug", action="store_true", help="đối chiếu top_ids legacy-http với inprocess để bắt lỗi encoding")
    r.set_defaults(fn=cmd_run)

    j = sub.add_parser("judge-import", help="nhập judgments JSONL → qrels JSONL chuẩn")
    j.add_argument("--judgments", type=Path, required=True)
    j.add_argument("--queries", type=Path, default=None)
    j.add_argument("--out", type=Path, required=True)
    j.set_defaults(fn=cmd_judge_import)

    s = sub.add_parser("score", help="metrics + primary_outcome từ run + qrels")
    s.add_argument("--run", type=Path, required=True)
    s.add_argument("--queries", type=Path, required=True)
    s.add_argument("--qrels", type=Path, required=True)
    s.add_argument("--evidence", type=Path, default=None)
    s.add_argument("--out", type=Path, default=None)
    s.set_defaults(fn=cmd_score)

    c = sub.add_parser("compare", help="delta theo nhóm + guardrails C5 giữa 2 score report")
    c.add_argument("--baseline", type=Path, required=True)
    c.add_argument("--candidate", type=Path, required=True)
    c.add_argument("--out", type=Path, default=None)
    c.set_defaults(fn=cmd_compare)

    rep = sub.add_parser("report", help="kết xuất markdown từ score JSON")
    rep.add_argument("--score", type=Path, required=True)
    rep.add_argument("--out", type=Path, default=None)
    rep.set_defaults(fn=cmd_report)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except BenchmarkError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
