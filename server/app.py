"""Vietnam Atlas HTTP server; stdlib-only, SQLite persistent storage.

Run: python -m server.app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .store import (
    add_media,
    add_memory,
    delete_place,
    feature_to_payload,
    geometry_feature,
    get_place,
    init_db,
    list_places,
    save_place,
    session,
    stats,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "atlas.sqlite3"
MAX_BODY = 16 * 1024 * 1024
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def satellite_layer_config():
    """Describe a licensed XYZ satellite source without proxying or downloading tiles.

    A MapTiler browser key is intentionally visible to the client: restrict it by
    Allowed HTTP Origin in the provider dashboard. Never use a secret server token.
    """

    custom_url = os.getenv("ATLAS_SATELLITE_TILES_URL", "").strip()
    if custom_url:
        parsed = urlsplit(custom_url)
        permitted = parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        attribution = os.getenv("ATLAS_SATELLITE_ATTRIBUTION", "").strip()
        if not (
            permitted
            and parsed.netloc
            and attribution
            and all("{" + axis + "}" in custom_url for axis in ("z", "x", "y"))
        ):
            return None
        return {
            "url": custom_url,
            "attribution": html.escape(attribution),
            "maxNativeZoom": 19,
            "tileSize": 256,
            "source": "custom",
        }

    key = os.getenv("ATLAS_MAPTILER_KEY", "").strip()
    if not key or len(key) > 256:
        return None
    return {
        "url": "https://api.maptiler.com/tiles/satellite-v4/{z}/{x}/{y}?key=" + quote(key, safe=""),
        "attribution": '&copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a> | Satellite imagery providers',
        "maxNativeZoom": 19,
        "tileSize": 512,
        "source": "maptiler",
    }


def make_handler(db_path=DEFAULT_DB, web_dir=None, token=""):
    db_path = Path(db_path)
    web_dir = Path(web_dir or ROOT / "web").resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "VietnamAtlas/2.0"

        def respond(self, code, data, kind="application/json; charset=utf-8", filename=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            elif isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store" if kind.startswith("application/json") else "public, max-age=300")
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def json(self, code, data):
            self.respond(code, data)

        def read_payload(self):
            try:
                n = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise ValueError("Content-Length không hợp lệ") from None
            if n < 0 or n > MAX_BODY:
                raise ValueError("Tệp quá lớn hoặc thiếu Content-Length (tối đa 16 MiB)")
            raw = self.rfile.read(n)
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("Dữ liệu JSON không hợp lệ") from None

        def authorized(self):
            # For local installations, same-origin browser forms need no secret.
            # For remote installations configure a trusted TLS/auth proxy instead.
            if token and self.headers.get("Authorization", "") != "Bearer " + token:
                self.json(401, {"error": "Yêu cầu API token hợp lệ"})
                return False
            origin = self.headers.get("Origin")
            if origin:
                target = urlsplit(origin)
                if target.netloc != self.headers.get("Host") or target.scheme not in ("http", "https"):
                    self.json(403, {"error": "Yêu cầu không cùng nguồn bị từ chối"})
                    return False
            return True

        def route(self):
            parsed = urlsplit(self.path)
            return unquote(parsed.path), parse_qs(parsed.query)

        def do_GET(self):
            path, params = self.route()
            try:
                if path == "/api/map-config":
                    return self.json(200, {"satellite": satellite_layer_config()})
                if path == "/api/health":
                    with session(db_path) as c:
                        c.execute("SELECT 1")
                    return self.json(200, {"ok": True, "storage": "sqlite"})
                if path == "/api/stats":
                    with session(db_path) as c:
                        return self.json(200, stats(c))
                if path == "/api/provinces":
                    data = json.loads((ROOT / "data" / "provinces_34.json").read_text(encoding="utf-8"))
                    return self.json(200, {"items": [x["name"] for x in data], "count": len(data), "boundary_available": False})
                if path == "/api/places" or path == "/api/export.geojson":
                    search = (params.get("search") or [""])[0]
                    province = (params.get("province") or [""])[0]
                    bbox = None
                    if "bbox" in params:
                        vals = [float(v) for v in params["bbox"][0].split(",")]
                        if (
                            len(vals) != 4
                            or not all(-180 <= v <= 180 for v in (vals[0], vals[2]))
                            or not all(-90 <= v <= 90 for v in (vals[1], vals[3]))
                            or vals[1] > vals[3]
                        ):
                            raise ValueError("BBox phải là west,south,east,north (WGS84)")
                        bbox = vals
                    if path == "/api/export.geojson":
                        # Always respect request filtering; capped to prevent unbounded memory.
                        lim = min(max(int((params.get("limit") or ["20000"])[0]), 1), 20000)
                        with session(db_path) as c:
                            places, total = list_places(c, search, province, bbox, lim)
                        fc = {
                            "type": "FeatureCollection",
                            "name": "vietnam_atlas_export",
                            "features": [geometry_feature(p) for p in places],
                            "count": len(places),
                            "total_matching": total,
                        }
                        return self.respond(200, fc, "application/geo+json; charset=utf-8", "vietnam-atlas.geojson")
                    lim = min(max(int((params.get("limit") or ["500"])[0]), 1), 1000)
                    offset = max(int((params.get("offset") or ["0"])[0]), 0)
                    with session(db_path) as c:
                        items, total = list_places(c, search, province, bbox, lim, offset)
                    return self.json(200, {"items": items, "total": total, "limit": lim, "offset": offset})
                if path == "/api/export.csv":
                    with session(db_path) as c:
                        items, total = list_places(c, limit=20000)
                    sio = io.StringIO()
                    keys = [
                        "id",
                        "name",
                        "category",
                        "address",
                        "province",
                        "phone",
                        "website",
                        "lat",
                        "lon",
                        "notes",
                        "source",
                        "source_id",
                        "status",
                        "created_at",
                        "updated_at",
                    ]
                    w = csv.DictWriter(sio, fieldnames=keys, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(items)
                    return self.respond(200, "\ufeff" + sio.getvalue(), "text/csv; charset=utf-8", "vietnam-atlas.csv")
                if path.startswith("/api/places/"):
                    sub = path[len("/api/places/") :]
                    if sub.endswith(("/memories", "/media")):
                        pid, category = sub.rsplit("/", 1)
                        tablename = "memory_records" if category == "memories" else "place_media"
                        with session(db_path) as c:
                            if not get_place(c, pid):
                                return self.json(404, {"error": "Không tìm thấy địa điểm"})
                            items = [
                                dict(row)
                                for row in c.execute(f"SELECT * FROM {tablename} WHERE place_id=? ORDER BY created_at DESC LIMIT 500", (pid,))
                            ]
                        return self.json(200, {"items": items})
                    if sub.endswith("/history"):
                        pid = sub[: -len("/history")]
                        with session(db_path) as c:
                            logs = [
                                dict(x)
                                for x in c.execute(
                                    "SELECT seq,action,prior_json,current_json,changed_at FROM changes WHERE place_id=? ORDER BY seq DESC LIMIT 200",
                                    (pid,),
                                )
                            ]
                        return self.json(200, {"items": logs})
                    with session(db_path) as c:
                        item = get_place(c, sub)
                    return self.json(200, item) if item else self.json(404, {"error": "Không tìm thấy địa điểm"})
                return self.static(path)
            except (ValueError, OverflowError) as e:
                self.json(400, {"error": str(e)})
            except sqlite3.Error:
                self.json(500, {"error": "Lỗi cơ sở dữ liệu"})

        def static(self, path):
            path = path if path != "/" else "/index.html"
            if path.startswith("/api/"):
                return self.json(404, {"error": "Không tìm thấy API"})
            requested = (web_dir / path.lstrip("/")).resolve()
            if not requested.is_relative_to(web_dir) or not requested.is_file():
                return self.json(404, {"error": "Không tìm thấy tài nguyên"})
            self.respond(200, requested.read_bytes(), MIME.get(requested.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self.authorized():
                return
            path, _ = self.route()
            try:
                data = self.read_payload()
                if path == "/api/places":
                    with session(db_path) as c:
                        p, action = save_place(c, data)
                    return self.json(201 if action == "create" else 200, {"item": p, "action": action})
                if path.startswith("/api/places/") and (path.endswith(("/memories", "/media"))):
                    pid, category = path[len("/api/places/") :].rsplit("/", 1)
                    with session(db_path) as c:
                        item = add_memory(c, pid, data) if category == "memories" else add_media(c, pid, data)
                    return self.json(201, {"item": item})
                if path == "/api/import":
                    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
                        raise ValueError("Cần GeoJSON FeatureCollection")
                    if len(data["features"]) > 10000:
                        raise ValueError("Mỗi lần nhập tối đa 10000 điểm")
                    created = updated = 0
                    errors = []
                    with session(db_path) as c:
                        for idx, f in enumerate(data["features"]):
                            c.execute("SAVEPOINT atlas_import_item")
                            try:
                                payload = feature_to_payload(f)
                                _, action = save_place(c, payload)
                                if action == "create":
                                    created += 1
                                else:
                                    updated += 1
                            except (ValueError, TypeError, sqlite3.IntegrityError, KeyError) as exc:
                                c.execute("ROLLBACK TO SAVEPOINT atlas_import_item")
                                if len(errors) < 30:
                                    errors.append({"index": idx, "reason": str(exc)})
                            finally:
                                c.execute("RELEASE SAVEPOINT atlas_import_item")
                    return self.json(
                        200, {"created": created, "updated": updated, "rejected": len(data["features"]) - created - updated, "errors": errors}
                    )
                return self.json(404, {"error": "Không tìm thấy API"})
            except (ValueError, TypeError, OverflowError) as e:
                return self.json(400, {"error": str(e)})
            except LookupError as e:
                return self.json(404, {"error": str(e)})
            except sqlite3.Error:
                return self.json(500, {"error": "Lỗi cơ sở dữ liệu"})

        def do_PUT(self):
            if not self.authorized():
                return
            path, _ = self.route()
            if not path.startswith("/api/places/"):
                return self.json(404, {"error": "Không tìm thấy API"})
            pid = path[len("/api/places/") :]
            try:
                data = self.read_payload()
                with session(db_path) as c:
                    if not get_place(c, pid):
                        return self.json(404, {"error": "Không tìm thấy địa điểm"})
                    p, action = save_place(c, data, pid)
                return self.json(200, {"item": p, "action": action})
            except (ValueError, TypeError) as e:
                return self.json(400, {"error": str(e)})
            except sqlite3.Error:
                return self.json(500, {"error": "Lỗi cơ sở dữ liệu"})

        def do_DELETE(self):
            if not self.authorized():
                return
            path, _ = self.route()
            if not path.startswith("/api/places/"):
                return self.json(404, {"error": "Không tìm thấy API"})
            pid = path[len("/api/places/") :]
            with session(db_path) as c:
                deleted = delete_place(c, pid)
            return self.json(200 if deleted else 404, {"deleted": deleted})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Vietnam Atlas server (SQLite persistent memory)")
    parser.add_argument("--host", default=os.getenv("ATLAS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ATLAS_PORT", "8765")))
    parser.add_argument("--db", default=os.getenv("ATLAS_DB", str(DEFAULT_DB)))
    args = parser.parse_args()
    init_db(args.db)
    handler = make_handler(args.db, token=os.getenv("ATLAS_API_TOKEN", ""))
    http = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Vietnam Atlas: http://{args.host}:{args.port} | Database: {args.db}", flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        http.server_close()


if __name__ == "__main__":
    main()
