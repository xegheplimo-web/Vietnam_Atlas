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


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def run_benchmark(atlas_db: Path, queries: list, limit: int = 10, repeats: int = 3):
    from server.store import connect, list_places

    if not atlas_db.is_file():
        raise FileNotFoundError(atlas_db)
    con = connect(atlas_db)
    per_query = []
    latencies = []
    try:
        for item in queries:
            query = item["query"]
            expected = set(item.get("expected_ids") or [])
            top_ids: list = []
            rank = None
            for _ in range(repeats):
                started = time.perf_counter()
                rows, _count = list_places(con, search=query, limit=limit)
                latencies.append((time.perf_counter() - started) * 1000)
                top_ids = [row["id"] for row in rows if row]
            if expected:
                rank = next((i + 1 for i, pid in enumerate(top_ids) if pid in expected), None)
                hit = rank is not None
                reciprocal = 1.0 / rank if rank else 0.0
                false_positive = None
            else:
                # No-match query: correct behaviour is an empty result set.
                hit = len(top_ids) == 0
                reciprocal = None
                false_positive = len(top_ids)
            per_query.append(
                {
                    "id": item["id"],
                    "kind": item.get("kind", ""),
                    "query": query,
                    "hit@10": hit,
                    "rank": rank if expected else None,
                    "rr": round(reciprocal, 4) if reciprocal is not None else None,
                    "false_positive": false_positive,
                    "top1": top_ids[0] if top_ids else None,
                    "note": item.get("note", ""),
                }
            )
    finally:
        con.close()

    labelled = [r for r in per_query if r["false_positive"] is None]
    no_match = [r for r in per_query if r["false_positive"] is not None]
    hits = [r for r in labelled if r["hit@10"]]
    by_kind = {}
    for kind in sorted({r["kind"] for r in per_query}):
        group = [r for r in labelled if r["kind"] == kind]
        if group:
            by_kind[kind] = round(sum(1 for r in group if r["hit@10"]) / len(group), 4)
    summary = {
        "queries_total": len(per_query),
        "queries_labelled": len(labelled),
        "hit@10": round(len(hits) / len(labelled), 4) if labelled else None,
        "mrr@10": round(sum((r["rr"] or 0.0) for r in labelled) / len(labelled), 4) if labelled else None,
        "hit@10_by_kind": by_kind,
        "no_match_queries": len(no_match),
        "no_match_false_positives": sum(1 for r in no_match if r["false_positive"]),
        "latency_ms_p50": round(percentile(latencies, 0.50), 2),
        "latency_ms_p95": round(percentile(latencies, 0.95), 2),
    }
    return {"summary": summary, "per_query": per_query}


def main():
    p = argparse.ArgumentParser(description="Search-quality benchmark for Vietnam Atlas (fixed labelled query set)")
    p.add_argument("--atlas-db", type=Path, default=Path("data/atlas.sqlite3"))
    p.add_argument("--queries", type=Path, default=Path("benchmarks/search_queries.json"))
    p.add_argument("--limit", type=int, default=10, help="top-N window for Hit@N / MRR@N")
    p.add_argument("--repeats", type=int, default=3, help="repeat each query to sample latency")
    p.add_argument("--json-out", type=Path, default=None, help="write full result JSON to this path")
    a = p.parse_args()
    try:
        queries = json.loads(a.queries.read_text(encoding="utf-8"))["queries"]
        result = run_benchmark(a.atlas_db, queries, a.limit, a.repeats)
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
        if a.json_out:
            a.json_out.parent.mkdir(parents=True, exist_ok=True)
            a.json_out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print("full results:", a.json_out)
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
