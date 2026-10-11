"""Integration tests: real HTTP server + persistent SQLite memory."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server.app import make_handler, satellite_layer_config
from server.store import init_db, normalize, session


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
            self.assertIn("key=browser-key-test", data["satellite"]["url"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
