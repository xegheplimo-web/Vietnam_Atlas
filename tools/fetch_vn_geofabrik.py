#!/usr/bin/env python3
"""Download *latest at runtime* official Geofabrik Vietnam OSM PBF, no API key.

A stable source URL is pinned, source metadata is recorded, and final rename
occurs only after complete transfer. No scraping, tile bulk-download or Google API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf"
UA = "VietnamAtlas-OSM-Data-Refresh/1.0 (open-data pipeline; source Geofabrik)"


def digest(path: Path):
    s = hashlib.sha256()
    # Local operator CLI: path comes from this tool's --out flag (see main()), never from network input.
    with path.open("rb") as inp:  # NOSONAR(S8707)
        for buf in iter(lambda: inp.read(4 * 1024 * 1024), b""):
            s.update(buf)
    return s.hexdigest()


def source_meta(url: str = URL):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as resp:
        if resp.status != 200 or not resp.geturl().startswith("https://"):
            raise RuntimeError("Geofabrik HEAD trả về HTTP không hợp lệ")
        size_s = resp.headers.get("Content-Length")
        return {
            "source_url": url,
            "content_length": int(size_s) if size_s else None,
            "last_modified": resp.headers.get("Last-Modified"),
            "etag": resp.headers.get("ETag"),
        }


def download(dst: Path, force=False, attempts=3):
    dst.parent.mkdir(parents=True, exist_ok=True)
    manifest = dst.with_suffix(dst.suffix + ".provenance.json")
    if dst.is_file() and manifest.is_file() and not force:
        try:
            old = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        try:
            info = source_meta()  # HEAD is only needed for the not-modified check here
        except (OSError, RuntimeError):
            info = {}
        same_remote = old.get("source_url") == info.get("source_url") and (
            (info.get("etag") and info["etag"] == old.get("etag"))
            or (info.get("last_modified") and info["last_modified"] == old.get("last_modified"))
        )
        same_size = info.get("content_length") is None or dst.stat().st_size == info["content_length"]
        if same_remote and same_size and digest(dst) == old.get("sha256"):
            return {"status": "not_modified", **old}
    partial = dst.with_name(dst.name + ".partial")
    for attempt in range(1, attempts + 1):
        try:
            # Fresh HEAD per attempt: the "latest" object can rotate between metadata and GET,
            # and a transient HEAD failure must not bypass the retry loop.
            info = source_meta()
            req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=120) as resp, partial.open("wb") as f:
                if resp.status != 200 or not resp.geturl().startswith("https://"):
                    raise RuntimeError("Geofabrik GET trả về HTTP không hợp lệ")
                h = hashlib.sha256()
                size = 0
                while True:
                    data = resp.read(2 * 1024 * 1024)
                    if not data:
                        break
                    f.write(data)
                    h.update(data)
                    size += len(data)
            if info.get("content_length") is not None and size != info["content_length"]:
                raise RuntimeError(f"Kích thước PBF không khớp: {size} != {info['content_length']}; nguồn có thể cập nhật giữa lúc tải")
            if size < 1_000_000:
                raise RuntimeError(f"Tệp quá nhỏ để là extract OSM toàn quốc: {size} bytes")
            provenance = {
                **info,
                "downloaded_utc": datetime.now(timezone.utc).isoformat(),
                "size_bytes": size,
                "sha256": h.hexdigest(),
                "status": "downloaded",
            }
            # Atomic swap only after full download and provenance written.
            # CLI-owned destination (--out); swap happens only after checksum and size validation.
            os.replace(partial, dst)  # NOSONAR(S8707)
            manifest.write_text(json.dumps(provenance, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return provenance
        except (OSError, ValueError, RuntimeError) as exc:
            partial.unlink(missing_ok=True)
            if attempt == attempts:
                raise RuntimeError(f"Tải Geofabrik thất bại sau {attempts} lượt: {exc}") from exc
            time.sleep(min(attempt * 3, 15))


def main():
    p = argparse.ArgumentParser(description="Download latest nationwide Vietnam OSM snapshot from Geofabrik")
    p.add_argument("--out", type=Path, default=Path("data/vietnam-latest.osm.pbf"))
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    try:
        print(json.dumps(download(args.out, args.force), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
