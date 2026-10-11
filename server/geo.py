"""Module địa hành chính: point-in-polygon (PIP) trên polygon GSO 34 tỉnh/thành.

Stdlib only (json, math, re, unicodedata, dataclasses) — KHÔNG shapely/geopandas.
Dữ liệu: data/provinces/provinces_34.geojson (xem tools/fetch_province_polygons.py).

API FROZEN (các vòng sau — vd T2 geo-aware search — gọi đúng các hàm này):
  load_provinces(path=None) -> list[Province]   # lazy cache theo process
  province_at(lat, lon) -> Province | None      # bbox prefilter + ray-casting PIP
  province_bbox(code_or_name) -> (w, s, e, n) | None
  resolve_province(text) -> Province | None     # tên có dấu/không dấu + alias cũ→mới
  all_provinces() -> list[Province]

File dữ liệu thiếu/hỏng → load_provinces() trả [] + log 1 dòng (server không crash).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "provinces" / "provinces_34.geojson"
_MERGERS_FILE = Path(__file__).resolve().parents[1] / "data" / "province_mergers_2025.json"


def normalize(text):
    """Chuẩn hoá tiếng Việt: lowercase, đ→d, bỏ dấu.

    Giữ đồng bộ với server.store.normalize (copy pattern, KHÔNG import store để tránh
    vòng import khi store import geo).
    """
    s = str(text or "").lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def _tokens(text):
    # [^\W_]+: tách trên ký tự không-phải-chữ-và-số (kể cả "_"), giống ranh giới từ.
    return tuple(re.findall(r"[^\W_]+", normalize(text)))


@dataclass(frozen=True)
class Ring:
    """Một vòng kín (ring) của polygon; points = tuple các (lon, lat)."""

    points: tuple
    bbox: tuple  # (west, south, east, north)


@dataclass(frozen=True)
class Polygon:
    """Polygon đa giác: ring đầu là vòng ngoài, các ring sau là lỗ (holes)."""

    outer: Ring
    holes: tuple  # tuple[Ring, ...]


@dataclass(frozen=True)
class Province:
    code: str  # mã tỉnh chính thống "01".."34" (matinh trong dataset GSO)
    name: str  # "Hà Nội"
    name_norm: str  # "ha noi"
    full_name: str  # "Thành phố Hà Nội"
    bbox: tuple  # (west, south, east, north)
    area_km2: float
    polygons: tuple  # tuple[Polygon, ...] — MultiPolygon


def _make_ring(coords) -> Ring:
    points = tuple((float(c[0]), float(c[1])) for c in coords)
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return Ring(points=points, bbox=(min(lons), min(lats), max(lons), max(lats)))


def _load_provinces(path) -> list[Province]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("geo: không tìm thấy %s — trả về danh sách tỉnh rỗng", path)
        return []
    except (OSError, ValueError):
        logger.warning("geo: lỗi đọc %s — trả về danh sách tỉnh rỗng", path)
        return []
    provinces = []
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        polygons = []
        for rings in geometry.get("coordinates", []):
            polygons.append(Polygon(outer=_make_ring(rings[0]), holes=tuple(_make_ring(r) for r in rings[1:])))
        bbox = (
            min(p.outer.bbox[0] for p in polygons),
            min(p.outer.bbox[1] for p in polygons),
            max(p.outer.bbox[2] for p in polygons),
            max(p.outer.bbox[3] for p in polygons),
        )
        name = str(props.get("name") or "")
        provinces.append(
            Province(
                code=str(props.get("code") or ""),
                name=name,
                name_norm=normalize(name),
                full_name=str(props.get("full_name") or ""),
                bbox=bbox,
                area_km2=float(props.get("area_km2") or 0.0),
                polygons=tuple(polygons),
            )
        )
    return provinces


@lru_cache(maxsize=1)
def _provinces() -> tuple:
    return tuple(_load_provinces(_DATA_FILE))


def load_provinces(path=None) -> list[Province]:
    """Nạp 34 tỉnh từ GeoJSON (lazy cache theo process). File thiếu/hỏng → [] + log 1 dòng.

    path=None → dùng file mặc định data/provinces/provinces_34.geojson (có cache);
    path truyền tường minh → đọc trực tiếp, không đụng cache mặc định.
    """
    if path is not None:
        return _load_provinces(path)
    return list(_provinces())


def _ring_contains(ring: Ring, lon: float, lat: float) -> bool:
    """Ray-casting point-in-ring; điểm nằm trên biên tính là ngoài (strict)."""
    west, south, east, north = ring.bbox
    if not (south < lat < north and west < lon < east):
        return False
    points = ring.points
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > lat) != (yj > lat):
            x_cross = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _polygon_contains(poly: Polygon, lon: float, lat: float) -> bool:
    """Trong vòng ngoài VÀ không trong bất kỳ lỗ nào."""
    if not _ring_contains(poly.outer, lon, lat):
        return False
    return not any(_ring_contains(hole, lon, lat) for hole in poly.holes)


def province_at(lat: float, lon: float):
    """Tỉnh chứa điểm (lat, lon) — bbox prefilter + PIP; None nếu ngoài 34 tỉnh."""
    for province in _provinces():
        west, south, east, north = province.bbox
        if not (south <= lat <= north and west <= lon <= east):
            continue
        if any(_polygon_contains(poly, lon, lat) for poly in province.polygons):
            return province
    return None


def _is_subsequence(small, big) -> bool:
    """True nếu token `small` xuất hiện liên tiếp (ranh giới từ/cụm) trong `big`."""
    n = len(small)
    if n == 0 or n > len(big):
        return False
    return any(tuple(big[i : i + n]) == small for i in range(len(big) - n + 1))


@lru_cache(maxsize=1)
def _province_aliases() -> tuple:
    """Map (token alias cũ) → Province mới, từ data/province_mergers_2025.json (cached).

    Gồm historical_aliases (vd "sài gòn" → Hồ Chí Minh) và new_to_old (vd "bình dương"
    → Hồ Chí Minh). Tên mới trùng tên cũ (vd "Hà Nội" → Hà Nội) vẫn resolve đúng.
    """
    by_name = {}
    for province in _provinces():
        by_name.setdefault(_tokens(province.name_norm), province)
        by_name.setdefault(_tokens(province.full_name), province)
    try:
        data = json.loads(_MERGERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    aliases = []
    for old, new in (data.get("historical_aliases") or {}).items():
        target = by_name.get(_tokens(new))
        if target is not None:
            aliases.append((_tokens(old), target))
    for new, olds in (data.get("new_to_old") or {}).items():
        target = by_name.get(_tokens(new))
        if target is not None:
            for old in olds:
                aliases.append((_tokens(old), target))
    return tuple(aliases)


def resolve_province(text):
    """Resolve tên tỉnh (có dấu/không dấu/alias cũ) → Province; None nếu không khớp.

    Khớp theo ranh giới từ/cụm: token của tên tỉnh phải xuất hiện liên tiếp trong
    token truy vấn (vd "tp hồ chí minh" khớp "hồ chí minh"). Ưu tiên tên hiện hành
    (name/full_name, khớp dài nhất), sau đó alias cũ→mới từ province_mergers_2025.json.
    """
    if not text:
        return None
    query = _tokens(text)
    if not query:
        return None
    best = None
    for province in _provinces():
        for toks in (_tokens(province.name_norm), _tokens(province.full_name)):
            if _is_subsequence(toks, query) and (best is None or len(toks) > len(best[0])):
                best = (toks, province)
    if best is not None:
        return best[1]
    best_alias = None
    for alias_tokens, target in _province_aliases():
        if _is_subsequence(alias_tokens, query) and (best_alias is None or len(alias_tokens) > len(best_alias[0])):
            best_alias = (alias_tokens, target)
    return best_alias[1] if best_alias is not None else None


def province_bbox(code_or_name):
    """bbox (west, south, east, north) theo code ("01" hoặc "1") hoặc tên; None nếu không thấy."""
    text = str(code_or_name or "").strip()
    if not text:
        return None
    for province in _provinces():
        if province.code == text:
            return province.bbox
    if text.isdigit() and len(text) <= 2:
        padded = text.zfill(2)
        for province in _provinces():
            if province.code == padded:
                return province.bbox
    province = resolve_province(text)
    return province.bbox if province is not None else None


def all_provinces() -> list[Province]:
    """Danh sách 34 Province (thứ tự code 01..34)."""
    return list(_provinces())
