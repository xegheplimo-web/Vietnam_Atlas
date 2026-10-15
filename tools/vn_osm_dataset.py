#!/usr/bin/env python3
"""Vietnam Atlas: independent, attributable OSM place dataset (not a company registry).

Full Vietnam PBF: optional pyosmium. Tiny .osm XML fixtures: stdlib parser.
No commercial API key, Google scrape, or copied Google Maps content.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSE = "ODbL-1.0"
ORIGIN = "https://www.openstreetmap.org/copyright"
CATEGORIES = ("shop", "amenity", "office", "craft", "tourism", "healthcare", "leisure")
NAME_VI = "name:vi"  # alternate Vietnamese name tag, used when "name" is absent
SQL_ACTIVE_COUNT = "SELECT count(*) FROM osm_places WHERE is_active=1"


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def coordinates_valid(lat: float, lon: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


def centroid(coords: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Approximate polygon centroid from lon/lat; map locator, NOT legal GIS boundary."""
    if not coords:
        return None
    clean = [(float(x), float(y)) for x, y in coords if coordinates_valid(float(y), float(x))]
    if not clean:
        return None
    if len(clean) < 3:
        return (sum(y for x, y in clean) / len(clean), sum(x for x, y in clean) / len(clean))
    pts = clean if clean[0] == clean[-1] else [*clean, clean[0]]
    double_a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in itertools.pairwise(pts):
        k = x1 * y2 - x2 * y1
        double_a += k
        cx += (x1 + x2) * k
        cy += (y1 + y2) * k
    if abs(double_a) < 1e-10:
        return (sum(y for x, y in clean) / len(clean), sum(x for x, y in clean) / len(clean))
    return cy / (3 * double_a), cx / (3 * double_a)


def ring_area(pts: list[tuple[float, float]]) -> float:
    """Absolute planar area (shoelace) used only to rank outer rings for a map locator, not legal GIS area."""
    closed = [*pts, pts[0]]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in itertools.pairwise(closed))) / 2


def locator(coords: list[tuple[float, float]]) -> tuple[float, float] | None:
    p = centroid(coords)
    return p if p is not None and coordinates_valid(*p) else None


def reject_reason(tags: dict, location: tuple[float, float] | None) -> str | None:
    """Why record() drops an object (None = accepted); same check order as record()."""
    if location is None or not coordinates_valid(*location):
        return "missing_or_invalid_coordinates"
    if not str(tags.get("name") or tags.get(NAME_VI) or "").strip():
        return "missing_name"
    if not any(tags.get(k) for k in CATEGORIES):
        return "unsupported_category"
    return None


