"""Offline tests for the E3a benchmark harness (tools/benchmark_e3a.py).

All tests run offline against the synthetic fixture DB built by
tests/fixtures/e3a/build_fixture_db.py — no real corpus, no network beyond
loopback, no LLM. Covers: scorer math (hit/mrr/recall/ndcg), the
no-match-vs-coverage-gap distinction, the evidence validator rules (card C2 §5),
the G8 master-SHA guard, the GET-only HTTP guard, and a 1-query smoke run.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e3a"

_spec = importlib.util.spec_from_file_location("benchmark_e3a", ROOT / "tools" / "benchmark_e3a.py")
be3a = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(be3a)

_builder_spec = importlib.util.spec_from_file_location("build_fixture_db", FIXTURES / "build_fixture_db.py")
builder = importlib.util.module_from_spec(_builder_spec)
_builder_spec.loader.exec_module(builder)


def _mk_result(qid, top_ids, latencies=(1.0,), error=None, top_rows=None):
    return {
        "query_id": qid,
        "top_ids": top_ids,
        "top_rows": top_rows if top_rows is not None else [{"id": p} for p in top_ids],
        "latencies_ms": list(latencies),
        "ids_by_repeat": [top_ids for _ in latencies],
        "error": error,
    }


def _mk_block(results, adapter="inprocess", p95=2.0):
    return {
        "adapter": adapter,
        "results": results,
        "warmup_ms": [],
        "latency_ms_p50": 1.0,
        "latency_ms_p95": p95,
        "requests_total": len(results),
        "errors_total": 0,
    }


class ScorerTest(unittest.TestCase):
    """Hand-computed metric values — the scorer must reproduce them exactly."""

    def setUp(self):
        # p1: hit at rank 2 (rr=0.5). p2: judged, no relevant in top10 -> retrieval_failure.
        # p3: hit at rank 1, full recall. n1: G7 returning a result -> false_positive.
        # n2: positive query proven missing in DB -> coverage_gap.
        self.queries = [
            {"id": "p1", "group": "G1", "query": "a"},
            {"id": "p2", "group": "G1", "query": "b"},
            {"id": "p3", "group": "G1", "query": "c"},
            {"id": "n1", "group": "G7", "query": "zzz"},
            {"id": "n2", "group": "G3", "query": "sân golf không tồn tại"},
        ]
        self.grades = {"p1": {"A": 2}, "p2": {"X": 3, "Y": 1}, "p3": {"B": 3, "C": 2}}
        self.coverage = {"p1": "present", "p2": "present", "p3": "present", "n2": "missing"}
        self.block = _mk_block(
            [
                _mk_result("p1", ["Z", "A"]),
                _mk_result("p2", ["Y"]),
                _mk_result("p3", ["B", "C"]),
                _mk_result("n1", ["SOMEWHERE"]),
                _mk_result("n2", []),
            ]
        )
        self.out = be3a.summarize_adapter(self.block, self.queries, self.grades, self.coverage, {})

    def test_outcomes(self):
        by = {r["query_id"]: r for r in self.out["per_query"]}
        self.assertEqual(by["p1"]["outcome"], "hit")
        self.assertEqual(by["p1"]["first_relevant_rank"], 2)
        self.assertEqual(by["p2"]["outcome"], "retrieval_failure")
        self.assertEqual(by["p3"]["outcome"], "hit")
        self.assertEqual(by["n1"]["outcome"], "false_positive")
        self.assertEqual(by["n2"]["outcome"], "coverage_gap")

    def test_hit_mrr_recall_ndcg_exact(self):
        m = self.out["metrics"]
        # scored positive = p1,p2,p3 -> hit 2/3
        self.assertAlmostEqual(m["hit@10"], 2 / 3, places=4)
        # mrr = (1/2 + 0 + 1) / 3
        self.assertAlmostEqual(m["mrr@10"], 0.5, places=4)
        # recall = (|{A}|/1 + 0 + |{B,C}|/2) / 3 = (1 + 0 + 1) / 3
        self.assertAlmostEqual(m["recall@10"], 2 / 3, places=4)
        # p1: DCG=3/log2(3)=1.8928, IDCG=3 -> 0.6309 ; p2: 0 ; p3: (7+3/log2(3))/(7+3/log2(3))=1
        expected = ((3 / 1.584962500721156) / 3 + 0.0 + 1.0) / 3
        self.assertAlmostEqual(m["ndcg@10"], expected, places=4)

    def test_no_match_vs_coverage_gap_not_merged(self):
        m = self.out["metrics"]
        self.assertEqual(m["no_match_queries"], 1)
        self.assertEqual(m["no_match_false_positive"], 1.0)  # n1 returned a result
        self.assertEqual(self.out["coverage"]["coverage_gap_rate"], round(1 / 4, 4))  # n2 / 4 positive
        self.assertEqual(m["coverage_gap"], 1)

    def test_auto_labels(self):
        by = {r["query_id"]: r for r in self.out["per_query"]}
        self.assertIn("rank_below_10", {x["label"] for x in by["p2"]["error_labels"]})
        self.assertIn("false_positive_no_match", {x["label"] for x in by["n1"]["error_labels"]})

    def test_unjudged_never_grade_zero(self):
        # Verified-looking judgment WITHOUT evidence_ids stays unjudged (B5).
        q = [{"id": "u1", "group": "G1", "query": "x"}]
        with tempfile.TemporaryDirectory() as tmp:
            jf = Path(tmp) / "j.jsonl"
            jf.write_text(
                json.dumps({"query_id": "u1", "place_id": "P", "grade": 3, "judged_by": "x", "judgment_status": "verified"}) + "\n",
                encoding="utf-8",
            )
            grades, coverage, _lbl, _meta = be3a.load_qrels(jf)
        self.assertEqual(grades.get("u1"), None)  # not effective -> still unjudged
        block = _mk_block([_mk_result("u1", ["P"])])
        out = be3a.summarize_adapter(block, q, grades, coverage, {})
        row = out["per_query"][0]
        self.assertEqual(row["outcome"], "unjudged")
        self.assertNotEqual(row["outcome"], "retrieval_failure")  # never silently grade 0
        self.assertEqual(out["metrics"]["scored_positive"], 0)

    def test_run_error_and_timeout(self):
        q = [{"id": "e1", "group": "G1", "query": "x"}]
        block = _mk_block([_mk_result("e1", [], latencies=(2000.0,), error="timeout")])
        out = be3a.summarize_adapter(block, q, {}, {}, {})
        row = out["per_query"][0]
        self.assertEqual(row["outcome"], "run_error")
        self.assertIn("query_timeout", {x["label"] for x in row["error_labels"]})

    def test_g5_spatial_labels(self):
        q = [
            {
                "id": "s1",
                "group": "G5",
                "query": "atm x",
                "near": {
                    "lat": 10.0,
                    "lon": 106.0,
                    "radius_km": 1.0,
                    "crs": "EPSG:4326",
                    "coordinate_source": "gps",
                    "evidence_id": "e",
                    "accuracy_m": 50,
                },
            }
        ]
        far = {"id": "P", "lat": 10.5, "lon": 106.5, "distance_km": 0.1}
        block = _mk_block([_mk_result("s1", ["P"], top_rows=[far])])
        out = be3a.summarize_adapter(block, q, {"s1": {"P": 1}}, {"s1": "present"}, {})
        labels = {x["label"] for x in out["per_query"][0]["error_labels"]}
        self.assertIn("outside_radius", labels)
        self.assertIn("incorrect_distance", labels)


class ValidatorTest(unittest.TestCase):
    """Every card-§5 violation must produce a failing validator result."""

    def setUp(self):
        self.queries = be3a.load_queries(FIXTURES / "queries.json")
        self.evidence = be3a.read_jsonl(FIXTURES / "evidence.jsonl")

    def _one(self, **over):
        base = dict(self.evidence[0][1])
        base.update(over)
        return base

    def test_fixture_files_pass(self):
        self.assertEqual(be3a.validate_query_set(self.queries), [])
        qids = {q["id"] for q in self.queries}
        self.assertEqual(be3a.validate_qrels(be3a.read_jsonl(FIXTURES / "qrels.jsonl"), qids), [])
        self.assertEqual(be3a.validate_evidence(self.evidence, self.queries), [])

    def test_missing_required_field(self):
        rec = self._one()
        del rec["publisher"]
        errs = be3a.validate_evidence([(1, rec)], self.queries)
        self.assertTrue(any("publisher" in e for e in errs), errs)

    def test_bad_accessed_at(self):
        for bad in ("hôm qua", "2026-10-19 10:00", "2026-13-99T99:99Z"):
            errs = be3a.validate_evidence([(1, self._one(accessed_at=bad))], self.queries)
            self.assertTrue(any("accessed_at" in e for e in errs), (bad, errs))

    def test_snippet_hash_mismatch(self):
        rec = self._one(snippet="Nội dung đã bị sửa sau khi hash")
        errs = be3a.validate_evidence([(1, rec)], self.queries)
        self.assertTrue(any("snippet_sha256" in e for e in errs), errs)

    def test_g5_missing_coordinates(self):
        # All G5 evidence records without geo fields -> query-level rule fires.
        geoless = [r for r in self.evidence if r[1].get("query_id") != "q006"]
        geoless.append((99, self._one(evidence_id="ev-q006-alt", query_id="q006", snippet="x", snippet_sha256=be3a.sha256_text("x"))))
        errs = be3a.validate_evidence(geoless, self.queries)
        self.assertTrue(any("G5" in e for e in errs), errs)
        # Partial geo fields on a G5 record -> per-record rule fires.
        partial = self._one(evidence_id="ev-q006-partial", query_id="q006", lat=10.77, snippet="x", snippet_sha256=be3a.sha256_text("x"))
        errs = be3a.validate_evidence([*self.evidence, (100, partial)], self.queries)
        self.assertTrue(any("coordinate_method" in e or "accuracy_m" in e for e in errs), errs)

    def test_unknown_query_id(self):
        rec = self._one(evidence_id="ev-ghost", query_id="q999")
        errs = be3a.validate_evidence([(1, rec)], self.queries)
        self.assertTrue(any("không tồn tại" in e for e in errs), errs)

    def test_qrel_bad_grade_and_label(self):
        qids = {q["id"] for q in self.queries}
        bad_grade = {"query_id": "q001", "place_id": "p", "grade": 9, "judged_by": "x", "judgment_status": "verified"}
        self.assertTrue(be3a.validate_qrels([(1, bad_grade)], qids))
        bad_label = {
            "query_id": "q001",
            "place_id": "p",
            "grade": 1,
            "judged_by": "x",
            "judgment_status": "verified",
            "error_labels": ["not_a_label"],
        }
        self.assertTrue(be3a.validate_qrels([(1, bad_label)], qids))
        ghost = {"query_id": "q999", "place_id": "p", "grade": 1, "judged_by": "x", "judgment_status": "pending"}
        self.assertTrue(be3a.validate_qrels([(1, ghost)], qids))

    def test_query_set_rules(self):
        bad = [{"id": "x", "group": "G5", "query": "atm"}]  # G5 without near
        self.assertTrue(any("near" in e for e in be3a.validate_query_set(bad)))
        dup = [{"id": "x", "group": "G1", "query": "a"}, {"id": "x", "group": "G1", "query": "b"}]
        self.assertTrue(any("trùng" in e for e in be3a.validate_query_set(dup)))


class GuardTest(unittest.TestCase):
    """G8: a changed master SHA must fail the run; HTTP is GET-only."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.db = builder.build_atlas(tmp / "atlas_fixture.sqlite3")
        cls.gaz = builder.build_gazetteer(tmp / "gazetteer_fixture.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_http_get_only(self):
        with self.assertRaises(ValueError):
            be3a.http_json("http://127.0.0.1:1/api/places", method="POST")
        with self.assertRaises(ValueError):
            be3a.http_json("http://127.0.0.1:1/api/places", method="DELETE")

    def test_master_sha_change_fails_run(self):
        out = Path(self.tmp.name) / "run_tampered.json"
        real = be3a.sha256_file
        calls = []

        def spy(path):
            calls.append(str(path))
            if str(path).endswith("atlas_fixture.sqlite3") and calls.count(str(path)) > 1:
                return "0" * 64  # simulate a master changed mid-run
            return real(path)

        with patch.object(be3a, "sha256_file", side_effect=spy):
            code = be3a.main(
                ["run", "--adapter", "inprocess", "--queries", str(FIXTURES / "queries.json"), "--db", str(self.db), "--quick", "--out", str(out)]
            )
        self.assertEqual(code, 1)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(report["master_unchanged"])

    def test_smoke_run_inprocess(self):
        out = Path(self.tmp.name) / "run_smoke.json"
        code = be3a.main(
            ["run", "--adapter", "inprocess", "--queries", str(FIXTURES / "queries.json"), "--db", str(self.db), "--quick", "--out", str(out)]
        )
        self.assertEqual(code, 0)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(be3a.validate_run_file(report), [])
        self.assertTrue(report["master_unchanged"])
        self.assertEqual(report["run_sha256"], be3a.canonical_sha(report["adapters"]))
        self.assertFalse(report["protocol"]["official"])
        block = report["adapters"][0]
        self.assertEqual(block["adapter"], "inprocess")
        self.assertIsNotNone(block["latency_ms_p50"])
        self.assertIsNotNone(block["latency_ms_p95"])

    def test_smoke_run_legacy_http_spawns_server(self):
        out = Path(self.tmp.name) / "run_http.json"
        code = be3a.main(
            [
                "run",
                "--adapter",
                "legacy-http",
                "--queries",
                str(FIXTURES / "queries.json"),
                "--db",
                str(self.db),
                "--gazetteer-db",
                str(self.gaz),
                "--quick",
                "--debug",
                "--out",
                str(out),
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(report["master_unchanged"])
        self.assertEqual(report["debug"]["mismatches"], [])
        q1 = next(r for r in report["adapters"][0]["results"] if r["query_id"] == "q001")
        self.assertEqual(q1["top_ids"][0], "fix-hn-bv-01")

    def test_readonly_master_refuses_writes(self):
        con = be3a.open_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO places(id,name,lat,lon,created_at,updated_at) VALUES('x','y',1,1,'t','t')")
        finally:
            con.close()


class CompareTest(unittest.TestCase):
    def _score(self, outcomes, adapter="legacy-http", p95=10.0, crit=0):
        rows = []
        for qid, (group, outcome, reg) in outcomes.items():
            rows.append(
                {
                    "query_id": qid,
                    "group": group,
                    "panel": None,
                    "regression150": reg,
                    "outcome": outcome,
                    "hit": outcome == "hit",
                    "error_labels": [],
                }
            )
        metrics = {"hit@10": 1.0, "mrr@10": 1.0, "recall@10": 1.0, "ndcg@10": 1.0, "critical_errors": crit}
        block = {
            "adapter": adapter,
            "metrics": metrics,
            "per_query": rows,
            "latency_ms_p50": 1.0,
            "latency_ms_p95": p95,
            "groups": {"G1": {"n": 1, "hit@10": 1.0}},
            "panels": {},
            "coverage": {},
        }
        return {"schema": "e3a-score/v1", "run_sha256": "x", "corpus_sha256": "same", "adapters": [block]}

    def test_compare_identical_passes(self):
        base = self._score({"q1": ("G1", "hit", True), "q2": ("G3", "hit", False)})
        rep = be3a.compare_reports(base, self._score({"q1": ("G1", "hit", True), "q2": ("G3", "hit", False)}))
        self.assertTrue(rep["pass"], rep["violations"])

    def test_regression150_lost_hit_fails(self):
        base = self._score({"q1": ("G1", "hit", True), "q2": ("G3", "hit", False)})
        cand = self._score({"q1": ("G1", "retrieval_failure", True), "q2": ("G3", "hit", False)})
        rep = be3a.compare_reports(base, cand)
        self.assertFalse(rep["pass"])
        names = {v["guardrail"] for v in rep["violations"]}
        self.assertIn("regression150_lost_hits", names)
        self.assertIn("exact_name_lost_hits", names)

    def test_p95_and_critical_guards(self):
        base = self._score({"q1": ("G1", "hit", True)})
        cand = self._score({"q1": ("G1", "hit", True)}, p95=250.0)
        rep = be3a.compare_reports(base, cand)
        self.assertFalse(rep["pass"])
        self.assertIn("http_p95_ms", {v["guardrail"] for v in rep["violations"]})
        cand2 = self._score({"q1": ("G1", "hit", True)}, crit=1)
        self.assertFalse(be3a.compare_reports(base, cand2)["pass"])


class JudgeImportTest(unittest.TestCase):
    def test_import_and_reject(self):
        with tempfile.TemporaryDirectory() as tdir:
            tmp = Path(tdir)
            good = tmp / "good.jsonl"
            good.write_text(
                json.dumps(
                    {
                        "query_id": "q001",
                        "place_id": "fix-hn-bv-01",
                        "grade": 3,
                        "judged_by": "h",
                        "judgment_status": "verified",
                        "evidence_ids": ["e1"],
                    }
                )
                + "\n"
                + json.dumps(
                    {"query_id": "q001", "coverage_audit": "present", "judged_by": "h", "judgment_status": "verified", "evidence_ids": ["e1"]}
                )
                + "\n",
                encoding="utf-8",
            )
            out = tmp / "qrels.jsonl"
            code = be3a.main(["judge-import", "--judgments", str(good), "--queries", str(FIXTURES / "queries.json"), "--out", str(out)])
            self.assertEqual(code, 0)
            lines = out.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

            bad = tmp / "bad.jsonl"
            bad.write_text(
                json.dumps({"query_id": "q001", "place_id": "x", "grade": 7, "judged_by": "h", "judgment_status": "verified"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(be3a.main(["judge-import", "--judgments", str(bad), "--queries", str(FIXTURES / "queries.json"), "--out", str(out)]), 1)


class CliEncodingTest(unittest.TestCase):
    """C2 hotfix: Windows consoles default to cp1252 — the CLI must force UTF-8 stdio.

    Subprocess-level (not in-process main()) so PYTHONIOENCODING applies exactly as
    on the windows-latest CI runner.
    """

    @staticmethod
    def _run_cp1252(args):
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "benchmark_e3a.py"), *args],
            check=False,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_cli_survives_cp1252_console(self):
        with tempfile.TemporaryDirectory() as tdir:
            # Fixture paths are pure ASCII, so validate's PASS output would still
            # encode on cp1252. Run via a non-ASCII dir so the printed path is
            # unencodable — otherwise this test could not catch a reverted fix.
            uni = Path(tdir) / "đường_dẫn"
            uni.mkdir()
            queries = uni / "queries.json"
            qrels = uni / "qrels.jsonl"
            queries.write_bytes((FIXTURES / "queries.json").read_bytes())
            qrels.write_bytes((FIXTURES / "qrels.jsonl").read_bytes())
            proc = self._run_cp1252(["validate", "--queries", str(queries), "--qrels", str(qrels)])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_judge_import_cp1252(self):
        with tempfile.TemporaryDirectory() as tdir:
            tmp = Path(tdir)
            good = tmp / "good.jsonl"
            good.write_text(
                json.dumps(
                    {
                        "query_id": "q001",
                        "place_id": "fix-hn-bv-01",
                        "grade": 3,
                        "judged_by": "h",
                        "judgment_status": "verified",
                        "evidence_ids": ["e1"],
                    }
                )
                + "\n"
                + json.dumps(
                    {"query_id": "q001", "coverage_audit": "present", "judged_by": "h", "judgment_status": "verified", "evidence_ids": ["e1"]}
                )
                + "\n",
                encoding="utf-8",
            )
            out = tmp / "qrels.jsonl"
            proc = self._run_cp1252(["judge-import", "--judgments", str(good), "--queries", str(FIXTURES / "queries.json"), "--out", str(out)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()
