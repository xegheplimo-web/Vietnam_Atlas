#!/usr/bin/env python3
"""Download the current Vietnam OpenStreetMap PBF extract from Geofabrik."""
from pathlib import Path
from urllib.request import urlopen, Request
import sys

URL = "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf"
DEST = Path(__file__).resolve().parents[1] / "data" / "vietnam-latest.osm.pbf"

def main():
    DEST.parent.mkdir(parents=True, exist_ok=True)
    req = Request(URL, headers={"User-Agent": "VietnamAtlasStarter/1.0 (manual OSM data download)"})
    try:
        with urlopen(req, timeout=60) as response, DEST.open("wb") as out:
            total = int(response.headers.get("Content-Length", "0") or 0)
            read = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                read += len(chunk)
                if total:
                    print(f"\rDownloaded {read/1024/1024:.1f}/{total/1024/1024:.1f} MiB", end="", flush=True)
                else:
                    print(f"\rDownloaded {read/1024/1024:.1f} MiB", end="", flush=True)
        print(f"\nSaved: {DEST}")
        print("Note: PBF is raw OSM data; it is not yet loaded into PostGIS or rendered offline.")
    except Exception as exc:
        if DEST.exists():
            DEST.unlink()
        print(f"Download failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