def record(kind: str, ident: int | str, tags: dict, location: tuple[float, float] | None):
    if reject_reason(tags, location) is not None:
        return None
    name = str(tags.get("name") or tags.get(NAME_VI) or "").strip()
    category = next((f"{k}:{tags[k]}" for k in CATEGORIES if tags.get(k)), None)
    lat, lon = location
    addr = " ".join(
        filter(None, [tags.get("addr:housenumber", ""), tags.get("addr:street", ""), tags.get("addr:ward", ""), tags.get("addr:city", "")])
    ).strip()
    phone = tags.get("contact:phone") or tags.get("phone") or ""
    website = tags.get("contact:website") or tags.get("website") or ""
    reduced = {str(k): str(v) for k, v in tags.items() if isinstance(k, str) and isinstance(v, str)}
    row = {
        "osm_type": str(kind),
        "osm_id": int(ident),
        "name": name[:1000],
        "category": category[:1000],
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "address": addr[:1000],
        "phone": str(phone)[:1000],
        "website": str(website)[:1000],
        "tags_json": json.dumps(reduced, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    row["payload_hash"] = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return row


def open_database(db: Path):
    db.parent.mkdir(parents=True, exist_ok=True)
    # --db path comes from this tool's own CLI flag (local operator run), never from a remote caller.
    con = sqlite3.connect(str(db), timeout=45)  # NOSONAR(S8706)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=45000")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS osm_places (
            osm_type TEXT NOT NULL CHECK(osm_type IN ('node','way','relation')),
            osm_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            address TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            source_licence TEXT NOT NULL DEFAULT 'ODbL-1.0',
            source_url TEXT NOT NULL DEFAULT 'https://www.openstreetmap.org/copyright',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_snapshot TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(osm_type, osm_id)
        );
        CREATE INDEX IF NOT EXISTS idx_osm_places_geo ON osm_places(lat, lon);
        CREATE INDEX IF NOT EXISTS idx_osm_places_category ON osm_places(category);
        CREATE INDEX IF NOT EXISTS idx_osm_places_active ON osm_places(is_active);
        CREATE TABLE IF NOT EXISTS ingestion_snapshots (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, file_sha256 TEXT NOT NULL,
            mode TEXT NOT NULL, source_date TEXT, imported_at TEXT NOT NULL,
            inserted INTEGER NOT NULL, updated INTEGER NOT NULL, unchanged INTEGER NOT NULL,
            skipped INTEGER NOT NULL, retired INTEGER NOT NULL DEFAULT 0,
            licence TEXT NOT NULL DEFAULT 'ODbL-1.0'
        );
    """)
    con.commit()
    return con


class Collector:
    def __init__(self, con, run_id: str, max_poi: int = 0):
        self.con = con
        self.run_id = run_id
        self.max_poi = max_poi
        self.counts = Counter()
        self.skip_reasons = Counter()  # reason -> count for every rejected object
        self.seen_keys = set() if max_poi else None  # only in a bounded pilot
        self.timestamp = now_utc()

    def add(self, kind, ident, tags, loc):
        if self.max_poi and (self.counts["inserted"] + self.counts["updated"] + self.counts["unchanged"]) >= self.max_poi:
            return
        d = record(kind, ident, tags, loc)
        if d is None:
            self.counts["skipped"] += 1
            self.skip_reasons[reject_reason(tags, loc) or "unknown"] += 1
            return
        key = (d["osm_type"], d["osm_id"])
        if self.seen_keys is not None:
            if key in self.seen_keys:
                return
            self.seen_keys.add(key)
        old = self.con.execute("SELECT payload_hash,is_active FROM osm_places WHERE osm_type=? AND osm_id=?", key).fetchone()
        if not old:
            action = "inserted"
        elif old["payload_hash"] == d["payload_hash"] and old["is_active"]:
            action = "unchanged"
        else:
            action = "updated"
        self.con.execute(
            """
          INSERT INTO osm_places(osm_type,osm_id,name,category,lat,lon,address,phone,website,tags_json,
                                 payload_hash,source_licence,source_url,first_seen,last_seen,last_snapshot,is_active)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
          ON CONFLICT(osm_type,osm_id) DO UPDATE SET
            name=excluded.name,category=excluded.category,lat=excluded.lat,lon=excluded.lon,
            address=excluded.address,phone=excluded.phone,website=excluded.website,
            tags_json=excluded.tags_json,payload_hash=excluded.payload_hash,
            last_seen=excluded.last_seen,last_snapshot=excluded.last_snapshot,is_active=1
        """,
            (
                *[d[k] for k in ("osm_type", "osm_id", "name", "category", "lat", "lon", "address", "phone", "website", "tags_json", "payload_hash")],
                LICENSE,
                ORIGIN,
                self.timestamp,
                self.timestamp,
                self.run_id,
            ),
        )
        self.counts[action] += 1


def import_small_osm_xml(path: Path, collector: Collector):
    """Small XML test/pilot only; full national PBF requires Pyosmium."""
    if path.stat().st_size > 40_000_000:
        raise ValueError("XML lớn hơn 40MB: dùng PBF + pyosmium thay vì bộ đọc XML thử nghiệm")
    node_coords = {}
    way_coords = {}
    for _event, elem in ET.iterparse(str(path), events=("end",)):
        typ = elem.tag
        if typ == "node":
            try:
                lat, lon = float(elem.attrib["lat"]), float(elem.attrib["lon"])
            except (KeyError, ValueError):
                collector.skip_reasons["missing_or_invalid_coordinates"] += 1
                elem.clear()
                continue
            nid = int(elem.attrib["id"])
            node_coords[nid] = (lon, lat)
            tags = {e.attrib["k"]: e.attrib["v"] for e in elem.findall("tag") if "k" in e.attrib and "v" in e.attrib}
            collector.add("node", nid, tags, (lat, lon))
        elif typ == "way":
            ident = int(elem.attrib["id"])
            coords = [node_coords[int(nd.attrib["ref"])] for nd in elem.findall("nd") if int(nd.attrib["ref"]) in node_coords]
            way_coords[ident] = coords
            tags = {e.attrib["k"]: e.attrib["v"] for e in elem.findall("tag") if "k" in e.attrib and "v" in e.attrib}
            collector.add("way", ident, tags, locator(coords))
        elif typ == "relation":
            tags = {e.attrib["k"]: e.attrib["v"] for e in elem.findall("tag") if "k" in e.attrib and "v" in e.attrib}
            # Non-area relations do not map to a single reliable point.
            if tags.get("type") in ("multipolygon", "boundary"):
                coords = []
                for m in elem.findall("member"):
                    if m.attrib.get("type") == "way" and m.attrib.get("role", "outer") in ("", "outer"):
                        coords.extend(way_coords.get(int(m.attrib["ref"]), []))
                collector.add("relation", int(elem.attrib["id"]), tags, locator(coords))
            else:
                collector.skip_reasons["non_area_relation"] += 1
        if typ in ("node", "way", "relation"):
            elem.clear()


def import_national_pbf(path: Path, collector: Collector):
    try:
        import osmium
    except ImportError as e:
        raise RuntimeError("Thiếu pyosmium: trên máy có Internet chạy: py -m pip install osmium") from e

    class Handler(osmium.SimpleHandler):
        def node(self, node):
            try:
                tags = dict(node.tags)
                if not node.location.valid():
                    collector.skip_reasons["missing_or_invalid_coordinates"] += 1
                    return
                if "name" not in tags and NAME_VI not in tags:
                    collector.skip_reasons["missing_name"] += 1
                    return
                if not any(k in tags for k in CATEGORIES):
                    collector.skip_reasons["unsupported_category"] += 1
                    return
                collector.add("node", node.id, tags, (node.location.lat, node.location.lon))
            except (ValueError, TypeError, RuntimeError):
                collector.counts["invalid_geometry"] += 1

        def way(self, way):
            try:
                tags = dict(way.tags)
                if "name" not in tags and NAME_VI not in tags:
                    collector.skip_reasons["missing_name"] += 1
                    return
                if not any(k in tags for k in CATEGORIES):
                    collector.skip_reasons["unsupported_category"] += 1
                    return
                pts = [(ref.location.lon, ref.location.lat) for ref in way.nodes if ref.location.valid()]
                if not pts or len(pts) < len(way.nodes):
                    collector.counts["invalid_geometry"] += 1
                    return
                collector.add("way", way.id, tags, locator(pts))
            except (ValueError, TypeError, RuntimeError):
                collector.counts["invalid_geometry"] += 1

        def area(self, area):
            try:
                if area.from_way():
                    return  # already handled as a way
                tags = dict(area.tags)
                if "name" not in tags and NAME_VI not in tags:
                    collector.skip_reasons["missing_name"] += 1
                    return
                if not any(k in tags for k in CATEGORIES):
                    collector.skip_reasons["unsupported_category"] += 1
                    return
                all_rings = []
                for outer in area.outer_rings():
                    pts = [(ref.location.lon, ref.location.lat) for ref in outer if ref.location.valid()]
                    if pts:
                        all_rings.append(pts)
                if not all_rings:
                    collector.counts["invalid_geometry"] += 1
                    return
                # Choose the largest outer ring by planar area for the map locator; never use this as precise polygon data.
                pts = max(all_rings, key=ring_area)
                collector.add("relation", area.orig_id(), tags, locator(pts))
            except (ValueError, TypeError, RuntimeError):
                collector.counts["invalid_geometry"] += 1

    handler = Handler()
    # PBF area callback assembles multipolygons, including relation POIs.
    # `locations=True` resolves way node coordinates, with extra memory cost.
    handler.apply_file(str(path), locations=True, idx="flex_mem")


def create_snapshot(source: Path, database: Path, max_poi=0, mark_missing=False, force=False, source_date=""):
    if source is None:
        raise ValueError("Cần truyền đường dẫn PBF/XML hoặc dùng --stats")
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    ext = source.name.lower()
    if ext.endswith((".osm.pbf", ".pbf")):
        mode = "pbf"
    elif ext.endswith((".osm", ".osm.xml")):
        mode = "xml"
    else:
        mode = None
    if not mode:
        raise ValueError("Chỉ hỗ trợ .osm.pbf hoặc .osm (XML nhỏ)")
    if max_poi < 0:
        raise ValueError("--limit không thể âm")
    if mark_missing and max_poi:
        raise ValueError("Cấm --mark-missing khi nhập thử có --limit")
    digest = sha256_file(source)
    scope = f"{mode}:full:retire={int(mark_missing)}" if not max_poi else f"{mode}:limit={max_poi}"
    key = hashlib.sha256((digest + "|" + scope).encode()).hexdigest()
    con = open_database(database)
    try:
        if not force and con.execute("SELECT 1 FROM ingestion_snapshots WHERE id=?", (key,)).fetchone():
            return {"status": "unchanged_snapshot_skipped", "file_sha256": digest, "snapshot_id": key, "skip_reasons": {}, "database": str(database)}
        collector = Collector(con, key, max_poi=max_poi)
        con.execute("BEGIN IMMEDIATE")
        try:
            if mode == "pbf":
                import_national_pbf(source, collector)
            else:
                import_small_osm_xml(source, collector)
            retired = 0
            if mark_missing:
                # Never mass-inactivate on a corrupt or incomplete source. Guard large DBs
                # unless a human inspects the unexpected discrepancy first.
                prior_active = con.execute(SQL_ACTIVE_COUNT).fetchone()[0]
                missing = con.execute(SQL_ACTIVE_COUNT + " AND last_snapshot<>?", (key,)).fetchone()[0]
                if missing and prior_active >= 1000 and missing > prior_active * 0.10:
                    raise RuntimeError(
                        f"Từ chối tự đánh dấu {missing}/{prior_active} POI biến mất (>10%); kiểm tra PBF, file nguồn, parser và chạy audit trước."
                    )
                retired = con.execute("UPDATE osm_places SET is_active=0 WHERE is_active=1 AND last_snapshot<>?", (key,)).rowcount
            con.execute(
                """INSERT OR REPLACE INTO ingestion_snapshots
                 (id,filename,file_sha256,mode,source_date,imported_at,inserted,updated,unchanged,skipped,retired)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key,
                    source.name,
                    digest,
                    scope,
                    source_date,
                    now_utc(),
                    collector.counts["inserted"],
                    collector.counts["updated"],
                    collector.counts["unchanged"],
                    collector.counts["skipped"],
                    retired,
                ),
            )
            con.commit()
        except BaseException:
            con.rollback()
            raise
        skip_reasons = dict(collector.skip_reasons)
        if collector.counts["invalid_geometry"]:
            skip_reasons["invalid_geometry"] = collector.counts["invalid_geometry"]
        return {
            "status": "imported",
            "file_sha256": digest,
            "snapshot_id": key,
            "inserted": collector.counts["inserted"],
            "updated": collector.counts["updated"],
            "unchanged": collector.counts["unchanged"],
            "skipped": collector.counts["skipped"],
            "skip_reasons": skip_reasons,
            "invalid_geometry": collector.counts["invalid_geometry"],
            "retired": retired,
            "active": con.execute(SQL_ACTIVE_COUNT).fetchone()[0],
            "database": str(database),
            "licence": LICENSE,
        }
    finally:
        con.close()


