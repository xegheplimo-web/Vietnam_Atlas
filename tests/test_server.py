"""Integration tests: real HTTP server + persistent SQLite memory."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unicodedata
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server.app import make_handler, satellite_layer_config
from server.live import LiveChanges
from server.store import init_db, list_places, normalize, save_place, session


class AtlasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "atlas.sqlite3"
        init_db(cls.db)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        h = {"Content-Type": "application/json"} if body else {}
        h.update(headers or {})
        req = Request(self.base + path, data=body, method=method, headers=h)
        try:
            with urlopen(req, timeout=10) as response:
                b = response.read()
                code = response.status
                ctype = response.headers["Content-Type"]
        except HTTPError as exc:
            b = exc.read()
            code = exc.code
            ctype = exc.headers["Content-Type"]
        return code, json.loads(b) if "json" in ctype else b

    def test_01_health_provinces_static(self):
        code, data = self.request("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        code, data = self.request("/api/provinces")
        self.assertEqual(data["count"], 34)
        self.assertEqual(len(set(data["items"])), 34)
        code, data = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Vietnam Atlas", data)
        code, data = self.request("/server/app.py")
        self.assertEqual(code, 404)

    def test_02_crud_search_history_geojson(self):
        p = {
            "name": "Cửa hàng Bến Thành",
            "lat": 10.7712,
            "lon": 106.6981,
            "province": "Hồ Chí Minh",
            "category": "shop",
            "source": "test",
            "source_id": "abc123",
            "notes": "Đường Nguyễn Huệ",
        }
        code, result = self.request("/api/places", "POST", p)
        self.assertEqual(code, 201)
        pid = result["item"]["id"]
        self.assertEqual(result["action"], "create")
        code, result = self.request("/api/places?search=cua%20hang&limit=10")
        self.assertEqual(code, 200)
        self.assertTrue(any(x["id"] == pid for x in result["items"]))
        code, result = self.request("/api/places?bbox=105,10,107,11")
        self.assertTrue(any(x["id"] == pid for x in result["items"]))
        code, result = self.request("/api/places?bbox=100,20,101,21")
        self.assertFalse(any(x["id"] == pid for x in result["items"]))
        code, result = self.request("/api/places/" + pid)
        self.assertEqual(result["name"], p["name"])
        p["name"] = "Cửa hàng cập nhật"
        code, result = self.request("/api/places/" + pid, "PUT", p)
        self.assertEqual(result["action"], "update")
        self.assertEqual(result["item"]["name"], p["name"])
        code, result = self.request("/api/places/" + pid + "/history")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["action"], "update")
        code, result = self.request("/api/export.geojson")
        self.assertEqual(code, 200)
        self.assertTrue(any(f["id"] == pid for f in result["features"]))
        code, result = self.request("/api/places/" + pid, "DELETE")
        self.assertTrue(result["deleted"])
        code, result = self.request("/api/places/" + pid)
        self.assertEqual(code, 404)
        code, result = self.request("/api/places/" + pid + "/history")
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["items"][0]["action"], "delete")

    def test_03_import_dedup_invalid(self):
        point = {"name": "Điểm Hà Nội", "source": "osm", "source_id": "node/123", "lat": 21.025, "lon": 105.8}
        payload = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [105.8, 21.025]}, "properties": point},
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [105.8, 21.025]}, "properties": dict(point, name="Đã sửa")},
                {"type": "Feature", "geometry": {"type": "LineString", "coordinates": []}, "properties": {}},
            ],
        }
        code, data = self.request("/api/import", "POST", payload)
        self.assertEqual(code, 200)
        self.assertEqual(data["created"], 1)
        self.assertEqual(data["updated"], 1)
        self.assertEqual(data["rejected"], 1)
        code, data = self.request("/api/places?search=diem")
        self.assertFalse(any(x["source_id"] == "node/123" for x in data["items"]))
        code, data = self.request("/api/places?search=sua")
        self.assertTrue(any(x["source_id"] == "node/123" for x in data["items"]))
        code, data = self.request("/api/places", "POST", {"name": "bad", "lat": 999, "lon": 1})
        self.assertEqual(code, 400)
        code, data = self.request("/api/stats")
        self.assertGreaterEqual(data["total_places"], 1)

    def test_04_origin_access_restriction(self):
        code, _result = self.request("/api/places", "POST", {"name": "X", "lat": 1, "lon": 1}, headers={"Origin": "https://evil.test"})
        self.assertEqual(code, 403)

    def test_05_csv(self):
        code, data = self.request("/api/export.csv")
        self.assertEqual(code, 200)
        self.assertIn(b"name,category", data)

    def test_06_normalization(self):
        self.assertEqual(normalize("Đường Trần Hưng Đạo"), "duong tran hung dao")

    def test_07_detailed_memory_and_media(self):
        p = {"name": "Địa điểm có bộ nhớ", "lat": 21.1, "lon": 105.9}
        code, data = self.request("/api/places", "POST", p)
        self.assertEqual(code, 201)
        pid = data["item"]["id"]
        code, data = self.request(
            f"/api/places/{pid}/memories",
            "POST",
            {"kind": "verification", "content": "Đã kiểm tra tọa độ thực tế", "source": "field visit", "confidence": 0.9},
        )
        self.assertEqual(code, 201)
        code, data = self.request(f"/api/places/{pid}/memories")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["kind"], "verification")
        code, data = self.request(
            f"/api/places/{pid}/media", "POST", {"url": "https://example.org/photo.jpg", "source": "user upload", "rights_basis": "My original photo"}
        )
        self.assertEqual(code, 201)
        code, data = self.request(f"/api/places/{pid}/media")
        self.assertEqual(len(data["items"]), 1)
        code, data = self.request(f"/api/places/{pid}/media", "POST", {"url": "http://bad.example/a.png", "source": "unknown", "rights_basis": "?"})
        self.assertEqual(code, 400)

    def test_08_mapping_and_csv_bulk(self):
        root = Path(__file__).resolve().parents[1]
        m = json.loads((root / "data/province_mergers_2025.json").read_text(encoding="utf-8"))
        self.assertEqual(len(m["new_to_old"]), 34)
        merged = [x for vals in m["new_to_old"].values() for x in vals]
        self.assertEqual(len(merged), 63)
        self.assertEqual(len(set(merged)), 63)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "import.sqlite3"
            proc = subprocess.run(
                [sys.executable, str(root / "tools/import_csv.py"), str(root / "data/sample_import.csv"), "--db", str(db)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with session(db) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM places").fetchone()[0], 2)
            proc = subprocess.run(
                [sys.executable, str(root / "tools/import_csv.py"), str(root / "data/sample_import.csv"), "--db", str(db)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with session(db) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM places").fetchone()[0], 2)

    def test_09_satellite_opt_in_and_url_validation(self):
        with patch.dict(
            os.environ,
            {
                "ATLAS_MAPTILER_KEY": "",
                "ATLAS_SATELLITE_TILES_URL": "",
                "ATLAS_SATELLITE_ATTRIBUTION": "",
            },
        ):
            code, data = self.request("/api/map-config")
            self.assertEqual(code, 200)
            self.assertIsNone(data["satellite"])

        with patch.dict(
            os.environ,
            {
                "ATLAS_MAPTILER_KEY": "browser-key-test",
                "ATLAS_SATELLITE_TILES_URL": "",
                "ATLAS_SATELLITE_ATTRIBUTION": "",
            },
        ):
            code, data = self.request("/api/map-config")
            self.assertEqual(code, 200)
            self.assertEqual(data["satellite"]["source"], "maptiler")
            url = data["satellite"]["url"]
            self.assertIn("key=browser-key-test", url)
            # Regression guard: pins the endpoint path verified live on 2026-10-11
            # (tiles/satellite-v4 shipped once and returned 404 with a real key).
            # Shape check only — live availability is covered by manual browser E2E,
            # not CI (tests make no external network calls).
            self.assertTrue(url.startswith("https://api.maptiler.com/maps/satellite/{z}/{x}/{y}.jpg?"), url)

        with patch.dict(
            os.environ,
            {
                "ATLAS_SATELLITE_TILES_URL": "https://tiles.example.test/{z}/{x}/{y}.png",
                "ATLAS_SATELLITE_ATTRIBUTION": "Imagery & Maps",
            },
        ):
            self.assertEqual(satellite_layer_config()["source"], "custom")
            self.assertIn("&amp;", satellite_layer_config()["attribution"])

        with patch.dict(
            os.environ,
            {
                "ATLAS_MAPTILER_KEY": "",
                "ATLAS_SATELLITE_TILES_URL": "http://external.example.test/{z}/{x}/{y}.png",
                "ATLAS_SATELLITE_ATTRIBUTION": "Unsafe",
            },
        ):
            self.assertIsNone(satellite_layer_config())

        with patch.dict(
            os.environ,
            {
                "ATLAS_MAPTILER_KEY": "browser-key-test",
                "ATLAS_SATELLITE_TILES_URL": "http://external.example.test/{z}/{x}/{y}.png",
                "ATLAS_SATELLITE_ATTRIBUTION": "Unsafe",
            },
        ):
            fallback = satellite_layer_config()
            self.assertEqual(fallback["source"], "maptiler")

        with patch.dict(
            os.environ,
            {
                "ATLAS_SATELLITE_TILES_URL": "https://tiles.example.test/{z}/{x}/{y}.png",
                "ATLAS_SATELLITE_ATTRIBUTION": '<a href="https://example.org/credits">Imagery</a> & <script>alert(1)</script>',
            },
        ):
            credit = satellite_layer_config()["attribution"]
            self.assertIn('<a href="https://example.org/credits" target="_blank"', credit)
            self.assertIn("&amp;", credit)
            self.assertNotIn("&lt;a", credit)
            self.assertNotIn("<script>", credit)

    def test_10_live_sse_updates_after_committed_write(self):
        # Streaming clients should receive a committed revision without polling
        # for all places or reopening the map.
        with urlopen(self.base + "/api/live", timeout=5) as stream:
            self.assertTrue(stream.headers["Content-Type"].startswith("text/event-stream"))
            ready_lines = []
            while True:
                line = stream.readline().decode("utf-8").strip()
                if not line:
                    if any(x.startswith("event: ready") for x in ready_lines):
                        break
                    continue
                ready_lines.append(line)
            self.assertIn("event: ready", ready_lines)

            code, created = self.request(
                "/api/places",
                "POST",
                {"name": "Điểm live demo", "lat": 21.03, "lon": 105.85, "source": "test_live", "source_id": "stream-01"},
            )
            self.assertIn(code, (200, 201))
            event_lines = []
            while True:
                line = stream.readline().decode("utf-8").strip()
                if not line:
                    if event_lines:
                        break
                    continue
                event_lines.append(line)
            self.assertIn("event: changed", event_lines)
            self.assertTrue(any(line.startswith("id: ") for line in event_lines))
            payload = json.loads(next(line.split("data: ", 1)[1] for line in event_lines if line.startswith("data: ")))
            self.assertGreater(payload["revision"], 0)

        place_id = created["item"]["id"]
        self.request("/api/places/" + place_id, "DELETE")

    def test_11_live_reconnect_recovers_missed_changes(self):
        # A reconnecting EventSource sends Last-Event-ID; if the committed
        # revision is newer, the stream must emit `changed` right away so a
        # tab that missed events while disconnected resyncs.
        code, created = self.request(
            "/api/places",
            "POST",
            {"name": "Điểm reconnect demo", "lat": 21.04, "lon": 105.86, "source": "test_live", "source_id": "reconnect-01"},
        )
        self.assertIn(code, (200, 201))
        req = Request(self.base + "/api/live", headers={"Last-Event-ID": "0"})
        with urlopen(req, timeout=5) as stream:
            seen = []
            while "event: changed" not in seen:
                line = stream.readline().decode("utf-8").strip()
                if line.startswith("event: "):
                    seen.append(line)
        self.assertIn("event: ready", seen)
        self.request("/api/places/" + created["item"]["id"], "DELETE")

    def test_12_live_capacity_rejects_extra_stream_with_503(self):
        class FakeHandler:
            def __init__(self):
                self.headers = {}
                self.status = None

            def json(self, status, _payload):
                self.status = status

        live = LiveChanges(self.db, capacity=1)
        self.assertTrue(live.slots.acquire(blocking=False))
        try:
            handler = FakeHandler()
            live.serve(handler)
            self.assertEqual(handler.status, 503)
        finally:
            live.slots.release()

    def test_13_search_relevance_and_vietnamese_aliases(self):
        # Exact normalized-name match must beat a newer row that only shares tokens (BM25 + exact boost).
        code, older = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Ăn Ngon", "lat": 21.03, "lon": 105.85, "category": "restaurant", "source": "test_search", "source_id": "rank-old"},
        )
        self.assertEqual(code, 201)
        code, newer = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Ăn Ngon 2", "lat": 21.04, "lon": 105.86, "category": "restaurant", "source": "test_search", "source_id": "rank-new"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=qu%C3%A1n%20%C4%83n%20ngon")
        self.assertEqual(code, 200)
        ids = [x["id"] for x in result["items"]]
        self.assertEqual(ids[0], older["item"]["id"])  # exact name first, despite being older
        self.assertIn(newer["item"]["id"], ids)

        # Vietnamese aliases: bv → bệnh viện.
        code, hosp = self.request(
            "/api/places",
            "POST",
            {
                "name": "Bệnh viện Đa khoa Tâm Anh",
                "lat": 21.0,
                "lon": 105.8,
                "category": "hospital",
                "source": "test_search",
                "source_id": "alias-hosp",
            },
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=bv%20tam%20anh")
        self.assertEqual(next(x["id"] for x in result["items"]), hosp["item"]["id"])

        # The alias OR keeps the literal token working too (ubnd stays ubnd).
        code, gov = self.request(
            "/api/places",
            "POST",
            {"name": "UBND xã Test Alias", "lat": 21.05, "lon": 105.87, "category": "townhall", "source": "test_search", "source_id": "alias-gov"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=ubnd%20xa%20test")
        self.assertEqual(next(x["id"] for x in result["items"]), gov["item"]["id"])

        # Unrelated query returns nothing (no false positives).
        code, result = self.request("/api/places?search=zzzqqq%20khong%20co")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["items"], [])

        for item in (older, newer, hosp, gov):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_14_benchmark_harness_scores_fixed_queries(self):
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("search_benchmark", root / "tools" / "search_benchmark.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        code, created = self.request(
            "/api/places",
            "POST",
            {"name": "Bến xe Miền Đông Mới", "lat": 10.85, "lon": 106.8, "category": "bus_station", "source": "test_bench", "source_id": "bench-01"},
        )
        self.assertEqual(code, 201)
        pid = created["item"]["id"]
        queries = [
            {"id": "b1", "kind": "exact_name", "query": "Bến xe Miền Đông Mới", "expected_ids": [pid]},
            {"id": "b2", "kind": "no_match", "query": "zzzqqq khong co gi", "expected_ids": []},
        ]
        result = module.run_benchmark(self.db, queries, limit=10, repeats=1)
        self.assertEqual(result["summary"]["hit@10"], 1.0)
        self.assertEqual(result["summary"]["no_match_false_positives"], 0)
        self.request("/api/places/" + pid, "DELETE")

    def test_16_reordered_matches_survive_exact_boost(self):
        # PR#6 review: a phrase-first candidate set used to hide reordered matches.
        # The exact match must rank first while reordered-token matches stay findable.
        code, a = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Ăn Ngon T16", "lat": 21.02, "lon": 105.83, "category": "restaurant", "source": "test_search", "source_id": "reorder-a"},
        )
        self.assertEqual(code, 201)
        code, b = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Ngon Ăn T16", "lat": 21.021, "lon": 105.831, "category": "restaurant", "source": "test_search", "source_id": "reorder-b"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=quan%20an%20ngon%20t16&limit=50")
        self.assertEqual(code, 200)
        ids = [x["id"] for x in result["items"]]
        self.assertEqual(ids[0], a["item"]["id"])
        self.assertIn(b["item"]["id"], ids)
        for item in (a, b):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_17_bbox_search_keeps_reordered_in_map(self):
        # PR#6 review: an off-map phrase match used to empty an otherwise valid in-map search
        # (the phrase probe ignored the viewport filter).
        code, far = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Ăn Ngon T17", "lat": 21.03, "lon": 105.85, "category": "restaurant", "source": "test_search", "source_id": "bbox-far"},
        )
        self.assertEqual(code, 201)
        code, near = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Ngon Ăn T17", "lat": 16.05, "lon": 108.20, "category": "restaurant", "source": "test_search", "source_id": "bbox-near"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=quan%20an%20ngon%20t17&bbox=108.15,16.0,108.25,16.1")
        self.assertEqual(code, 200)
        ids = [x["id"] for x in result["items"]]
        self.assertIn(near["item"]["id"], ids)
        self.assertNotIn(far["item"]["id"], ids)
        for item in (far, near):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_15_long_exact_name_survives_doc_length_bias(self):
        # A long exact name must outrank many short names sharing the same leading tokens:
        # BM25 doc-length bias used to bury it beyond the fetch window (phrase-first match pins it).
        created = []
        for i in range(12):
            code, item = self.request(
                "/api/places",
                "POST",
                {
                    "name": f"Trường Tiểu học và Trung học cơ sở Test {i}",
                    "lat": 21.02,
                    "lon": 105.83,
                    "category": "school",
                    "source": "test_search",
                    "source_id": f"bias-{i}",
                },
            )
            self.assertEqual(code, 201)
            created.append(item)
        code, exact = self.request(
            "/api/places",
            "POST",
            {
                "name": "Trường Trung Học Cơ Sở Và Trung Học Phổ Thông Test Long",
                "lat": 21.03,
                "lon": 105.84,
                "category": "school",
                "source": "test_search",
                "source_id": "bias-exact",
            },
        )
        self.assertEqual(code, 201)
        created.append(exact)
        code, result = self.request("/api/places?search=truong%20trung%20hoc%20co%20so%20va%20trung%20hoc%20pho%20thong%20test%20long")
        self.assertEqual(code, 200)
        self.assertEqual(next(x["id"] for x in result["items"]), exact["item"]["id"])
        for item in created:
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_18_category_prefix_filter(self):
        code, cafe = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Cà Phê T18", "lat": 21.02, "lon": 105.83, "category": "amenity:cafe", "source": "test_search", "source_id": "cat-cafe"},
        )
        self.assertEqual(code, 201)
        code, rest = self.request(
            "/api/places",
            "POST",
            {
                "name": "Quán Ăn T18",
                "lat": 21.021,
                "lon": 105.831,
                "category": "amenity:restaurant",
                "source": "test_search",
                "source_id": "cat-rest",
            },
        )
        self.assertEqual(code, 201)
        cafe_id, rest_id = cafe["item"]["id"], rest["item"]["id"]

        code, result = self.request("/api/places?search=t18&category=amenity:cafe")
        self.assertEqual(code, 200)
        ids = [x["id"] for x in result["items"]]
        self.assertIn(cafe_id, ids)
        self.assertNotIn(rest_id, ids)

        code, result = self.request("/api/places?search=t18&category=amenity:cafe%7Camenity:restaurant")
        ids = [x["id"] for x in result["items"]]
        self.assertIn(cafe_id, ids)
        self.assertIn(rest_id, ids)

        code, result = self.request("/api/places?search=t18&category=shop:")
        self.assertNotIn(cafe_id, [x["id"] for x in result["items"]])
        self.assertNotIn(rest_id, [x["id"] for x in result["items"]])

        code, fc = self.request("/api/export.geojson?search=t18&category=amenity:restaurant")
        self.assertEqual(code, 200)
        fids = [f["id"] for f in fc["features"]]
        self.assertIn(rest_id, fids)
        self.assertNotIn(cafe_id, fids)
        for item in (cafe, rest):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_19_category_and_near_param_validation(self):
        for bad in ("DROP%20TABLE", "amenity:cafe%3Bdrop", "amenity:|", "|shop:x", "Amenity:cafe", "shop:x|"):
            code, _result = self.request("/api/places?category=" + bad)
            self.assertEqual(code, 400, bad)
        code, _result = self.request("/api/places?category=" + "|".join("p" + str(i) for i in range(21)))
        self.assertEqual(code, 400)
        code, _result = self.request("/api/places?category=amenity:cafe&limit=1")
        self.assertEqual(code, 200)
        for bad in ("abc", "21.0", "91,0", "10,200", "21.0,105.0,extra"):
            code, _result = self.request("/api/places?near=" + bad)
            self.assertEqual(code, 400, bad)
        code, _result = self.request("/api/places?near=21.0,105.0&limit=1")
        self.assertEqual(code, 200)

    def test_20_near_distance_and_ordering(self):
        code, near_p = self.request(
            "/api/places",
            "POST",
            {"name": "Điểm Gần T20", "lat": 21.0, "lon": 105.0, "category": "test:near", "source": "test_search", "source_id": "near-a"},
        )
        self.assertEqual(code, 201)
        code, far_p = self.request(
            "/api/places",
            "POST",
            {"name": "Điểm Xa T20", "lat": 21.2, "lon": 105.0, "category": "test:near", "source": "test_search", "source_id": "near-b"},
        )
        self.assertEqual(code, 201)
        near_id, far_id = near_p["item"]["id"], far_p["item"]["id"]

        code, result = self.request("/api/places?near=21.0,105.0&category=test:near")
        self.assertEqual(code, 200)
        by_id = {x["id"]: x for x in result["items"]}
        self.assertIn(near_id, by_id)
        self.assertIn(far_id, by_id)
        self.assertEqual(by_id[near_id]["distance_km"], 0.0)
        self.assertAlmostEqual(by_id[far_id]["distance_km"], 22.2, delta=0.5)
        ids = [x["id"] for x in result["items"]]
        self.assertLess(ids.index(near_id), ids.index(far_id))  # closer first without search

        # With a search term, relevance (exact boost + BM25) still wins over distance.
        code, exact_far = self.request(
            "/api/places",
            "POST",
            {
                "name": "Bia Hơi T20 Distance Check",
                "lat": 10.0,
                "lon": 106.0,
                "category": "test:near",
                "source": "test_search",
                "source_id": "near-c",
            },
        )
        self.assertEqual(code, 201)
        code, partial_near = self.request(
            "/api/places",
            "POST",
            {
                "name": "Bia Hơi T20 Distance Check Khác",
                "lat": 21.0,
                "lon": 105.0,
                "category": "test:near",
                "source": "test_search",
                "source_id": "near-d",
            },
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=bia%20hoi%20t20%20distance%20check&near=21.0,105.0&category=test:near")
        items = result["items"]
        self.assertEqual(items[0]["id"], exact_far["item"]["id"])  # exact name beats closer distance
        self.assertIn("distance_km", items[0])
        self.assertGreater(items[0]["distance_km"], 100)
        for item in (near_p, far_p, exact_far, partial_near):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_21_province_phrase_alias(self):
        # Legacy name in the query must also match POIs recorded under the 2025 name.
        code, hcm = self.request(
            "/api/places",
            "POST",
            {
                "name": "Trung Tâm Hành Chính T21",
                "address": "Quận 1, Hồ Chí Minh",
                "lat": 10.78,
                "lon": 106.7,
                "source": "test_search",
                "source_id": "legacy-hcm",
            },
        )
        self.assertEqual(code, 201)
        code, literal = self.request(
            "/api/places",
            "POST",
            {"name": "Quán Sài Gòn T21", "lat": 10.75, "lon": 106.66, "source": "test_search", "source_id": "legacy-sg"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=s%C3%A0i%20g%C3%B2n%20t21")
        ids = [x["id"] for x in result["items"]]
        self.assertIn(hcm["item"]["id"], ids)  # matched via sài gòn -> hồ chí minh
        self.assertIn(literal["item"]["id"], ids)  # literal spelling still matches

        code, hn = self.request(
            "/api/places",
            "POST",
            {"name": "Ủy Ban Kiểm Tra T21", "address": "Hà Nội", "lat": 21.03, "lon": 105.85, "source": "test_search", "source_id": "legacy-hn"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=h%C3%A0%20t%C3%A2y%20t21")
        self.assertIn(hn["item"]["id"], [x["id"] for x in result["items"]])  # hà tây -> hà nội
        for item in (hcm, literal, hn):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_22_phrase_alias_needs_token_boundary(self):
        # "saigon" (one token) must NOT trigger the "sai gon" phrase alias.
        code, hcm = self.request(
            "/api/places",
            "POST",
            {"name": "Công Viên T22", "address": "Hồ Chí Minh", "lat": 10.77, "lon": 106.69, "source": "test_search", "source_id": "bound-hcm"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=saigon%20t22")
        self.assertNotIn(hcm["item"]["id"], [x["id"] for x in result["items"]])
        code, result = self.request("/api/places?search=sai%20gon%20t22")
        self.assertIn(hcm["item"]["id"], [x["id"] for x in result["items"]])
        self.request("/api/places/" + hcm["item"]["id"], "DELETE")

    def test_23_recall_and_coverage_gap_harness(self):
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("search_benchmark", root / "tools" / "search_benchmark.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        code, hit_p = self.request(
            "/api/places",
            "POST",
            {"name": "Bến xe Recall Alpha", "lat": 10.85, "lon": 106.8, "source": "test_bench", "source_id": "recall-01"},
        )
        self.assertEqual(code, 201)
        code, miss_p = self.request(
            "/api/places",
            "POST",
            {"name": "Nhà thờ Không Trùng", "lat": 10.86, "lon": 106.81, "source": "test_bench", "source_id": "recall-02"},
        )
        self.assertEqual(code, 201)
        queries = [
            {"id": "r1", "kind": "exact", "query": "Bến xe Recall Alpha", "expected_ids": [hit_p["item"]["id"], miss_p["item"]["id"]]},
            {"id": "r2", "kind": "admin_legacy", "query": "quán ăn tỉnh không tồn tại", "expected_ids": [], "coverage_gap": True},
            {"id": "r3", "kind": "no_match", "query": "zzzqqq khong co", "expected_ids": []},
        ]
        result = module.run_benchmark(self.db, queries, limit=10, repeats=1)
        summary = result["summary"]
        self.assertEqual(summary["queries_labelled"], 1)
        self.assertEqual(summary["recall@10"], 0.5)  # 1 of 2 expected ids in top-10
        self.assertEqual(summary["coverage_gap_queries"], 1)
        self.assertEqual(summary["no_match_false_positives"], 0)
        row = next(r for r in result["per_query"] if r["id"] == "r1")
        self.assertEqual(row["recall@10"], 0.5)
        for item in (hit_p, miss_p):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_24_category_like_underscore_literal(self):
        # '_' in a category prefix must be literal, not a LIKE single-char wildcard.
        code, parts = self.request(
            "/api/places",
            "POST",
            {
                "name": "Xưởng Phụ Tùng T24",
                "lat": 21.04,
                "lon": 105.84,
                "category": "shop:car_parts",
                "source": "test_search",
                "source_id": "esc-parts",
            },
        )
        self.assertEqual(code, 201)
        code, near_miss = self.request(
            "/api/places",
            "POST",
            {
                "name": "Gian Hàng Gần T24",
                "lat": 21.041,
                "lon": 105.841,
                "category": "shop:carXparts",
                "source": "test_search",
                "source_id": "esc-miss",
            },
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=t24&category=shop:car_parts")
        self.assertEqual(code, 200)
        ids = [x["id"] for x in result["items"]]
        self.assertIn(parts["item"]["id"], ids)
        self.assertNotIn(near_miss["item"]["id"], ids)
        for item in (parts, near_miss):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_25_near_ordering_uses_unrounded_distance(self):
        # Both distances round to 0.0 km; the nearer place has the LARGER id, so a
        # rounded ORDER BY would rank the farther place first.
        code, far = self.request(
            "/api/places",
            "POST",
            {
                "id": "a-t25-far",
                "name": "Điểm Xa T25",
                "lat": 21.0004,
                "lon": 105.0,
                "category": "test:t25",
                "source": "test_search",
                "source_id": "t25-far",
            },
        )
        self.assertEqual(code, 201)
        code, near = self.request(
            "/api/places",
            "POST",
            {
                "id": "b-t25-near",
                "name": "Điểm Gần T25",
                "lat": 21.00005,
                "lon": 105.0,
                "category": "test:t25",
                "source": "test_search",
                "source_id": "t25-near",
            },
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?near=21.0,105.0&category=test:t25")
        self.assertEqual(code, 200)
        items = result["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["distance_km"], 0.0)
        self.assertEqual(items[1]["distance_km"], 0.0)
        self.assertEqual(items[0]["id"], near["item"]["id"])  # ~6 m ranks before ~44 m
        self.assertEqual(items[1]["id"], far["item"]["id"])
        for item in (near, far):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_26_tp_hcm_alias_keeps_token_expansion(self):
        # 'tp hcm' must match 'thành phố hcm' (tp -> thanh pho inside the old-spelling
        # branch) as well as 'hồ chí minh' (phrase alias branch).
        code, spelled = self.request(
            "/api/places",
            "POST",
            {"name": "Ủy Ban Thành Phố HCM T26", "lat": 10.78, "lon": 106.7, "source": "test_search", "source_id": "t26-spelled"},
        )
        self.assertEqual(code, 201)
        code, renamed = self.request(
            "/api/places",
            "POST",
            {"name": "Trung Tâm Hồ Chí Minh T26", "lat": 10.79, "lon": 106.71, "source": "test_search", "source_id": "t26-renamed"},
        )
        self.assertEqual(code, 201)
        code, result = self.request("/api/places?search=tp%20hcm%20t26")
        self.assertEqual(code, 200)
        ids = [x["id"] for x in result["items"]]
        self.assertIn(spelled["item"]["id"], ids)
        self.assertIn(renamed["item"]["id"], ids)
        for item in (spelled, renamed):
            self.request("/api/places/" + item["item"]["id"], "DELETE")

    def test_27_province_alias_in_like_fallback(self):
        # Without FTS5 the LIKE fallback must still expand legacy province phrases.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "nofts.sqlite3"
            init_db(db)
            with session(db) as con:
                save_place(
                    con,
                    {"name": "Ủy Ban T27", "address": "Hà Nội", "lat": 21.03, "lon": 105.85, "source": "test_search", "source_id": "t27-fallback"},
                )
                con.execute("DROP TRIGGER IF EXISTS places_fts_ai")
                con.execute("DROP TRIGGER IF EXISTS places_fts_ad")
                con.execute("DROP TRIGGER IF EXISTS places_fts_au")
                con.execute("DROP TABLE places_fts")
            with session(db) as con:
                items, total = list_places(con, search="hà tây t27")
            self.assertEqual(total, 1)
            self.assertEqual(items[0]["source_id"], "t27-fallback")

    def test_28_geo_scope_search(self):
        # Province name in the query geo-scopes results: in-polygon POIs win via
        # province_code, while literal text matches (outside polygon / unassigned)
        # stay reachable through the OR fallback.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite3"
            init_db(db)
            with session(db) as con:
                save_place(con, {"name": "Phở Geo T28", "lat": 10.776, "lon": 106.700, "source": "t28", "source_id": "t28-hcm1"})
                save_place(con, {"name": "Phở Geo T28 Hai", "lat": 10.81, "lon": 106.67, "source": "t28", "source_id": "t28-hcm2"})
                save_place(con, {"name": "Phở Geo T28 HN", "lat": 21.03, "lon": 105.85, "source": "t28", "source_id": "t28-hn"})
                # Sea point (outside every polygon) that names the province literally.
                save_place(
                    con,
                    {"name": "Phở Biển T28", "address": "Hồ Chí Minh", "lat": 16.0, "lon": 110.0, "source": "t28", "source_id": "t28-sea"},
                )
                save_place(con, {"name": "Quán Ăn T28", "lat": 22.147, "lon": 105.834, "source": "t28", "source_id": "t28-bk"})
            with session(db) as con:
                items, _total = list_places(con, search="phở hồ chí minh")
                ids = {x["source_id"] for x in items}
                in_hcm = [x for x in items if x["province_code"] == "79"]
                self.assertGreaterEqual(len(in_hcm), 2)
                self.assertIn("t28-hcm1", ids)
                self.assertIn("t28-hcm2", ids)
                # No text mention of HCM → the Hà Nội POI is out of scope.
                self.assertNotIn("t28-hn", ids)
                # Outside-polygon POI still found via the province-text fallback.
                self.assertIn("t28-sea", ids)
                # Old province name scopes to the merged province (Bắc Kạn → Thái Nguyên).
                items, _total = list_places(con, search="quán ăn bắc kạn")
                self.assertGreaterEqual(len(items), 1)
                self.assertEqual(items[0]["province_code"], "19")
                self.assertEqual(items[0]["source_id"], "t28-bk")
                # No province in query → old behaviour: both provinces' POIs return.
                items, _total = list_places(con, search="phở geo t28")
                self.assertEqual({x["province_code"] for x in items}, {"79", "01"})

    def test_29_save_place_assigns_province_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite3"
            init_db(db)
            with session(db) as con:
                current, _ = save_place(con, {"name": "T29 HCM", "lat": 10.776, "lon": 106.700, "source": "t29", "source_id": "t29-in"})
                self.assertEqual(current["province_code"], "79")
                current, _ = save_place(con, {"name": "T29 Sea", "lat": 16.0, "lon": 110.0, "source": "t29", "source_id": "t29-out"})
                self.assertIsNone(current["province_code"])

    def test_30_generic_city_phrase_does_not_scope(self):
        # "thành phố" đứng riêng không được scope theo HCM — query giữ hành vi text cũ.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite3"
            init_db(db)
            with session(db) as con:
                save_place(con, {"name": "Bệnh viện Thành Phố T30", "lat": 21.03, "lon": 105.85, "source": "t30", "source_id": "t30-hn"})
                save_place(con, {"name": "Bệnh viện Khác T30", "lat": 10.776, "lon": 106.700, "source": "t30", "source_id": "t30-hcm"})
            with session(db) as con:
                items, _total = list_places(con, search="bệnh viện thành phố")
                ids = {x["source_id"] for x in items}
                self.assertIn("t30-hn", ids)
                # Nếu bị scope HCM (bug cũ), POI HCM "Bệnh viện Khác" sẽ lọt vào.
                self.assertNotIn("t30-hcm", ids)

    def test_31_nfd_query_still_scopes(self):
        # Query ở dạng NFD (dấu tách rời) phải tokenize đúng như NFC.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite3"
            init_db(db)
            with session(db) as con:
                save_place(con, {"name": "Phở NFD T31", "lat": 21.03, "lon": 105.85, "source": "t31", "source_id": "t31-hn"})
                save_place(con, {"name": "Phở NFD T31 HCM", "lat": 10.776, "lon": 106.700, "source": "t31", "source_id": "t31-hcm"})
            with session(db) as con:
                items, _total = list_places(con, search=unicodedata.normalize("NFD", "phở hà nội"))
                ids = {x["source_id"] for x in items}
                self.assertIn("t31-hn", ids)
                self.assertNotIn("t31-hcm", ids)

    def test_32_province_param_matches_null_code_labels(self):
        # Rows ngoài polygon (code NULL) khớp mọi spelling được chấp nhận của tỉnh.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite3"
            init_db(db)
            with session(db) as con:
                save_place(con, {"name": "Quán Biển T32", "province": "Sài Gòn", "lat": 16.0, "lon": 110.0, "source": "t32", "source_id": "t32-sea"})
                save_place(con, {"name": "Quán Nội T32", "lat": 10.776, "lon": 106.700, "source": "t32", "source_id": "t32-hcm"})
            with session(db) as con:
                for value in ("79", "hồ chí minh", "sài gòn"):
                    with self.subTest(value=value):
                        items, _total = list_places(con, province=value, limit=50)
                        ids = {x["source_id"] for x in items}
                        self.assertIn("t32-hcm", ids)
                        self.assertIn("t32-sea", ids)

    def test_33_uppercase_accented_names_keep_boost(self):
        # SQLite lower() chỉ hạ ASCII — unicode_lower phải boost cả tên in hoa có dấu.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo.sqlite3"
            init_db(db)
            with session(db) as con:
                self.assertEqual(con.execute("SELECT unicode_lower('PHỞ')").fetchone()[0], "phở")
                save_place(con, {"name": "PHỞ NÂU T33", "lat": 21.03, "lon": 105.85, "source": "t33", "source_id": "t33-up"})
                save_place(con, {"name": "Phở Bò T33", "lat": 21.04, "lon": 105.86, "source": "t33", "source_id": "t33-low"})
            with session(db) as con:
                items, _total = list_places(con, search="phở hà nội")
                ids = [x["source_id"] for x in items]
                self.assertIn("t33-up", ids)
                self.assertIn("t33-low", ids)

    def test_34_init_db_backfills_province_code_on_upgrade(self):
        # DB cũ (chưa có cột province_code) → init_db migrate + backfill ngay trong upgrade path.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.sqlite3"
            init_db(db)
            with session(db) as con:
                save_place(con, {"name": "Legacy HN T34", "lat": 21.03, "lon": 105.85, "source": "t34", "source_id": "t34-hn"})
            con = sqlite3.connect(str(db))
            try:
                con.execute("DROP INDEX idx_places_province_code")
                con.execute("ALTER TABLE places DROP COLUMN province_code")
                con.commit()
            finally:
                con.close()
            init_db(db)
            with session(db) as con:
                row = con.execute("SELECT province_code FROM places WHERE source_id = 't34-hn'").fetchone()
                self.assertEqual(row["province_code"], "01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
