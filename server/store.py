"""SQLite-backed persistent place memory with provenance and change history.

No third-party Python dependencies. All database queries use bound parameters.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("name", "category", "address", "province", "phone", "website", "lat", "lon", "notes", "source", "source_id", "status")
VALID_STATUSES = {"unverified", "verified", "inactive", "closed"}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text):
    s = str(text or "").lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def connect(dbpath):
    con = sqlite3.connect(str(dbpath), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def session(dbpath):
    con = connect(dbpath)
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db(dbpath):
    Path(dbpath).parent.mkdir(parents=True, exist_ok=True)
    with session(dbpath) as con:
        con.execute("PRAGMA journal_mode=WAL")
        fts_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='places_fts'").fetchone() is not None
        con.executescript("""
        CREATE TABLE IF NOT EXISTS places (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
          address TEXT NOT NULL DEFAULT '', province TEXT NOT NULL DEFAULT '',
          phone TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
          lat REAL NOT NULL, lon REAL NOT NULL, notes TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT 'user', source_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'unverified', search_text TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          CHECK(lat BETWEEN -90 AND 90), CHECK(lon BETWEEN -180 AND 180)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_identity
           ON places(source, source_id) WHERE source_id <> '';
        CREATE INDEX IF NOT EXISTS idx_location ON places(lat, lon);
        CREATE INDEX IF NOT EXISTS idx_province ON places(province);
        CREATE INDEX IF NOT EXISTS idx_update ON places(updated_at);
        CREATE TABLE IF NOT EXISTS changes (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, place_id TEXT NOT NULL,
          action TEXT NOT NULL, prior_json TEXT, current_json TEXT,
          changed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_change_place ON changes(place_id, seq DESC);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version','1');
        CREATE TABLE IF NOT EXISTS memory_records (
          id TEXT PRIMARY KEY, place_id TEXT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
          confidence REAL NOT NULL DEFAULT 1,
          observed_at TEXT, created_at TEXT NOT NULL,
          CHECK(confidence BETWEEN 0 AND 1)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_place ON memory_records(place_id,created_at);
        CREATE TABLE IF NOT EXISTS place_media (
          id TEXT PRIMARY KEY, place_id TEXT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
          url TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL, rights_basis TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_place ON place_media(place_id);
        """)
        # Search indexes are optional for SQLite builds without FTS5.
        try:
            con.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(
                search_text, content='places', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
            CREATE TRIGGER IF NOT EXISTS places_fts_ai AFTER INSERT ON places BEGIN
                INSERT INTO places_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
            END;
            CREATE TRIGGER IF NOT EXISTS places_fts_ad AFTER DELETE ON places BEGIN
                INSERT INTO places_fts(places_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
            END;
            CREATE TRIGGER IF NOT EXISTS places_fts_au AFTER UPDATE ON places BEGIN
                INSERT INTO places_fts(places_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
                INSERT INTO places_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
            END;
            """)
            if not fts_exists:
                con.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass


def as_dict(row):
    return dict(row) if row is not None else None


def clean(payload):
    if not isinstance(payload, dict):
        raise ValueError("Bản ghi phải là object JSON")
    p = {}
    for field in FIELDS:
        if field in ("lat", "lon"):
            try:
                n = float(payload[field])
            except (ValueError, TypeError, KeyError):
                raise ValueError(f"Tọa độ {field} không hợp lệ") from None
            if not math.isfinite(n) or (field == "lat" and not -90 <= n <= 90) or (field == "lon" and not -180 <= n <= 180):
                raise ValueError(f"Tọa độ {field} ngoài giới hạn WGS84")
            p[field] = n
        else:
            value = payload.get(field, "")
            value = str(value if value is not None else "").strip()
            if len(value) > (10000 if field == "notes" else 1000):
                raise ValueError(f"{field} vượt quá độ dài cho phép")
            p[field] = value
    if not p["name"]:
        raise ValueError("Tên địa điểm không được trống")
    p["status"] = p["status"] or "unverified"
    if p["status"] not in VALID_STATUSES:
        raise ValueError("Trạng thái không hợp lệ")
    p["source"] = p["source"] or "user"
    p["search_text"] = normalize(" ".join(p.get(k, "") for k in ("name", "category", "address", "province", "phone", "notes")))
    return p


def get_place(con, place_id):
    return as_dict(con.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone())


def save_place(con, payload, place_id=None):
    p = clean(payload)
    if place_id is None:
        place_id = str(payload.get("id") or uuid.uuid4())
    if not isinstance(place_id, str) or len(place_id) > 128 or not re.fullmatch(r"[a-zA-Z0-9_.:/-]+", place_id):
        raise ValueError("ID địa điểm không hợp lệ")
    if p["source_id"]:
        other = con.execute("SELECT id FROM places WHERE source = ? AND source_id = ?", (p["source"], p["source_id"])).fetchone()
        if other:
            place_id = other["id"]
    old = get_place(con, place_id)
    now = utcnow()
    if old:
        columns = [k for k in FIELDS] + ["search_text", "updated_at"]
        vals = [p[k] for k in FIELDS] + [p["search_text"], now, place_id]
        con.execute("UPDATE places SET " + ",".join(f"{k}=?" for k in columns) + " WHERE id=?", vals)
        action = "update"
    else:
        columns = ["id", *list(FIELDS), "search_text", "created_at", "updated_at"]
        vals = [place_id] + [p[k] for k in FIELDS] + [p["search_text"], now, now]
        con.execute("INSERT INTO places (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", vals)
        action = "create"
    current = get_place(con, place_id)
    if old is None or any(old.get(k) != current.get(k) for k in FIELDS):
        con.execute(
            "INSERT INTO changes(place_id,action,prior_json,current_json,changed_at) VALUES(?,?,?,?,?)",
            (place_id, action, json.dumps(old, ensure_ascii=False) if old else None, json.dumps(current, ensure_ascii=False), now),
        )
    return current, action


def delete_place(con, place_id):
    old = get_place(con, place_id)
    if not old:
        return False
    con.execute("DELETE FROM places WHERE id=?", (place_id,))
    con.execute(
        "INSERT INTO changes(place_id,action,prior_json,current_json,changed_at) VALUES(?,?,?,?,?)",
        (place_id, "delete", json.dumps(old, ensure_ascii=False), None, utcnow()),
    )
    return True


def add_memory(con, place_id, payload):
    if not get_place(con, place_id):
        raise LookupError("Không tìm thấy địa điểm")
    if not isinstance(payload, dict):
        raise ValueError("Bộ nhớ phải là một JSON object")
    kind = str(payload.get("kind") or "note").strip()[:80]
    content = str(payload.get("content") or "").strip()
    source = str(payload.get("source") or "user").strip()[:240]
    try:
        confidence = float(payload.get("confidence", 1))
    except (ValueError, TypeError):
        raise ValueError("Độ tin cậy không hợp lệ") from None
    if not content or len(content) > 10000:
        raise ValueError("Nội dung bộ nhớ phải từ 1-10000 ký tự")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Độ tin cậy phải từ 0 đến 1")
    observed_at = str(payload.get("observed_at") or "").strip()[:80] or None
    mid = str(uuid.uuid4())
    now = utcnow()
    con.execute(
        "INSERT INTO memory_records(id,place_id,kind,content,source,confidence,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (mid, place_id, kind, content, source, confidence, observed_at, now),
    )
    return dict(con.execute("SELECT * FROM memory_records WHERE id=?", (mid,)).fetchone())


def add_media(con, place_id, payload):
    if not get_place(con, place_id):
        raise LookupError("Không tìm thấy địa điểm")
    if not isinstance(payload, dict):
        raise ValueError("Thông tin ảnh phải là JSON object")
    url = str(payload.get("url") or "").strip()
    rights = str(payload.get("rights_basis") or "").strip()[:500]
    source = str(payload.get("source") or "").strip()[:240]
    caption = str(payload.get("caption") or "").strip()[:500]
    if not url.startswith("https://") or len(url) > 3000:
        raise ValueError("URL ảnh bắt buộc HTTPS")
    if not rights or not source:
        raise ValueError("Ảnh cần nguồn và căn cứ quyền sử dụng")
    mid = str(uuid.uuid4())
    now = utcnow()
    con.execute(
        "INSERT INTO place_media(id,place_id,url,caption,source,rights_basis,created_at) VALUES(?,?,?,?,?,?,?)",
        (mid, place_id, url, caption, source, rights, now),
    )
    return dict(con.execute("SELECT * FROM place_media WHERE id=?", (mid,)).fetchone())


def list_places(con, search="", province="", bbox=None, limit=500, offset=0):
    where = []
    args = []
    join = ""
    if province:
        where.append("places.province = ?")
        args.append(province)
    if search:
        tokens = re.findall(r"[\w]+", normalize(search))[:8]
        fts = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='places_fts'").fetchone()
        if tokens and fts:
            join = " JOIN places_fts ON places.rowid=places_fts.rowid"
            where.append("places_fts MATCH ?")
            args.append(" ".join('"' + t + '"*' for t in tokens))
        else:
            for token in tokens:
                where.append("places.search_text LIKE ?")
                args.append("%" + token + "%")
    if bbox:
        w, s, e, n = bbox
        if w <= e:
            where.append("places.lon BETWEEN ? AND ?")
            args.extend((w, e))
        else:
            where.append("(places.lon >= ? OR places.lon <= ?)")
            args.extend((w, e))
        where.append("places.lat BETWEEN ? AND ?")
        args.extend((s, n))
    base = " FROM places" + join
    predicate = (" WHERE " + " AND ".join(where)) if where else ""
    count = con.execute("SELECT COUNT(*)" + base + predicate, args).fetchone()[0]
    rows = con.execute(
        "SELECT places.*" + base + predicate + " ORDER BY places.updated_at DESC, places.id LIMIT ? OFFSET ?", [*args, limit, offset]
    ).fetchall()
    return [as_dict(r) for r in rows], count


def stats(con):
    total = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    by_status = {r["status"]: r["n"] for r in con.execute("SELECT status,COUNT(*) n FROM places GROUP BY status")}
    province_count = con.execute("SELECT COUNT(DISTINCT province) FROM places WHERE province != ''").fetchone()[0]
    updates = con.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    return {"total_places": total, "provinces_with_places": province_count, "history_events": updates, "by_status": by_status}


def geometry_feature(p):
    attrs = {k: v for k, v in p.items() if k not in ("lat", "lon", "search_text")}
    return {"type": "Feature", "id": p["id"], "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": attrs}


def feature_to_payload(f):
    if f.get("type") != "Feature" or (f.get("geometry") or {}).get("type") != "Point":
        raise ValueError("Chỉ hỗ trợ GeoJSON Point")
    coords = f["geometry"].get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ValueError("GeoJSON thiếu tọa độ")
    props = dict(f.get("properties") or {})
    props["lon"], props["lat"] = coords[0], coords[1]
    if "id" not in props and f.get("id") is not None:
        props["id"] = str(f["id"])
    return props
