"""Optional streaming importer for named OSM POI NODES from Geofabrik PBF.

Requires `pip install osmium`. Nodes only! Does NOT ingest way/relation POIs,
photographs, registered-company status, or full administrative boundary polygons.
Designed for large input with bounded in-memory buffers.

SUPERSEDED (2026-10-11): prefer tools/vn_osm_dataset.py + tools/sync_osm_into_atlas.py
for full node/way/relation ingestion with dedup, snapshots and dry-run sync.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.store import connect, init_db, save_place
from tools.vn_osm_dataset import compose_address

try:
    import osmium
except ImportError:
    raise SystemExit("Thiếu thư viện osmium. Cài: python -m pip install osmium") from None

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ("shop", "amenity", "tourism", "healthcare", "office", "craft", "leisure")


class Pois(osmium.SimpleHandler):
    def __init__(self, con, batch, limit):
        super().__init__()
        self.con, self.batch, self.limit = con, batch, limit
        self.seen = self.inserted = self.changed = self.rejected = 0
        self.pending = []

    def node(self, n):
        if self.limit and self.seen >= self.limit:
            return
        tags = n.tags
        if not n.location.valid() or "name" not in tags:
            return
        category = next((f"{key}:{tags[key]}" for key in CATEGORIES if key in tags), None)
        if not category:
            return
        self.seen += 1
        lat, lon = float(n.location.lat), float(n.location.lon)
        addr = compose_address(dict(n.tags))
        self.pending.append(
            {
                "name": tags["name"],
                "category": category,
                "address": addr,
                "phone": tags.get("contact:phone", tags.get("phone", "")),
                "website": tags.get("contact:website", tags.get("website", "")),
                "lat": lat,
                "lon": lon,
                "source": "OpenStreetMap",
                "source_id": f"node/{n.id}",
                "status": "unverified",
            }
        )
        if len(self.pending) >= self.batch:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        with self.con:
            for p in self.pending:
                try:
                    _row, action = save_place(self.con, p)
                    if action == "create":
                        self.inserted += 1
                    else:
                        self.changed += 1
                except (ValueError, TypeError):
                    self.rejected += 1
        self.pending.clear()


def main():
    ap = argparse.ArgumentParser(description="Import OSM named POI nodes into Atlas SQLite")
    ap.add_argument("pbf", help="Path to vietnam-latest.osm.pbf")
    ap.add_argument("--db", default=str(ROOT / "data" / "atlas.sqlite3"))
    ap.add_argument("--batch", type=int, default=1500)
    ap.add_argument("--limit", type=int, default=0, help="max matching POIs for testing, 0=all")
    args = ap.parse_args()
    if args.batch < 1:
        raise SystemExit("--batch must be positive")
    init_db(args.db)
    con = connect(args.db)
    try:
        handler = Pois(con, args.batch, args.limit)
        handler.apply_file(args.pbf, locations=False)  # nodes already include lat/lon
        handler.flush()
        print(f"DONE matched={handler.seen} inserted={handler.inserted} updated={handler.changed} rejected={handler.rejected}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