def stats(db: Path):
    con = open_database(db)
    try:
        return {
            "database": str(db),
            "active": con.execute(SQL_ACTIVE_COUNT).fetchone()[0],
            "inactive": con.execute("SELECT count(*) FROM osm_places WHERE is_active=0").fetchone()[0],
            "node": con.execute("SELECT count(*) FROM osm_places WHERE osm_type='node'").fetchone()[0],
            "way": con.execute("SELECT count(*) FROM osm_places WHERE osm_type='way'").fetchone()[0],
            "relation": con.execute("SELECT count(*) FROM osm_places WHERE osm_type='relation'").fetchone()[0],
            "snapshots": con.execute("SELECT count(*) FROM ingestion_snapshots").fetchone()[0],
            "licence": LICENSE,
        }
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(description="Build independent ODbL Vietnam OSM POI database for Vietnam Atlas")
    p.add_argument("input", nargs="?", type=Path, help="Geofabrik Vietnam .osm.pbf (or small .osm XML for testing)")
    p.add_argument("--db", type=Path, default=Path("data/osm_vietnam.sqlite3"))
    p.add_argument("--limit", type=int, default=0, help="POI pilot limit; 0=whole input")
    p.add_argument("--mark-missing", action="store_true", help="with full input only: mark disappeared OSM objects inactive, never delete")
    p.add_argument("--force", action="store_true", help="re-process unchanged input (usually not needed)")
    p.add_argument("--source-date", default="", help="source upstream date, informational")
    p.add_argument("--stats", action="store_true")
    a = p.parse_args()
    try:
        out = stats(a.db) if a.stats else create_snapshot(a.input, a.db, a.limit, a.mark_missing, a.force, a.source_date)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, ET.ParseError) as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
