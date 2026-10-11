#!/usr/bin/env python3
"""Run a fixed, labelled Vietnamese search-quality benchmark against the Atlas DB.

Measures Hit@10, MRR@10 and p95 latency on a pinned query set so ranking changes
can be compared objectively before/after. Labelled queries live in
benchmarks/search_queries.json and reference real place IDs in the target DB.

No third-party dependencies; read-only access to the Atlas SQLite file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HIT10 = "hit@10"


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def run_one(con, item, limit, repeats):
    """Score a single labelled query; returns (per-query row, latency samples in ms)."""
    from server import geo
    from server.store import list_places

    query = item["query"]
    expected = set(item.get("expected_ids") or [])
    top_ids: list = []
    top_rows: list = []
    latencies = []
    for _ in range(repeats):
        started = time.perf_counter()
        rows, _count = list_places(con, search=query, limit=limit)
        latencies.append((time.perf_counter() - started) * 1000)
        top_rows = rows
        top_ids = [row["id"] for row in rows if row]
    rank = None
    coverage_gap = bool(item.get("coverage_gap"))
    if expected:
        rank = next((i + 1 for i, pid in enumerate(top_ids) if pid in expected), None)
        hit = rank is not None
        reciprocal = 1.0 / rank if rank else 0.0
        recall = len(expected.intersection(top_ids)) / len(expected)
        false_positive = None
    elif coverage_gap:
        # Coverage probe: the DB is not expected to hold matching data; the row is
        # reported separately and never counted in the headline hit-rate.
        hit = len(top_ids) > 0
        reciprocal = None
        recall = None
        false_positive = None
    else:
        # No-match query: correct behaviour is an empty result set.
        hit = len(top_ids) == 0
        reciprocal = None
        recall = None
        false_positive = len(top_ids)
    # Secondary geo metric (never counted in hit@10): does the top-10 contain a
    # POI physically inside the province the query resolved to? Reported honestly
    # for coverage-gap queries that have no labelable expected IDs.
    resolved = geo.resolve_province(query)
    geo_hit = None
    if coverage_gap and resolved is not None:
        geo_hit = any((found := geo.province_at(row["lat"], row["lon"])) is not None and found.code == resolved.code for row in top_rows)
    row = {
        "id": item["id"],
        "kind": item.get("kind", ""),
        "query": query,
        HIT10: hit,
        "rank": rank if expected else None,
        "rr": round(reciprocal, 4) if reciprocal is not None else None,
        "recall@10": round(recall, 4) if recall is not None else None,
        "false_positive": false_positive,
        "coverage_gap": coverage_gap,
        "top1": top_ids[0] if top_ids else None,
        "geo_province": resolved.name if resolved is not None else None,
        "geo_hit": geo_hit,
        "note": item.get("note", ""),
    }
    return row, latencies


def summarize(per_query, latencies):
    coverage = [r for r in per_query if r["coverage_gap"]]
    labelled = [r for r in per_query if r["false_positive"] is None and not r["coverage_gap"]]
    no_match = [r for r in per_query if r["false_positive"] is not None]
    hits = [r for r in labelled if r[HIT10]]
    by_kind = {}
    recall_by_kind = {}
    for kind in sorted({r["kind"] for r in per_query}):
        group = [r for r in labelled if r["kind"] == kind]
        if group:
            by_kind[kind] = round(sum(1 for r in group if r[HIT10]) / len(group), 4)
            recall_by_kind[kind] = round(sum(r["recall@10"] or 0.0 for r in group) / len(group), 4)
    return {
        "queries_total": len(per_query),
        "queries_labelled": len(labelled),
        HIT10: round(len(hits) / len(labelled), 4) if labelled else None,
        "mrr@10": round(sum((r["rr"] or 0.0) for r in labelled) / len(labelled), 4) if labelled else None,
        "recall@10": round(sum(r["recall@10"] or 0.0 for r in labelled) / len(labelled), 4) if labelled else None,
        "hit@10_by_kind": by_kind,
        "recall@10_by_kind": recall_by_kind,
        "coverage_gap_queries": len(coverage),
        "coverage_gap_returning_results": sum(1 for r in coverage if r[HIT10]),
        "coverage_gap_geo_hits": sum(1 for r in coverage if r["geo_hit"]),
        "geo_resolved_queries": sum(1 for r in per_query if r["geo_province"] is not None),
        "no_match_queries": len(no_match),
        "no_match_false_positives": sum(1 for r in no_match if r["false_positive"]),
        "latency_ms_p50": round(percentile(latencies, 0.50), 2),
        "latency_ms_p95": round(percentile(latencies, 0.95), 2),
    }


def run_benchmark(atlas_db: Path, queries: list, limit: int = 10, repeats: int = 3):
    from server.store import connect

    if not atlas_db.is_file():
        raise FileNotFoundError(atlas_db)
    con = connect(atlas_db)
    per_query = []
    latencies = []
    try:
        for item in queries:
            row, samples = run_one(con, item, limit, repeats)
            per_query.append(row)
            latencies.extend(samples)
    finally:
        con.close()
    return {"summary": summarize(per_query, latencies), "per_query": per_query}


def main():
    p = argparse.ArgumentParser(description="Search-quality benchmark for Vietnam Atlas (fixed labelled query set)")
    p.add_argument("--atlas-db", type=Path, default=Path("data/atlas.sqlite3"))
    p.add_argument("--queries", type=Path, default=Path("benchmarks/search_queries.json"))
    p.add_argument("--limit", type=int, default=10, help="top-N window for Hit@N / MRR@N")
    p.add_argument("--repeats", type=int, default=3, help="repeat each query to sample latency")
    p.add_argument("--json-out", type=Path, default=None, help="write full result JSON to this path")
    a = p.parse_args()
    try:
        # Paths below come from this tool's own CLI flags, run locally by the operator.
        queries = json.loads(a.queries.read_text(encoding="utf-8"))["queries"]  # NOSONAR(S8707)
        result = run_benchmark(a.atlas_db, queries, a.limit, a.repeats)  # NOSONAR(S8707)
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
        if a.json_out:
            a.json_out.parent.mkdir(parents=True, exist_ok=True)
            a.json_out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")  # NOSONAR(S8707)
            print("full results:", a.json_out)
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
