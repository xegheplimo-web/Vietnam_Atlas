#!/usr/bin/env python3
"""Tải polygon ranh giới hành chính 34 tỉnh/thành và merge thành một GeoJSON duy nhất.

Nguồn: repo GitHub thanglequoc/vietnamese-provinces-database (license MIT —
Copyright (c) 2021 Thang Le Quoc); polygon sinh từ dữ liệu Cục Thống kê (GSO)
Việt Nam, dataset v5.2.0 (Nghị quyết 388/NQ-UBTVQH16).

Output (trong --out-dir, mặc định data/provinces):
  - provinces_34.geojson : FeatureCollection 34 features MultiPolygon, mỗi feature có
    properties {code, name, name_en, full_name, gso_code, area_km2, source_folder}.
    `code` = mã tỉnh chính thống 01..34 (trường `matinh` trong dataset nguồn — mã mới
    theo NQ 388/NQ-UBTVQH16; LƯU Ý: matinh không theo thứ tự folder, vd folder
    2_tinh_cao_bang mang matinh=7); `gso_code` = mã GSO cũ trong JSON nguồn
    (vd "01","04",...,"96") để join dữ liệu GSO; `source_folder` giữ traceability.
  - PROVENANCE.json     : nguồn, license, decree, sha256 + bytes từng file nguồn.
  - .cache/             : cache bytes từng file nguồn, KHÔNG commit; dùng để bỏ qua
    lần tải sau khi sha256 khớp PROVENANCE.json (idempotent).

Stdlib only (urllib/json/hashlib). Không bịa dữ liệu: nguồn ngoài lỗi → dừng, báo file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "VietnamAtlas/2.2 (agent@hermes.local)"
REPO = "thanglequoc/vietnamese-provinces-database"
BRANCH = "master"
BASE_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
LICENSE = "MIT"
LICENSE_NOTE = "Copyright (c) 2021 Thang Le Quoc — polygon sinh từ dữ liệu Cục Thống kê (GSO) Việt Nam."

GEOJSON_DIR = "dataset-generation-scripts/resources/gis/geojson_11Mar2026"
UNITS_JSON = "json/full_json_generated_data_vn_units.json"
METADATA_JSON = "json/vn_provinces_metadata.json"
MERGED_NAME = "provinces_34.geojson"
PROVENANCE_NAME = "PROVENANCE.json"
CACHE_DIRNAME = ".cache"

# 34 folder tỉnh/thành trong dataset nguồn (kiểm chứng qua git trees API;
# KHÔNG derive tên có dấu từ folder — folder là ASCII thiếu dấu).
PROVINCE_FOLDERS = (
    "1_thu_đo_ha_noi",
    "2_tinh_cao_bang",
    "3_tinh_tuyen_quang",
    "4_tinh_lao_cai",
    "5_tinh_đien_bien",
    "6_tinh_lai_chau",
    "7_tinh_son_la",
    "8_tinh_thai_nguyen",
    "9_tinh_lang_son",
    "10_tinh_quang_ninh",
    "11_tinh_phu_tho",
    "12_tinh_bac_ninh",
    "13_hai_phong",
    "14_tinh_hung_yen",
    "15_tinh_ninh_binh",
    "16_tinh_thanh_hoa",
    "17_tinh_nghe_an",
    "18_tinh_ha_tinh",
    "19_tinh_quang_tri",
    "20_hue",
    "21_đa_nang",
    "22_tinh_quang_ngai",
    "23_tinh_khanh_hoa",
    "24_tinh_gia_lai",
    "25_tinh_đak_lak",
    "26_tinh_lam_đong",
    "27_tinh_tay_ninh",
    "28_tinh_đong_nai",
    "29_ho_chi_minh",
    "30_tinh_vinh_long",
    "31_tinh_đong_thap",
    "32_tinh_an_giang",
    "33_can_tho",
    "34_tinh_ca_mau",
)

# Bán hành trung bình (km/độ) để ước lượng diện tích tọa độ trên mặt phẳng địa phương.
KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON = 111.320

# Kiểm tra thô tọa độ ngoài vùng Việt Nam (kể cả quần đảo xa) → dừng, chứ không bịa.
SANITY_LON = (100.0, 120.0)
SANITY_LAT = (5.0, 25.0)


def normalize(text):
    """Chuẩn hoá tiếng Việt: lowercase, đ→d, bỏ dấu (giống server.store.normalize)."""
    s = str(text or "").lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def tokens(text):
    # [^\W_]+: tách cả trên "_" (slug folder nguồn dùng "_" thay khoảng trắng).
    return tuple(re.findall(r"[^\W_]+", normalize(text)))


def is_sublist(small, big):
    """True nếu `small` xuất hiện liên tiếp (ranh giới từ/cụm) trong `big`."""
    n = len(small)
    if n == 0 or n > len(big):
        return False
    return any(tuple(big[i : i + n]) == small for i in range(len(big) - n + 1))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_url(repo_path: str) -> str:
    # URL-encode ký tự đ trong path (vd 1_thu_đo_ha_noi), giữ nguyên dấu "/".
    return f"{BASE_URL}/{urllib.parse.quote(repo_path, safe='/')}"


def fetch_url(repo_path: str, retries: int = 3, timeout: int = 60) -> bytes:
    """Tải 1 file nguồn với User-Agent, retry 3 lần backoff; HTTP 4xx (trừ 429) dừng ngay."""
    url = raw_url(repo_path)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"HTTP {exc.code} khi tải {repo_path}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(2**attempt)  # backoff 2s, 4s
    raise RuntimeError(f"tải thất bại sau {retries} lần: {repo_path} ({last_error})")


def cache_path(out_dir: Path, source_path: str, sha: str) -> Path:
    return out_dir / CACHE_DIRNAME / (sha + Path(source_path).suffix)


def load_provenance(out_dir: Path) -> dict:
    path = out_dir / PROVENANCE_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def collect_sources(out_dir: Path, force: bool, log) -> list[tuple[str, bytes]]:
    """Đảm bảo đủ bytes cho 36 file nguồn (34 province + units + metadata).

    Idempotent: cache tồn tại + sha256 khớp PROVENANCE.json → skip tải.
    """
    expected = [f"{GEOJSON_DIR}/{folder}/province.geojson" for folder in PROVINCE_FOLDERS]
    expected += [UNITS_JSON, METADATA_JSON]
    provenance = {} if force else load_provenance(out_dir)
    known_sha = {f["source_path"]: f["sha256"] for f in provenance.get("files", []) if "source_path" in f}
    cache_dir = out_dir / CACHE_DIRNAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    sources: list[tuple[str, bytes]] = []
    for source_path in expected:
        cached = cache_path(out_dir, source_path, known_sha[source_path]) if source_path in known_sha else None
        if cached is not None and cached.exists():
            data = cached.read_bytes()
            if sha256_hex(data) == known_sha[source_path]:
                log(f"  skip  {source_path} (cache khớp sha256)")
                sources.append((source_path, data))
                continue
        data = fetch_url(source_path)
        cache_path(out_dir, source_path, sha256_hex(data)).write_bytes(data)
        log(f"  tải  {source_path} ({len(data):,} bytes)")
        sources.append((source_path, data))
    return sources


def match_unit(folder: str, units: list[dict]) -> dict:
    """Ghép folder nguồn → phần tử full_json bằng tên chuẩn có dấu.

    Folder là ASCII thiếu dấu ("1_thu_đo_ha_noi"), JSON mang tên thật ("Hà Nội") —
    khớp khi token tên tỉnh xuất hiện liên tiếp trong token folder; phải đúng 1 kết quả.
    """
    folder_tokens = tokens(folder)
    matches = [u for u in units if is_sublist(tokens(u.get("Name", "")), folder_tokens)]
    if len(matches) != 1:
        raise RuntimeError(f"{folder}: không ghép được tên tỉnh trong full_json ({len(matches)} ứng viên)")
    return matches[0]


def ring_area_km2(ring: list[tuple[float, float]]) -> float:
    """Shoelace trên mặt phẳng địa phương, tỉ lệ cos(vĩ độ trung bình)."""
    if len(ring) < 3:
        return 0.0
    acc = 0.0
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        acc += x1 * y2 - x2 * y1
    mean_lat = sum(y for _, y in ring) / len(ring)
    return abs(acc) / 2.0 * KM_PER_DEG_LAT * KM_PER_DEG_LON * math.cos(math.radians(mean_lat))


def polygon_area_km2(rings: list[list[tuple[float, float]]]) -> float:
    """Diện tích polygon = vòng ngoài trừ các lỗ (holes)."""
    if not rings:
        return 0.0
    total = ring_area_km2(rings[0])
    for hole in rings[1:]:
        total -= ring_area_km2(hole)
    return max(total, 0.0)


def _parse_geometry(geom: dict, folder: str):
    gtype = geom.get("type")
    if gtype == "MultiPolygon":
        return geom["coordinates"]
    if gtype == "Polygon":
        return [geom["coordinates"]]
    raise RuntimeError(f"{folder}: geometry không hỗ trợ: {gtype}")


def _parse_ring(ring, folder: str) -> list[tuple[float, float]]:
    pts = [(float(c[0]), float(c[1])) for c in ring]
    if len(pts) < 3:
        raise RuntimeError(f"{folder}: ring ít hơn 3 điểm")
    for lon, lat in pts:
        if not (SANITY_LON[0] <= lon <= SANITY_LON[1] and SANITY_LAT[0] <= lat <= SANITY_LAT[1]):
            raise RuntimeError(f"{folder}: tọa độ ({lon},{lat}) ngoài vùng Việt Nam")
    return pts


def parse_province_geojson(data: bytes, folder: str):
    """Parse + validate 1 file province.geojson → (MultiPolygon [(lon,lat)], matinh)."""
    doc = json.loads(data.decode("utf-8"))
    if doc.get("type") != "FeatureCollection":
        raise RuntimeError(f"{folder}: không phải FeatureCollection")
    features = doc.get("features") or []
    if len(features) != 1:
        raise RuntimeError(f"{folder}: cần đúng 1 feature, thấy {len(features)}")
    polygons = _parse_geometry(features[0].get("geometry") or {}, folder)
    props = features[0].get("properties") or {}
    matinh = str(props.get("matinh") or "")
    if not matinh.isdigit():
        raise RuntimeError(f"{folder}: matinh không hợp lệ: {props.get('matinh')!r}")
    parsed = []
    for poly in polygons:
        if not isinstance(poly, list) or not poly:
            raise RuntimeError(f"{folder}: polygon rỗng")
        parsed.append([_parse_ring(ring, folder) for ring in poly])
    return parsed, matinh


def feature_bbox(polygons) -> tuple[float, float, float, float]:
    lons = [p[0] for poly in polygons for ring in poly for p in ring]
    lats = [p[1] for poly in polygons for ring in poly for p in ring]
    return (min(lons), min(lats), max(lons), max(lats))  # (west, south, east, north)


def build_merged(sources: list[tuple[str, bytes]]) -> tuple[list[dict], dict]:
    """Merge 34 file thành FeatureCollection + metadata nguồn cho PROVENANCE.json."""
    units = next(json.loads(data.decode("utf-8")) for path, data in sources if path == UNITS_JSON)
    metadata = next(json.loads(data.decode("utf-8")) for path, data in sources if path == METADATA_JSON)
    if not isinstance(units, list) or len(units) != len(PROVINCE_FOLDERS):
        raise RuntimeError(f"{UNITS_JSON}: cần {len(PROVINCE_FOLDERS)} phần tử")

    parsed = []
    for folder in PROVINCE_FOLDERS:
        source_path = f"{GEOJSON_DIR}/{folder}/province.geojson"
        data = next(d for p, d in sources if p == source_path)
        polygons, matinh = parse_province_geojson(data, folder)
        parsed.append((folder, match_unit(folder, units), polygons, matinh))

    # matinh là mã tỉnh chính thống 01..34 của dataset (không theo thứ tự folder) —
    # bắt buộc là hoán vị 1..34 để chắc chắn đủ 34 file, không trùng/thiếu.
    matinh_values = sorted(int(m) for _, _, _, m in parsed)
    if matinh_values != list(range(1, len(PROVINCE_FOLDERS) + 1)):
        raise RuntimeError(f"matinh không phải hoán vị 1..{len(PROVINCE_FOLDERS)}: {matinh_values}")

    features = []
    for folder, unit, polygons, matinh in parsed:
        code = f"{int(matinh):02d}"
        features.append(
            {
                "type": "Feature",
                "id": code,
                "geometry": {"type": "MultiPolygon", "coordinates": [[[list(p) for p in ring] for ring in poly] for poly in polygons]},
                "properties": {
                    "code": code,
                    "name": unit["Name"],
                    "name_en": unit["NameEn"],
                    "full_name": unit["FullName"],
                    "gso_code": unit["Code"],
                    "area_km2": round(sum(polygon_area_km2(poly) for poly in polygons), 1),
                    "source_folder": folder,
                },
            }
        )
    features.sort(key=lambda f: f["properties"]["code"])
    meta = {
        "dataset_version": metadata.get("DatasetVersion", ""),
        "decree": metadata.get("LatestDecree", ""),
        "generated_at": metadata.get("GeneratedAt", ""),
    }
    return features, meta


def cmd_fetch(out_dir: Path, force: bool, log) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)  # NOSONAR(S8707)
    sources = collect_sources(out_dir, force, log)
    features, meta = build_merged(sources)
    if len(features) != len(PROVINCE_FOLDERS):
        raise RuntimeError(f"cần {len(PROVINCE_FOLDERS)} features, thấy {len(features)}")

    merged = {"type": "FeatureCollection", "features": features}
    merged_text = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
    (out_dir / MERGED_NAME).write_text(merged_text, encoding="utf-8")

    file_entries = [{"source_path": path, "sha256": sha256_hex(data), "bytes": len(data)} for path, data in sources]
    provenance = {
        "source_repo": f"https://github.com/{REPO}",
        "source_url": raw_url(GEOJSON_DIR) + "/",
        "license": LICENSE,
        "license_note": LICENSE_NOTE,
        "decree": meta["decree"],
        "dataset_version": meta["dataset_version"],
        "generated_at": meta["generated_at"],
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merged_file": MERGED_NAME,
        "merged_sha256": sha256_hex(merged_text.encode("utf-8")),
        "merged_bytes": len(merged_text.encode("utf-8")),
        "files": file_entries,
    }
    (out_dir / PROVENANCE_NAME).write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"OK: {len(features)} features -> {out_dir / MERGED_NAME}")
    log(f"OK: provenance ({len(file_entries)} file nguồn) -> {out_dir / PROVENANCE_NAME}")
    return 0


def cmd_check(out_dir: Path, log) -> int:
    """Verify sha256 file đã tải (cache) + file merge vs PROVENANCE.json. exit≠0 nếu lệch/thiếu."""
    provenance = load_provenance(out_dir)
    if not provenance:
        log(f"FAIL: {out_dir / PROVENANCE_NAME} thiếu — chạy fetch trước")
        return 1
    problems = []
    for entry in provenance.get("files", []):
        cached = cache_path(out_dir, entry["source_path"], entry["sha256"])
        if not cached.exists():
            problems.append(f"thiếu cache: {entry['source_path']}")
            continue
        actual = sha256_hex(cached.read_bytes())
        if actual != entry["sha256"]:
            problems.append(f"lệch sha256: {entry['source_path']} (cache={actual[:12]}…, provenance={entry['sha256'][:12]}…)")
    merged_path = out_dir / provenance.get("merged_file", MERGED_NAME)
    if not merged_path.exists():
        problems.append(f"thiếu file merge: {merged_path.name}")
    elif sha256_hex(merged_path.read_bytes()) != provenance.get("merged_sha256"):
        problems.append("lệch sha256 file merge " + merged_path.name)
    if problems:
        for problem in problems:
            log(f"FAIL: {problem}")
        return 1
    log(f"OK: {len(provenance.get('files', []))} file nguồn + {MERGED_NAME} khớp sha256")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tải polygon 34 tỉnh (nguồn GSO qua repo MIT) → data/provinces.")
    parser.add_argument("--out-dir", default=str(ROOT / "data" / "provinces"), help="thường chứa output (mặc định: data/provinces)")
    parser.add_argument("--check", action="store_true", help="verify sha256 cache + file merge vs PROVENANCE.json, exit≠0 nếu lệch/thiếu")
    parser.add_argument("--force", action="store_true", help="tải lại toàn bộ dù cache khớp")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    def log(msg):
        print(msg)

    try:
        if args.check:
            return cmd_check(out_dir, log)
        return cmd_fetch(out_dir, args.force, log)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

# Bán hành trung bình (km/độ) để ước lượng diện tích tọa độ trên mặt phẳng địa phương.
KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON = 111.320

# Kiểm tra thô tọa độ ngoài vùng Việt Nam (kể cả quần đảo xa) → dừng, chứ không bịa.
SANITY_LON = (100.0, 120.0)
SANITY_LAT = (5.0, 25.0)
