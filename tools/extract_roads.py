"""Export OSM highway ways to bounded preview GeoJSON.

Requires python -m pip install osmium.
This reads a PBF and resolves way-node coordinates: memory use can be large.
It deliberately caps exported roads. Full-country rendering needs a tile pipeline.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import osmium
except ImportError:
    raise SystemExit("Cần cài python -m pip install osmium") from None


class Roads(osmium.SimpleHandler):
    def __init__(self, output, bbox, limit):
        super().__init__()
        self.output = output
        self.bbox = bbox
        self.limit = limit
        self.count = 0
        self.first = True

    def way(self, w):
        if self.count >= self.limit or "highway" not in w.tags:
            return
        try:
            coords = [[float(n.lon), float(n.lat)] for n in w.nodes if n.location.valid()]
        except (ValueError, TypeError, RuntimeError):
            return
        if len(coords) < 2 or len(coords) != len(w.nodes):
            return
        west, south, east, north = self.bbox
        xs = [x for x, y in coords]
        ys = [y for x, y in coords]
        if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
            return
        props = {
            "highway": w.tags.get("highway", ""),
            "name": w.tags.get("name", ""),
            "surface": w.tags.get("surface", ""),
            "lanes": w.tags.get("lanes", ""),
            "maxspeed": w.tags.get("maxspeed", ""),
            "source": "OpenStreetMap",
            "osm_id": "way/" + str(w.id),
        }
        feature = {"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": coords}}
        if not self.first:
            self.output.write(",\n")
        self.output.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")))
        self.first = False
        self.count += 1


def main():
    p = argparse.ArgumentParser(description="Extract a limited number of OSM roads from Vietnam PBF")
    p.add_argument("pbf", help="Path to input .osm.pbf")
    p.add_argument("--bbox", required=True, help="west,south,east,north (WGS84), e.g. 105.7,20.9,106.1,21.15")
    p.add_argument("--out", default="data/roads-preview.geojson")
    p.add_argument("--limit", type=int, default=10000)
    args = p.parse_args()
    bbox = [float(v) for v in args.bbox.split(",")]
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or args.limit < 1:
        raise SystemExit("BBox hoặc giới hạn không hợp lệ")
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        output.write('{"type":"FeatureCollection","features":[\n')
        handler = Roads(output, bbox, args.limit)
        handler.apply_file(args.pbf, locations=True)
        output.write("\n]}\n")
    print(f"Wrote {handler.count} OSM road ways to {path}. Bounding box is approximate (not clipped).")


if __name__ == "__main__":
    main()
