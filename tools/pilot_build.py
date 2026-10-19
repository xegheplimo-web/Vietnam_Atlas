#!/usr/bin/env python3
"""C3a pilot builder — 100 dev queries (85 random + 15 hard), deterministic per seed.

Two subcommands:

    build       --db <atlas> --gazetteer-db <gaz> --seed <s> --out <dir>
                → queries.json + evidence.jsonl + pilot_manifest.json
    worksheet   --run <run.json> --queries <queries.json> --out <worksheet.jsonl>
                → 1 dòng JSON/câu với top-10 từ adapter legacy-http

Corpus isolation (giống harness C2): atlas + gazetteer mở `<uri>?mode=ro` +
`PRAGMA query_only=ON`; SHA-256 của cả hai được assert trước/sau — build không
bao giờ ghi vào master.

Determinism: chỉ random.Random(seed); mọi pool sắp xếp/lọc deterministic trước khi
rng.sample/shuffle. `accessed_at`/`created_at` lấy từ `--now`; mặc định =
nửa đêm +07:00 của ngày mã hoá trong seed 8-chữ-số (vd 20261019 → 2026-10-19) —
vì vậy 2 lần chạy cùng seed cho output byte-identical kể cả không truyền --now.
Seed không phải ngày → fallback đồng hồ hệ thống (dùng --now để kiểm thử).

Panel/hard rules (card không định nghĩa "hard" — quy ước của builder):
  G1   tất cả random.
  G2   hard = biến thể (b) xoá ký tự giữa từ / (c) nhân đôi nguyên âm; random = (a) bỏ dấu.
  G3   hard = area cấp phường/xã (category hẹp); random = area cấp tỉnh.
  G4   hard = dạng lịch sử qua admin_alias; random = luân phiên phường/quận.
  G5   hard = landmark có mật độ ứng viên cao nhất trong pool đã duyệt.
  G6   5 template đầu = random; 2 câu tái dùng template (tỉnh khác) = hard.
  G7   hard = tên nằm ở nhiều tỉnh nhất trong các tên đã chọn.

Nhãn nhóm theo card C3a: G6 = no-match, G7 = ambiguous/nhiều chi nhánh (contract
E3A đánh nhãn ngược lại — giữ đúng card; hệ quả scorer ghi chú trong PR).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ_ICT = timezone(timedelta(hours=7), name="ICT")
GROUP_ORDER = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")

# Phân bổ chốt card C3a (random, hard) — không tự đổi.
QUOTAS = {
    "G1": (20, 0),
    "G2": (13, 3),
    "G3": (20, 3),
    "G4": (12, 3),
    "G5": (10, 2),
    "G6": (5, 2),
    "G7": (5, 2),
}

PERSONAS = {
    "G1": (("nguoi_dan", 12), ("du_khach", 8)),
    "G2": (("nguoi_dan", 16),),
    "G3": (("du_khach", 12), ("nguoi_dan", 11)),
    "G4": (("nguoi_dan", 15),),
    "G5": (("du_khach", 8), ("nguoi_dan", 4)),
    "G6": (("lao_dong", 4), ("nguoi_dan", 3)),
    "G7": (("du_khach", 4), ("nguoi_dan", 3)),
}

# Lexicon category → danh từ tiếng Việt cho G3/G4/G5 (card tham chiếu "lexicon"
# nhưng không đính kèm — đây là bảng nội bộ của builder).
CATEGORY_NOUN = {
    "amenity:restaurant": "quán ăn",
    "amenity:fast_food": "quán ăn nhanh",
    "amenity:cafe": "quán cà phê",
    "amenity:bar": "quán bar",
    "amenity:school": "trường học",
    "amenity:kindergarten": "trường mầm non",
    "amenity:university": "trường đại học",
    "amenity:college": "trường cao đẳng",
    "amenity:hospital": "bệnh viện",
    "amenity:clinic": "phòng khám",
    "amenity:pharmacy": "nhà thuốc",
    "amenity:marketplace": "chợ",
    "amenity:bank": "ngân hàng",
    "amenity:atm": "cây atm",
    "amenity:fuel": "trạm xăng",
    "amenity:post_office": "bưu điện",
    "amenity:police": "công an",
    "tourism:hotel": "khách sạn",
    "tourism:guest_house": "nhà nghỉ",
    "tourism:hostel": "hostel",
    "shop:convenience": "cửa hàng tiện lợi",
    "shop:supermarket": "siêu thị",
    "shop:clothes": "cửa hàng quần áo",
    "shop:motorcycle_repair": "tiệm sửa xe máy",
    "shop:hairdresser": "tiệm cắt tóc",
    "shop:mobile_phone": "cửa hàng điện thoại",
    "shop:department_store": "trung tâm thương mại",
    "leisure:park": "công viên",
    "office:government": "cơ quan nhà nước",
}
# Category hẹp → area phường/xã (card: "bệnh viện, trường học, chợ" + đồng nghĩa trường).
NARROW_CATEGORIES = ("amenity:hospital", "amenity:school", "amenity:marketplace", "amenity:university", "amenity:kindergarten")
# Noun pool cho G5 (loại người dùng hay tìm gần điểm tham quan/đền chùa).
G5_NOUN_CATEGORIES = (
    "amenity:restaurant",
    "amenity:cafe",
    "amenity:fast_food",
    "amenity:pharmacy",
    "amenity:atm",
    "tourism:hotel",
    "shop:convenience",
    "amenity:fuel",
)
LANDMARK_CATEGORIES = ("tourism:attraction", "amenity:place_of_worship")

G6_TEMPLATES = (
    "bệnh viện vũ trụ {province}",
    "nhà hàng lỗ đen {province}",
    "khách sạn trên mây {province}",
    "trường đại học phép thuật {province}",
    "quán phở sao hoả {province}",
)

_UNIT_PREFIX_RE = re.compile(r"^(Thành phố|Tỉnh|Phường|Xã|Thị trấn|Quận|Huyện|Thị xã)\s+")
_UNIT_MENTION_RE = re.compile(r"(Phường|Xã|Thị trấn|Quận|Huyện|Thị xã)\s+([^,;.]+)", re.IGNORECASE)
_OSM_ID_RE = re.compile(r"(node|way|relation)/\d+")
_VOWELS = frozenset("aeiouyàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹ")

G5_RADIUS_KM = 3.0
G5_ACCURACY_M = 50
G5_MIN_GOLDS = 2
G7_MAX_BRANCHES = 40  # giới nhánh để evidence không phình; gold = tất cả chi nhánh của tên đã chọn
MAX_GOLD = 5
MIN_GOLD = 3


class PilotBuildError(Exception):
    """Lỗi build pilot (không đủ candidate, dữ liệu xấu, ...)."""


# ---------------------------------------------------------------------------
# Helpers — _normalize_query/_sha256_file giữ import lười để E402 sạch
# (sys.path.insert ở top-level trước mọi import local).
# ---------------------------------------------------------------------------
def _normalize_query(text):
    from server.store import normalize

    return normalize(text)


def _sha256_file(path):
    from tools.benchmark_e3a import sha256_file

    return sha256_file(path)


def _unit_bare(name: str) -> str:
    """Bỏ tiền tố loại đơn vị: 'Phường Bến Thành' → 'Bến Thành', 'Tỉnh Cao Bằng' → 'Cao Bằng'."""
    return _UNIT_PREFIX_RE.sub("", name or "").strip()


def _osm_ok(row: dict) -> bool:
    """POI đủ điều kiện dựng URL OSM — ngược lại loại khỏi gold (không bịa URL)."""
    return row.get("source") == "OpenStreetMap" and bool(_OSM_ID_RE.fullmatch(row.get("source_id") or ""))


def _resolve_now(seed, now_arg):
    """ISO-8601 +07:00. --now > seed-date (8 digits) > đồng hồ hệ thống."""
    if now_arg:
        from tools.benchmark_e3a import is_iso8601_tz

        if not is_iso8601_tz(now_arg):
            raise PilotBuildError(f"--now phải là ISO-8601 có timezone, nhận: {now_arg!r}")
        return now_arg
    s = str(seed)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}T00:00:00+07:00"
    return datetime.now(TZ_ICT).isoformat(timespec="seconds")


def _evidence_record(qid, seq, poi, province_name, now, geo=False):
    from tools.benchmark_e3a import normalize_snippet, sha256_text

    snippet = f"{poi['name']}, {poi['address'] or province_name or 'Việt Nam'}"
    rec = {
        "evidence_id": f"ev-{qid}-{seq}",
        "query_id": qid,
        "source_type": "openstreetmap",
        "publisher": "OpenStreetMap contributors",
        "url": "https://www.openstreetmap.org/" + poi["source_id"],
        "accessed_at": now,
        "claim_supported": "POI tồn tại trong corpus OSM",
        "snippet": snippet,
        "snippet_sha256": sha256_text(normalize_snippet(snippet)),
        "content_sha256": None,
        "rights_basis": "ODbL 1.0 — ghi công OpenStreetMap contributors",
        "review_status": "pending",
    }
    if geo:
        rec["lat"] = poi["lat"]
        rec["lon"] = poi["lon"]
        rec["crs"] = "EPSG:4326"  # WGS84 — const bắt buộc của schema_v1
        rec["accuracy_m"] = G5_ACCURACY_M
        rec["coordinate_method"] = "osm_node"
    return rec


def _open_ro(path):
    p = Path(path)
    if not p.is_file():
        raise PilotBuildError(f"DB không tồn tại: {p}")
    con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _persona_list(group, n, rng):
    """Trả list persona độ dài n theo quota card; thu nhỏ tỉ lệ khi quota test < chuẩn."""
    spec = PERSONAS[group]
    total = sum(k for _p, k in spec)
    out = []
    remaining = n
    for i, (persona, k) in enumerate(spec):
        take = remaining if i == len(spec) - 1 else min(remaining, round(n * k / total))
        out.extend([persona] * take)
        remaining -= take
    rng.shuffle(out)
    return out


# ---------------------------------------------------------------------------
# Corpus + gazetteer loading
# ---------------------------------------------------------------------------
PURPOSE = (
    "Harness smoke set — KHÔNG phải E3a dev set. Câu hỏi lấy tên POI từ corpus nên vi phạm "
    "contract §5.4 (cấm lấy tên POI từ atlas/OSM) và evidence G5 dùng toạ độ OSM (cấm bởi §5.5). "
    "Chỉ dùng để kiểm tra cơ chế harness (validate/run/score/compare/latency), không công bố số."
)


def _load_context(atlas_path, gaz_path):
    con = _open_ro(atlas_path)
    gaz = _open_ro(gaz_path)
    rows = [
        dict(r)
        for r in con.execute("SELECT id, name, category, address, lat, lon, source, source_id, province_code, name_norm FROM places ORDER BY id")
    ]
    by_province = {}
    for r in rows:
        by_province.setdefault(r["province_code"], []).append(r)
    name_count = {}
    for r in rows:
        if r["province_code"]:
            key = (r["province_code"], r["name"])
            name_count[key] = name_count.get(key, 0) + 1

    units = [dict(r) for r in gaz.execute("SELECT id, code, name, name_norm, level, unit_type, parent_id, valid_to FROM admin_unit")]
    by_uid = {u["id"]: u for u in units}
    provinces = {u["code"]: u for u in units if u["level"] == "province" and u["valid_to"] is None}

    def province_code_of(unit):
        seen = set()
        u = unit
        while u is not None and u["level"] != "province" and u["id"] not in seen:
            seen.add(u["id"])
            u = by_uid.get(u["parent_id"])
        return u["code"] if u else None

    # Index tên đơn vị (ward/district) + alias → uid, khóa = normalize của server.
    name_to_uids = {}
    for u in units:
        if u["level"] in ("ward", "district"):
            name_to_uids.setdefault(_normalize_query(u["name_norm"]), []).append(u["id"])
    aliases = [dict(r) for r in gaz.execute("SELECT unit_id, alias, alias_type FROM admin_alias ORDER BY id")]
    for a in aliases:
        u = by_uid.get(a["unit_id"])
        if u is not None and u["level"] in ("ward", "district"):
            name_to_uids.setdefault(_normalize_query(a["alias"]), []).append(u["id"])

    # Một pass qua address: mention "Phường X"/"Quận Y" → resolve về unit (scope theo province_code).
    # Buckets dict id→row: cùng POI có thể mention 1 unit nhiều lần.
    unit_hits = {}
    for r in rows:
        if not r["address"] or not r["province_code"]:
            continue
        for m in _UNIT_MENTION_RE.finditer(r["address"]):
            mention = _normalize_query(f"{m.group(1)} {m.group(2).strip()}")
            for uid in name_to_uids.get(mention, []):
                u = by_uid[uid]
                if province_code_of(u) == r["province_code"]:
                    bucket = unit_hits.setdefault(uid, {"name": {}, "alias": {}})
                    key = "name" if _normalize_query(u["name_norm"]) == mention else "alias"
                    bucket[key][r["id"]] = r

    return {
        "con": con,
        "gaz": gaz,
        "rows": rows,
        "osm_rows": [r for r in rows if _osm_ok(r)],
        "by_province": by_province,
        "name_count": name_count,
        "by_uid": by_uid,
        "provinces": provinces,
        "province_code_of": province_code_of,
        "unit_hits": unit_hits,
        "aliases": aliases,
    }


def _province_name(ctx, code):
    u = ctx["provinces"].get(code)
    return u["name"] if u else ""


def _unit_rows(bucket_dict):
    return [bucket_dict[k] for k in sorted(bucket_dict)]


def _pick_gold(rows, n=MAX_GOLD, prefer_category=None):
    """Gold ≤ n; ưu tiên category khớp noun + address không rỗng; thứ tự id."""
    pool = [r for r in rows if _osm_ok(r)]
    pool.sort(key=lambda r: (prefer_category is not None and r["category"] != prefer_category, r["address"] == "", r["id"]))
    return pool[:n]


# ---------------------------------------------------------------------------
# Generators — mỗi hàm trả list entry {query, golds, panel, notes, near?, province_code?, landmark?}
# `used_texts` là set dùng chung toàn bộ build — generator tự check + add.
# ---------------------------------------------------------------------------
def _gen_g1(ctx, n_random, n_hard, used_texts):
    rng = ctx["rng"]
    pool = [
        r
        for r in ctx["osm_rows"]
        if r["province_code"]
        and len(r["name"]) >= 12
        and not any(c.isdigit() for c in r["name"])
        and ctx["name_count"].get((r["province_code"], r["name"]), 0) == 1
    ]
    rng.shuffle(pool)
    entries = []
    for r in pool:
        if len(entries) >= n_random + n_hard:
            break
        if r["name"].casefold() in used_texts:
            continue
        used_texts.add(r["name"].casefold())
        entries.append({"query": r["name"], "golds": [r], "panel": "pilot_random_85", "notes": "exact-name", "province_code": r["province_code"]})
    if len(entries) < n_random + n_hard:
        raise PilotBuildError(f"G1 thiếu candidate: cần {n_random + n_hard}, có {len(entries)}")
    ctx["g1_used"] = [e["golds"][0] for e in entries]
    return entries


def _g2_nodiac(name):
    """Bỏ dấu; None nếu tên vốn không có dấu (biến thể sẽ trùng câu gốc)."""
    text = _normalize_query(name)
    return None if text.casefold() == name.casefold() else text


def _g2_delete_mid(name, rng):
    """Xoá 1 ký tự giữa từ (không đầu/cuối) của một từ đủ dài."""
    words = name.split()
    spots = [(wi, ci) for wi, w in enumerate(words) if len(w) >= 4 for ci in range(1, len(w) - 1)]
    wi, ci = rng.choice(spots)
    w = words[wi]
    words[wi] = w[:ci] + w[ci + 1 :]
    return " ".join(words)


def _g2_double_vowel(name, rng):
    """Nhân đôi 1 nguyên âm giữa từ."""
    words = name.split()
    spots = [(wi, ci) for wi, w in enumerate(words) for ci in range(1, len(w) - 1) if w[ci] in _VOWELS]
    wi, ci = rng.choice(spots)
    w = words[wi]
    words[wi] = w[:ci] + w[ci] + w[ci:]
    return " ".join(words)


def _gen_g2(ctx, n_random, n_hard, used_texts):
    rng = ctx["rng"]
    pool = list(ctx["g1_used"])
    rng.shuffle(pool)
    total = n_random + n_hard
    if len(pool) < total:
        raise PilotBuildError(f"G2 thiếu nguồn từ G1: cần {total}, có {len(pool)}")
    entries = []
    for i, r in enumerate(pool):
        if len(entries) >= total:
            break
        if i < n_random:
            text, variant = _g2_nodiac(r["name"]), "nodia"
            panel = "pilot_random_85"
        else:
            variant = "del_mid" if i % 2 == 0 else "dup_vowel"
            text = (_g2_delete_mid if variant == "del_mid" else _g2_double_vowel)(r["name"], rng)
            panel = "pilot_hard_15"
        if text is None or text.casefold() in used_texts:
            # Tên vốn không dấu (nodia vô nghĩa) hoặc biến thể bị trùng: thử biến thể khác
            # trước khi bỏ — panel giữ nguyên theo phân tầng lấy mẫu.
            for fallback in ("del_mid", "dup_vowel", "nodia"):
                cand = (
                    (_g2_delete_mid if fallback == "del_mid" else _g2_double_vowel)(r["name"], rng) if fallback != "nodia" else _g2_nodiac(r["name"])
                )
                if cand and cand.casefold() not in used_texts:
                    text, variant = cand, fallback
                    break
            else:
                continue
        used_texts.add(text.casefold())
        entries.append(
            {
                "query": text,
                "golds": [r],
                "panel": panel,
                "notes": f"variant:{variant} of {r['name']}",
                "province_code": r["province_code"],
            }
        )
    if len(entries) < total:
        raise PilotBuildError(f"G2 thiếu sau dedupe: {len(entries)}/{total}")
    return entries


def _gen_g3(ctx, n_random, n_hard, used_texts):
    rng = ctx["rng"]
    narrow = set(NARROW_CATEGORIES)
    combos = []
    for cat in sorted(set(CATEGORY_NOUN) - narrow):
        noun = CATEGORY_NOUN[cat]
        for code in sorted(ctx["provinces"]):
            golds = _pick_gold([r for r in ctx["by_province"].get(code, []) if r["category"] == cat])
            if len(golds) >= MIN_GOLD:
                combos.append((cat, code, noun, golds))
    rng.shuffle(combos)
    entries = []
    for cat, code, noun, golds in combos:
        if len(entries) >= n_random:
            break
        text = f"{noun} {_unit_bare(ctx['provinces'][code]['name'])}"
        if text.casefold() in used_texts:
            continue
        used_texts.add(text.casefold())
        entries.append({"query": text, "golds": golds, "panel": "pilot_random_85", "notes": f"category:{cat}", "province_code": code})
    if len(entries) < n_random:
        raise PilotBuildError(f"G3 province-form thiếu: cần {n_random}, có {len(entries)}")

    # Hard: category hẹp + area phường/xã.
    ward_combos = []
    for uid, bucket in sorted(ctx["unit_hits"].items(), key=lambda kv: kv[0]):
        u = ctx["by_uid"][uid]
        if u["level"] != "ward":
            continue
        rows = _unit_rows(bucket["name"])
        for cat in NARROW_CATEGORIES:
            golds = _pick_gold([r for r in rows if r["category"] == cat])
            if len(golds) >= MIN_GOLD:
                ward_combos.append((u, cat, golds))
    rng.shuffle(ward_combos)
    hard = []
    for u, cat, golds in ward_combos:
        if len(hard) >= n_hard:
            break
        text = f"{CATEGORY_NOUN[cat]} {u['name']}"
        if text.casefold() in used_texts:
            continue
        used_texts.add(text.casefold())
        hard.append(
            {
                "query": text,
                "golds": golds,
                "panel": "pilot_hard_15",
                "notes": f"ward-form category:{cat}",
                "province_code": ctx["province_code_of"](u),
            }
        )
    if len(hard) < n_hard:
        raise PilotBuildError(f"G3 ward-form thiếu: cần {n_hard}, có {len(hard)}")
    return entries + hard


def _g4_noun_of(rows):
    """Danh từ = category lexicon phổ biến nhất trong gold (tie → tên category)."""
    counts = {}
    for r in rows:
        if r["category"] in CATEGORY_NOUN:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
    if not counts:
        return None, None
    cat = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return cat, CATEGORY_NOUN[cat]


def _gen_g4(ctx, n_random, n_hard, used_texts):
    rng = ctx["rng"]
    a_pool, b_pool, c_pool = [], [], []
    for uid, bucket in sorted(ctx["unit_hits"].items(), key=lambda kv: kv[0]):
        u = ctx["by_uid"][uid]
        name_rows, alias_rows = _unit_rows(bucket["name"]), _unit_rows(bucket["alias"])
        if u["level"] == "ward" and u["name"].startswith("Phường ") and len(name_rows) >= MIN_GOLD:
            a_pool.append((u, name_rows))
        if u["level"] == "district" and u["name"].startswith("Quận ") and len(name_rows) >= MIN_GOLD:
            b_pool.append((u, name_rows))
        merged = list({r["id"]: r for r in alias_rows + name_rows}.values())
        if alias_rows and len(merged) >= MIN_GOLD:
            c_pool.append((u, merged))
    # Form A nói "quán ăn" — ưu tiên phường thật sự có quán ăn khi đủ candidate.
    a_pref = [x for x in a_pool if any(r["category"] == "amenity:restaurant" for r in x[1])]
    if a_pref:
        a_pool = a_pref
    for pool in (a_pool, b_pool, c_pool):
        rng.shuffle(pool)

    entries = []
    used_uids = set()
    forms = (["A", "B"] * ((n_random + 1) // 2))[:n_random] + ["C"] * n_hard
    pools = {"A": a_pool, "B": b_pool, "C": c_pool}
    for form in forms:
        picked = None
        for f in (form, "A", "B", "C"):  # fallback theo thứ tự khi pool cạn
            pool = pools[f]
            while pool and pool[0][0]["id"] in used_uids:
                pool.pop(0)
            if pool:
                picked = (f, pool.pop(0))
                break
        if picked is None:
            raise PilotBuildError("G4 cạn candidate ở cả 3 form")
        actual_form, (u, rows) = picked
        used_uids.add(u["id"])
        if actual_form == "A":
            text = f"quán ăn phường {_unit_bare(u['name'])}"
            golds = _pick_gold(rows, prefer_category="amenity:restaurant")
            note = "form:phuong"
        elif actual_form == "B":
            cat, noun = _g4_noun_of(rows)
            text = f"{noun or 'quán ăn'} quận {_unit_bare(u['name'])}"
            golds = _pick_gold(rows, prefer_category=cat)
            note = "form:quan"
        else:
            cat, noun = _g4_noun_of(rows)
            unit_aliases = sorted(
                (a for a in ctx["aliases"] if a["unit_id"] == u["id"]),
                key=lambda a: (a["alias_type"] != "old_name", a["alias"]),
            )
            alias = unit_aliases[0]["alias"]
            text = f"{noun or 'quán ăn'} {alias}"
            golds = _pick_gold(rows, prefer_category=cat)
            note = f"form:alias({alias})"
        if text.casefold() in used_texts:
            continue
        used_texts.add(text.casefold())
        entries.append(
            {
                "query": text,
                "golds": golds,
                "panel": "pilot_hard_15" if form == "C" else "pilot_random_85",
                "notes": note,
                "province_code": ctx["province_code_of"](u),
            }
        )
    if len(entries) < n_random + n_hard:
        raise PilotBuildError(f"G4 thiếu câu sau dedupe: {len(entries)}/{n_random + n_hard}")
    return entries


def _gen_g5(ctx, n_random, n_hard, used_texts):
    from tools.benchmark_e3a import haversine_km

    rng = ctx["rng"]
    cat_rows = {cat: [r for r in ctx["osm_rows"] if r["category"] == cat] for cat in G5_NOUN_CATEGORIES}
    landmarks = [r for r in ctx["osm_rows"] if r["category"] in LANDMARK_CATEGORIES and len(r["name"]) >= 8]
    rng.shuffle(landmarks)
    combos = []
    local_texts = set()
    for lm in landmarks:
        offset = rng.randrange(len(G5_NOUN_CATEGORIES))
        ordered_cats = G5_NOUN_CATEGORIES[offset:] + G5_NOUN_CATEGORIES[:offset]
        for cat in ordered_cats:
            text = f"{CATEGORY_NOUN[cat]} gần {lm['name']}"
            if text.casefold() in used_texts or text.casefold() in local_texts:
                continue
            golds = [
                r
                for r in cat_rows[cat]
                if abs(r["lat"] - lm["lat"]) <= 0.03
                and abs(r["lon"] - lm["lon"]) <= 0.035
                and haversine_km(lm["lat"], lm["lon"], r["lat"], r["lon"]) <= G5_RADIUS_KM
            ]
            if len(golds) < G5_MIN_GOLDS:
                continue
            golds.sort(key=lambda r: (haversine_km(lm["lat"], lm["lon"], r["lat"], r["lon"]), r["id"]))
            combos.append((lm, cat, golds[:MAX_GOLD], text))
            local_texts.add(text)
            break
        if len(combos) >= 4 * (n_random + n_hard):
            break
    if len(combos) < n_random + n_hard:
        raise PilotBuildError(f"G5 thiếu landmark: cần {n_random + n_hard}, có {len(combos)}")
    # Hard = combo nhiều ứng viên nhất (dense ⇒ ranking khó); random = phần còn lại.
    combos.sort(key=lambda c: (-len(c[2]), c[0]["id"]))
    hard = combos[:n_hard]
    rest = combos[n_hard : n_hard + n_random]
    entries = []
    for lm, cat, golds, text in [*rest, *hard]:
        used_texts.add(text.casefold())
        entries.append(
            {
                "query": text,
                "golds": golds,
                "panel": "pilot_hard_15" if (lm, cat, golds, text) in hard else "pilot_random_85",
                "notes": f"landmark:{lm['name']} ({lm['category']})",
                "landmark": lm,
                "province_code": lm["province_code"],
            }
        )
    return entries


def _gen_g6(ctx, n_random, n_hard, used_texts):
    rng = ctx["rng"]
    total = n_random + n_hard
    provs = sorted(ctx["provinces"].values(), key=lambda u: u["code"])
    rng.shuffle(provs)
    entries = []
    ti = 0
    for u in provs:
        if len(entries) >= total:
            break
        template = G6_TEMPLATES[ti % len(G6_TEMPLATES)]
        ti += 1
        text = template.format(province=_unit_bare(u["name"]))
        if text.casefold() in used_texts:
            continue
        n = ctx["con"].execute("SELECT COUNT(*) FROM places WHERE name_norm = ?", (_normalize_query(text),)).fetchone()[0]
        if n != 0:
            continue  # template/tỉnh trùng dữ liệu thật → đổi tỉnh khác (card)
        used_texts.add(text.casefold())
        entries.append(
            {
                "query": text,
                "golds": [],
                "panel": "pilot_random_85" if len(entries) < n_random else "pilot_hard_15",
                "notes": f"template:{template.split('{')[0].strip()}",
                "province_code": u["code"],
                "_g6_count": n,
            }
        )
    if len(entries) < total:
        raise PilotBuildError(f"G6 thiếu: cần {total}, có {len(entries)} (hết tỉnh để thử)")
    return entries


def _gen_g7(ctx, n_random, n_hard, used_texts):
    rng = ctx["rng"]
    by_name = {}
    for r in ctx["osm_rows"]:
        if r["province_code"]:
            by_name.setdefault(r["name"], []).append(r)
    pool = []
    for name in sorted(by_name):
        rows = by_name[name]
        provs = {r["province_code"] for r in rows}
        if len(provs) >= 3 and len(rows) <= G7_MAX_BRANCHES and name.casefold() not in used_texts:
            pool.append((name, sorted(rows, key=lambda r: (r["province_code"], r["id"])), len(provs)))
    rng.shuffle(pool)
    total = n_random + n_hard
    if len(pool) < total:
        raise PilotBuildError(f"G7 thiếu tên đa-tỉnh: cần {total}, có {len(pool)}")
    taken = pool[:total]
    # Hard = tên xuất hiện ở nhiều tỉnh nhất trong nhóm đã chọn (mơ hồ nhất).
    by_difficulty = sorted(taken, key=lambda p: (-p[2], p[0]))
    hard_names = {p[0] for p in by_difficulty[:n_hard]}
    entries = []
    for name, rows, npc in [p for p in taken if p[0] not in hard_names] + [p for p in taken if p[0] in hard_names]:
        used_texts.add(name.casefold())
        entries.append(
            {
                "query": name,
                "golds": rows,
                "panel": "pilot_hard_15" if name in hard_names else "pilot_random_85",
                "notes": f"branches:{len(rows)} in {npc} provinces",
            }
        )
    return entries


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build_pilot(db, gaz_db, seed, out_dir, now=None, quotas=None, verbose=False):
    """Sinh pilot set vào out_dir; trả về manifest dict."""
    from tools.benchmark_e3a import git_sha, sha256_text

    quotas = quotas or QUOTAS
    now = _resolve_now(seed, now)
    rng = random.Random(str(seed))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    atlas_sha_before = _sha256_file(db)
    gaz_sha_before = _sha256_file(gaz_db)
    ctx = _load_context(db, gaz_db)
    ctx["rng"] = rng
    try:
        used_texts = set()
        generators = {
            "G1": lambda: _gen_g1(ctx, *quotas["G1"], used_texts),
            "G2": lambda: _gen_g2(ctx, *quotas["G2"], used_texts),
            "G3": lambda: _gen_g3(ctx, *quotas["G3"], used_texts),
            "G4": lambda: _gen_g4(ctx, *quotas["G4"], used_texts),
            "G5": lambda: _gen_g5(ctx, *quotas["G5"], used_texts),
            # Contract §5.2: G6 = trùng tên/mơ hồ, G7 = không có kết quả phù hợp.
            # _gen_g6 sinh câu no-match (template hư cấu) ⇒ thuộc G7; _gen_g7 sinh
            # câu nhiều chi nhánh ⇒ thuộc G6. Giữ tên hàm, hoán đổi vị trí.
            "G6": lambda: _gen_g7(ctx, *quotas["G6"], used_texts),
            "G7": lambda: _gen_g6(ctx, *quotas["G7"], used_texts),
        }

        queries = []
        evidence = []
        g6_report = []
        seq = 0
        for group in GROUP_ORDER:
            entries = generators[group]()
            personas = _persona_list(group, len(entries), rng)
            for entry, persona in zip(entries, personas, strict=True):
                seq += 1
                qid = f"q{seq:03d}"
                q = {
                    "id": qid,
                    "group": group,
                    "persona": persona,
                    "split": "dev",
                    "panel": entry["panel"],
                    "regression150": False,
                    "query": entry["query"],
                }
                if entry.get("province_code"):
                    q["province_code"] = entry["province_code"]
                if entry.get("notes"):
                    q["notes"] = entry["notes"]

                if group == "G5":
                    lm = entry["landmark"]
                    ev = _evidence_record(qid, 1, lm, _province_name(ctx, lm.get("province_code")), now, geo=True)
                    evidence.append(ev)
                    q["near"] = {
                        "lat": lm["lat"],
                        "lon": lm["lon"],
                        "radius_km": G5_RADIUS_KM,
                        "crs": "EPSG:4326",
                        "coordinate_source": "osm_node",
                        "evidence_id": ev["evidence_id"],
                        "accuracy_m": G5_ACCURACY_M,
                    }
                    for i, poi in enumerate(entry["golds"], start=2):
                        evidence.append(_evidence_record(qid, i, poi, _province_name(ctx, poi.get("province_code")), now, geo=True))
                else:
                    for i, poi in enumerate(entry["golds"], start=1):
                        evidence.append(
                            _evidence_record(qid, i, poi, _province_name(ctx, poi.get("province_code") or entry.get("province_code")), now)
                        )
                if group == "G7":
                    g6_report.append((qid, entry["query"], entry["_g6_count"]))
                queries.append(q)
    finally:
        ctx["con"].close()
        ctx["gaz"].close()

    atlas_sha_after = _sha256_file(db)
    gaz_sha_after = _sha256_file(gaz_db)
    if atlas_sha_before != atlas_sha_after or gaz_sha_before != gaz_sha_after:
        raise PilotBuildError("G8: SHA-256 master thay đổi trong lúc build — huỷ output")
    if verbose:
        for qid, text, n in g6_report:
            print(f"  G7 {qid}: {text!r} → name_norm count={n}")

    quota_table = {g: {"random": quotas[g][0], "hard": quotas[g][1], "total": quotas[g][0] + quotas[g][1]} for g in GROUP_ORDER}
    counts = {g: sum(1 for q in queries if q["group"] == g) for g in GROUP_ORDER}
    manifest = {
        "purpose": PURPOSE,
        "seed": int(seed) if str(seed).isdigit() else str(seed),
        "created_at": now,
        "git_sha": git_sha(),
        "atlas_sha256": atlas_sha_after,
        "gazetteer_sha256": gaz_sha_after,
        "quotas": quota_table,
        "counts_by_group": counts,
        "ids_sha256": sha256_text("\n".join(q["id"] for q in queries)),
    }

    (out_dir / "queries.json").write_text(
        json.dumps({"version": "e3a-pilot/v1", "purpose": PURPOSE, "queries": queries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (out_dir / "evidence.jsonl").write_text(
        "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in evidence),
        encoding="utf-8",
        newline="\n",
    )
    (out_dir / "pilot_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if verbose:
        print(f"build OK: {len(queries)} queries, {len(evidence)} evidence → {out_dir}")
    return manifest


# ---------------------------------------------------------------------------
# worksheet
# ---------------------------------------------------------------------------
def cmd_worksheet(a) -> int:
    from tools.benchmark_e3a import load_queries, read_json

    run = read_json(a.run)
    block = next((b for b in (run.get("adapters") or []) if b.get("adapter") == "legacy-http"), None)
    if block is None:
        raise PilotBuildError("run.json thiếu adapter 'legacy-http'")
    by_q = {r["query_id"]: r for r in block.get("results") or []}
    queries = load_queries(a.queries)
    lines = []
    for q in queries:
        res = by_q.get(q["id"]) or {}
        candidates = []
        for i, row in enumerate((res.get("top_rows") or [])[:10]):
            cand = {
                "rank": i + 1,
                "place_id": row.get("id"),
                "name": row.get("name"),
                "address": row.get("address"),
                "category": row.get("category"),
            }
            if row.get("distance_km") is not None:
                cand["distance_km"] = row["distance_km"]
            candidates.append(cand)
        lines.append(
            {
                "query_id": q["id"],
                "query": q["query"],
                "group": q.get("group"),
                "persona": q.get("persona"),
                "candidates": candidates,
            }
        )
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8", newline="\n")
    print(f"worksheet OK: {len(lines)} rows → {a.out}")
    return 0


def cmd_build(a) -> int:
    manifest = build_pilot(a.db, a.gazetteer_db, a.seed, a.out, now=a.now, verbose=a.verbose)
    print(f"build OK: {sum(manifest['counts_by_group'].values())} queries → {a.out}")
    return 0


def main(argv=None) -> int:
    from tools.benchmark_e3a import _configure_stdio

    _configure_stdio()
    p = argparse.ArgumentParser(description="C3a pilot builder — sinh 100 câu dev deterministic theo seed")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="sinh queries.json + evidence.jsonl + pilot_manifest.json (corpus read-only)")
    b.add_argument("--db", type=Path, required=True, help="master Atlas DB (read-only)")
    b.add_argument("--gazetteer-db", type=Path, required=True, help="master gazetteer DB (read-only)")
    b.add_argument("--seed", required=True, help="seed cho random.Random (vd 20261019)")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--now", default=None, help="ISO-8601 có tz — pin accessed_at/created_at (mặc định: nửa đêm +07:00 ngày trong seed)")
    b.add_argument("--verbose", action="store_true", help="in tiến trình + assertion G6")
    b.set_defaults(fn=cmd_build)

    w = sub.add_parser("worksheet", help="sinh worksheet chấm tay từ run.json (adapter legacy-http)")
    w.add_argument("--run", type=Path, required=True)
    w.add_argument("--queries", type=Path, required=True)
    w.add_argument("--out", type=Path, required=True)
    w.set_defaults(fn=cmd_worksheet)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except PilotBuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
