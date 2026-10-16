#!/usr/bin/env python3
"""Import the official NSO administrative gazetteer into data/gazetteer.sqlite3.

Sources (data/gazetteer/raw/, sha256-pinned in raw/SOURCES.json):
  1. BangChuyendoiĐVHCmoi_cu_final.xlsx   — ward mapping old->new, sheet "Tổng hợp_không merge "
  2. Danh sách cấp xã ___16_10_2026.xls   — 3.321 current wards (16/10/2026)
  3. Danh sách cấp tỉnh __16_10_2026.xls  — 34 current provinces
  4. Danh sách cấp tỉnh __30_06_2025.xls  — 63 pre-reform provinces
  5. Danh sách cấp huyện __30_06_2025.xls — 686 pre-reform districts
  6. Danh sách cấp xã ___30_06_2025.xls   — 9.843 pre-reform wards
  7. Đối chiếu ... 16_10_2026 và 31_10_2025 .xls — ward comparison + narrative

Model notes / known source-data quirks (all decisions kept to file evidence):
  * Time-versioned units: pre-reform rows get valid_to=2025-06-30 and the
    documented sentinel valid_from='0001-01-01' (inception dates are not in the
    snapshot files). Reform-era rows get valid_from=2025-07-01, except units
    created/changed by 2026 instruments where the file's effective date is used.
  * QH16/2026 provinces (22 Quảng Ninh, 24 Bắc Ninh, 75 Đồng Nai) and the 22
    NQ-UBTVQH16 xã->phường upgrades get an intermediate record (the 'Tỉnh'/'Xã'
    version evidenced by file 7's đối chiếu side) plus a type_changed relation.
  * 2026 instruments: valid_from/valid_to/relation effective_date are taken
    from data/gazetteer/DECREES.json (ngày HIỆU LỰC), never from the ngày ban
    hành embedded in the NSO 'Nghị định' cells (hotfix T-E2c).
  * The 30/06/2025 ward file omits every Bắc Giang ward (192) and the district
    file omits all 10 Bắc Giang districts; they are rebuilt from file 1's old
    side (name, code, district, province are all present there).
  * The district file's "Mã TP" column uses post-merger province codes for
    absorbed provinces (Bắc Ninh districts filed under '24'); its province NAME
    column is authoritative, so district->province links resolve by name.
  * Two file-1 rows carry wrong old codes ('Xã Mỹ Hưng' 01615, 'Xã Phú Thịnh'
    02485). When old code and old name disagree and the name matches exactly one
    ward in the same district that appears nowhere else in the file, the code is
    treated as a typo; the case is logged in the report's ambiguous section.
  * Out of scope for this slice: FTS5/search/resolver, boundary polygons,
    province-level merge relations (file 1 is ward-level only).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

# Dev-only readers; runtime of the atlas itself stays stdlib-only.
try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised only without the dev lib
    openpyxl = None
try:
    import xlrd
except ImportError:  # pragma: no cover
    xlrd = None

OLD_CUTOFF = "2025-06-30"
REFORM_DATE = "2025-07-01"
LEGACY_VALID_FROM = "0001-01-01"  # sentinel: inception date not present in the snapshot files
RETRIEVED_AT = "2026-10-16T00:00:00+07:00"
PUBLISHER = "NSO (Cục Thống kê)"
RIGHTS_BASIS = "Dữ liệu công khai của cơ quan nhà nước; lưu bản gốc cho truy vết. Kiểm tra điều khoản tái phân phối trước khi công bố lại."
URL_MAIN_LIST = "https://danhmuchanhchinh.nso.gov.vn/Default.aspx"
URL_DOI_CHIEU = "https://danhmuchanhchinh.nso.gov.vn/Doi_Chieu_Moi.aspx"
URL_CONVERSION = "https://danhmuchanhchinh.nso.gov.vn/TAPTIN/BangChuyendoiĐVHCmoi_cu_final.xlsx"
DECREES_FILE = Path(__file__).resolve().parents[1] / "data" / "gazetteer" / "DECREES.json"

F_MAPPING = "BangChuyendoiĐVHCmoi_cu_final.xlsx"
F_WARD_NEW = "Danh sách cấp xã ___16_10_2026.xls"
F_PROV_NEW = "Danh sách cấp tỉnh __16_10_2026.xls"
F_PROV_OLD = "Danh sách cấp tỉnh __30_06_2025.xls"
F_DIST_OLD = "Danh sách cấp huyện __30_06_2025.xls"
F_WARD_OLD = "Danh sách cấp xã ___30_06_2025.xls"
F_DOI_CHIEU = "Đối chiếu đơn vị hành chính cấp Xã _ 16_10_2026 và 31_10_2025 .xls"

SOURCES = {
    "mapping": {"file": F_MAPPING, "url": URL_CONVERSION, "as_of": REFORM_DATE},
    "ward_new": {"file": F_WARD_NEW, "url": URL_MAIN_LIST, "as_of": "2026-10-16"},
    "prov_new": {"file": F_PROV_NEW, "url": URL_MAIN_LIST, "as_of": "2026-10-16"},
    "prov_old": {"file": F_PROV_OLD, "url": URL_MAIN_LIST, "as_of": OLD_CUTOFF},
    "dist_old": {"file": F_DIST_OLD, "url": URL_MAIN_LIST, "as_of": OLD_CUTOFF},
    "ward_old": {"file": F_WARD_OLD, "url": URL_MAIN_LIST, "as_of": OLD_CUTOFF},
    "doichieu": {"file": F_DOI_CHIEU, "url": URL_DOI_CHIEU, "as_of": "2026-10-16"},
}

# QH16/2026 centrally-governed-city upgrades: province code -> (decree, effective date).
# These are the rows in the 16/10/2026 province list whose nghị định is a QH16 act.
TYPE_PREFIXES = ("thành phố", "thị xã", "thị trấn", "đặc khu", "tỉnh", "quận", "huyện", "phường", "xã")

SCHEMA = """
CREATE TABLE gazetteer_source (
  id INTEGER PRIMARY KEY,
  publisher TEXT NOT NULL,
  source_url TEXT NOT NULL,
  document_ref TEXT,
  effective_as_of TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  license_id TEXT, rights_basis TEXT,
  redistribution_status TEXT NOT NULL CHECK(redistribution_status IN ('permitted','restricted','unverified'))
);
CREATE TABLE admin_unit (
  id INTEGER PRIMARY KEY,
  authority TEXT NOT NULL DEFAULT 'NSO',
  code TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('province','district','ward')),
  unit_type TEXT NOT NULL,
  parent_id INTEGER REFERENCES admin_unit(id),
  valid_from TEXT NOT NULL, valid_to TEXT,
  source_id INTEGER NOT NULL REFERENCES gazetteer_source(id),
  notes TEXT,
  UNIQUE(authority, level, code, valid_from)
);
CREATE TABLE admin_alias (
  id INTEGER PRIMARY KEY,
  unit_id INTEGER NOT NULL REFERENCES admin_unit(id),
  alias TEXT NOT NULL, alias_norm TEXT NOT NULL,
  alias_type TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT,
  source_id INTEGER NOT NULL REFERENCES gazetteer_source(id)
);
CREATE TABLE admin_relation (
  id INTEGER PRIMARY KEY,
  from_unit_id INTEGER NOT NULL REFERENCES admin_unit(id),
  to_unit_id INTEGER NOT NULL REFERENCES admin_unit(id),
  relation_type TEXT NOT NULL CHECK(relation_type IN ('renamed_to','merged_into','split_into','partially_into','type_changed')),
  effective_date TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES gazetteer_source(id),
  notes TEXT
);
CREATE INDEX idx_admin_code ON admin_unit(authority, level, code);
CREATE INDEX idx_admin_parent ON admin_unit(parent_id);
CREATE INDEX idx_alias_norm ON admin_alias(alias_norm);
"""


# ---------------------------------------------------------------- pure helpers


def cell_text(v) -> str:
    """Cell value -> stripped text; numeric cells lose the '.0' tail."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return " ".join(str(v).split())


def norm_code(v, width: int) -> str:
    """Zero-pad a bare-digit administrative code to its level width; '' if invalid."""
    s = cell_text(v)
    if re.fullmatch(r"\d+", s) and len(s) <= width:
        return s.zfill(width)
    return ""


def name_norm(s: str) -> str:
    """lowercase -> strip combining marks (đ->d) -> unify apostrophes/punct -> collapse ws."""
    s = unicodedata.normalize("NFC", str(s)).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("đ", "d")
    s = re.sub("['\u2019\u2018`\u00b4\u02bc\"]", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def short_name(name: str) -> str:
    """Strip a leading level prefix ('Thành phố X' -> 'X', 'Phường Y' -> 'Y')."""
    words = name.split()
    if len(words) < 2:
        return ""
    two = " ".join(words[:2]).lower()
    if two in ("thành phố", "thị xã", "thị trấn", "đặc khu"):
        return " ".join(words[2:]) if len(words) > 2 else ""
    if words[0].lower() in ("tỉnh", "quận", "huyện", "phường", "xã"):
        return " ".join(words[1:])
    return ""


def unit_type_from_name(name: str) -> str:
    """Leading level prefix of a unit name, canonical casing ('Thị Xã' -> 'Thị xã')."""
    words = name.split()
    for cand in (" ".join(words[:2]), words[0] if words else ""):
        for p in TYPE_PREFIXES:
            if cand.lower() == p:
                return p[:1].upper() + p[1:]
    return ""


def excel_serial_to_iso(v, datemode: int = 0) -> str:
    """Excel serial date -> ISO (datemode 0: 1899-12-30 epoch, xlrd convention)."""
    if isinstance(v, str):
        return v.strip()
    base = date(1899, 12, 30) if datemode == 0 else date(1904, 1, 1)
    return (base + timedelta(days=int(v))).isoformat()


def parse_decree_date(text: str) -> str:
    """Last dd/mm/yyyy inside an nghị định cell -> ISO, else ''."""
    hits = re.findall(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not hits:
        return ""
    d, m, y = hits[-1]
    return f"{y}-{int(m):02d}-{int(d):02d}"


def is_2026_decree(text: str) -> bool:
    """Nghị định issued under the post-reform national assembly (QH16 / NQ-UBTVQH16)."""
    return bool(re.search(r"/(QH|NQ-UBTVQH)16\b", text)) or "/2026/" in text


def decree_document_id(text: str) -> str:
    """Document id inside a nghị định cell: '39/2026/QH16 - 24/08/2026' -> '39/2026/QH16'."""
    m = re.search(r"\d{1,4}/(?:\d{4}/)?(?:QH|NQ-UBTVQH)\d{2}", text)
    return m.group(0) if m else ""


def load_decrees(path: Path | None = None) -> dict:
    """DECREES.json -> {'reform_milestone': {...}|None, 'documents': [...]}.

    A missing file yields the empty structure: imports then keep running on
    the historical issued-date parsing instead of failing.
    """
    p = path if path is not None else DECREES_FILE
    if not p.exists():
        return {"reform_milestone": None, "documents": []}
    data = json.loads(p.read_text(encoding="utf-8"))  # NOSONAR(S8707)
    return {"reform_milestone": data.get("reform_milestone"), "documents": data.get("documents", [])}


def check_reform_milestone(milestone: dict | None) -> None:
    """Fail loudly when DECREES.json's reform milestone disagrees with REFORM_DATE.

    Every reform-era valid_from derives from REFORM_DATE; a wrong milestone in
    the metadata file would silently shift all of them.
    """
    if not milestone:
        return
    eff = milestone.get("effective_on")
    if eff is not None and eff != REFORM_DATE:
        raise ValueError(f"DECREES.json reform_milestone.effective_on={eff!r} != REFORM_DATE {REFORM_DATE!r}")


def find_decrees(doc_id: str, documents: list) -> list:
    """Decree records with an exact document_id match ([] for '' or unknown ids)."""
    if not doc_id:
        return []
    return [d for d in documents if d.get("document_id") == doc_id]


def decree_effective_on(doc_id: str, documents: list) -> str:
    """Ngày hiệu lực of a decree record; '' when the document id is unknown."""
    hits = find_decrees(doc_id, documents)
    return str(hits[0].get("effective_on", "")) if hits else ""


def decree_valid_from(decree: str, documents: list) -> str:
    """valid_from for a unit created/changed by `decree` (hiệu lực, not ban hành).

    DECREES.json is authoritative when the decree id is known; unknown ids fall
    back to the historical issued-date parse so pre-metadata behavior is kept.
    """
    eff = decree_effective_on(decree_document_id(decree), documents)
    if eff:
        return eff
    return parse_decree_date(decree) if is_2026_decree(decree) else REFORM_DATE


def classify_relation(note: str) -> str | None:
    """File-1 'Ghi chú' -> relation_type; None = do not create a relation."""
    n = name_norm(note)
    if n == "giu nguyen":
        return None  # same unit continues; caller adds renamed_to if the name differs
    if n == "doi ten":
        return "renamed_to"
    if n == "doi loai hinh":
        return "type_changed"
    if n.startswith(("nhap toan bo", "sap xep toan bo")):
        return "merged_into"
    if n.startswith(("nhap mot phan", "phan con lai")):
        return "partially_into"
    if n.startswith("doi ten"):  # 'đổi tên (đổi loại hình) từ một phần X' -> partial source
        return "partially_into"
    return None


def parse_list_rows(rows, ncols: int, code_width: int):
    """NSO export sheet -> unit dicts; skips header/footer ('Số lượng : N') rows.

    rows: iterable of raw row sequences (row 0 = header). Only rows whose first
    cell is a valid code are returned; each dict carries the first `ncols` cells.
    """
    out = []
    for row in rows:
        vals = [cell_text(c) for c in list(row)[:ncols]]
        code = norm_code(vals[0], code_width)
        if not code:
            continue
        out.append({"code": code, "cells": vals})
    return out


def parse_mapping_rows(rows):
    """File-1 sheet 'Tổng hợp_không merge ' -> mapping dicts.

    Skips the two header rows and province-divider rows (both code cells empty).
    Returns (mappings, dividers, partial) where partial rows have exactly one of
    (new_code, old_code) empty — e.g. district->đặc khu or unincorporated land.
    """
    mappings, dividers, partial = [], 0, []
    for i, row in enumerate(rows):
        if i < 2:
            continue
        vals = [cell_text(c) for c in list(row)[:8]]
        if not any(vals):
            continue
        new_code = norm_code(vals[2], 5)
        old_code = norm_code(vals[4], 5)
        if not vals[2] and not vals[4]:
            dividers += 1
            continue
        rec = {
            "row": i + 1,
            "new_prov": vals[0],
            "new_name": " ".join(vals[1].split()),
            "new_code": new_code,
            "old_name": " ".join(vals[3].split()),
            "old_code": old_code,
            "note": " ".join(vals[5].split()),
            "old_district": vals[6],
            "old_prov": vals[7],
        }
        if vals[2] and vals[4]:
            mappings.append(rec)
        else:
            partial.append(rec)
    return mappings, dividers, partial


def parse_doichieu_rows(rows, datemode: int = 0):
    """File-7 sheet -> dicts; 'Ngày hiệu lực' Excel serials converted to ISO."""
    out = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        vals = list(row)[:13]
        code = norm_code(vals[2], 5)
        if not code:
            continue
        out.append(
            {
                "prov_code": cell_text(vals[0]),
                "prov_name": cell_text(vals[1]),
                "code": code,
                "name": " ".join(cell_text(vals[3]).split()),
                "decree": cell_text(vals[4]),
                "effective": excel_serial_to_iso(vals[5], datemode),
                "dc_name": " ".join(cell_text(vals[6]).split()),
                "dc_code": norm_code(vals[7], 5),
                "dc_decree": cell_text(vals[8]),
                "dc_effective": excel_serial_to_iso(vals[9], datemode),
                "dc_prov_name": cell_text(vals[10]),
                "dc_prov_code": cell_text(vals[11]),
                "note": " ".join(cell_text(vals[12]).split()),
            }
        )
    return out


def _code_in_parens(text: str) -> str:
    m = re.search(r"\((\d+)\)\s*$", text.strip())
    return m.group(1) if m else ""


def strip_trailing_code(text: str) -> str:
    """Drop a trailing ' (NNN)' record-code suffix (linear regex, no backtracking)."""
    m = re.search(r"\((\d+)\)\s*$", text)
    return text[: m.start()].rstrip() if m else text


def _day_before(iso: str) -> str:
    d = date.fromisoformat(iso)
    return (d - timedelta(days=1)).isoformat()


def _name_tokens(name: str) -> set:
    """Diacritic-free tokens minus the level prefix — for typo detection."""
    n = name_norm(name)
    words = n.split()
    if words[:2] and " ".join(words[:2]) in ("thanh pho", "thi xa", "thi tran", "dac khu"):
        words = words[2:]
    elif words and words[0] in ("tinh", "quan", "huyen", "phuong", "xa"):
        words = words[1:]
    return set(words)


# ------------------------------------------------------------------ file input


def find_manifest(raw_dir: Path) -> Path | None:
    """Locate the sha256 manifest beside the raw files or in the committed spot."""
    for cand in (raw_dir / "SOURCES.json", raw_dir.parent / "SOURCES.json"):
        if cand.exists():
            return cand
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_xls(path: Path, datemode_out=None):
    if xlrd is None:
        raise ImportError("xlrd is required to read .xls sources (dev dependency)")
    wb = xlrd.open_workbook(str(path), logfile=io.StringIO())  # sink cosmetic sector-size warnings
    sh = wb.sheet_by_index(0)
    rows = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
    if datemode_out is not None:
        datemode_out.append(wb.datemode)
    return rows


def read_xlsx(path: Path, sheet_substr: str):
    if openpyxl is None:
        raise ImportError("openpyxl is required to read .xlsx sources (dev dependency)")
    wb = openpyxl.load_workbook(str(path), read_only=True)
    try:
        target = next((n for n in wb.sheetnames if sheet_substr in n.lower()), None)
        if target is None:
            raise ValueError(f"sheet containing '{sheet_substr}' not found in {path.name}: {wb.sheetnames}")
        return [list(r) for r in wb[target].iter_rows(values_only=True)]
    finally:
        wb.close()


# ------------------------------------------------------------- model building


def _unit(code, name, level, unit_type, valid_from, valid_to, source_key, parent_key=None, notes=None):
    return {
        "code": code,
        "name": name,
        "name_norm": name_norm(name),
        "level": level,
        "unit_type": unit_type,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "source_key": source_key,
        "parent_key": parent_key,
        "notes": notes,
    }


def build_model(raw: dict, report: dict, decree_docs: list | None = None):
    """Turn parsed sheets into unit/alias/relation lists. `raw` keys per SOURCES;
    a key may be None only under `--allow-partial`.

    `decree_docs`: decree records from DECREES.json; None loads the committed
    file. Every validity branch below resolves valid_from via
    decree_valid_from (hiệu lực) when metadata is available.
    """
    units = {}
    aliases = []
    relations = []
    ambiguous = report["ambiguous"]

    def add_unit(u):
        key = (u["level"], u["code"], u["valid_from"])
        if key in units:
            ambiguous.append({"kind": "duplicate_unit_key", "key": key, "name": u["name"]})
            return key
        units[key] = u
        return key

    prov_old = raw.get("prov_old") or []
    prov_new = raw.get("prov_new") or []
    dist_old = raw.get("dist_old") or []
    ward_old = raw.get("ward_old") or []
    ward_new = raw.get("ward_new") or []
    mapping = raw.get("mapping") or {"mappings": [], "dividers": 0, "partial": []}
    doichieu = raw.get("doichieu") or []
    if decree_docs is None:
        decree_docs = load_decrees()["documents"]

    # ---------------- provinces ------------------------------------------------
    old_prov_key = {}
    old_prov_by_name = {}
    for r in prov_old:
        name, utype = r["cells"][1], r["cells"][3]
        key = add_unit(_unit(r["code"], name, "province", utype, LEGACY_VALID_FROM, OLD_CUTOFF, "prov_old"))
        old_prov_key[r["code"]] = key
        old_prov_by_name[name_norm(name)] = key

    prov_timeline = {}  # code -> [(valid_from, valid_to, key)]
    prov_upgrades = {}  # code -> QH16 effective date
    prov_current = {}  # code -> key of the latest (open-ended) version

    def prov_at(code, on_date):
        for vf, vt, key in sorted(prov_timeline.get(code, []), key=lambda t: t[0]):
            if vf <= on_date and (vt is None or on_date <= vt):
                return key
        return None

    # intermediate 'Tỉnh' records for provinces upgraded to TP TW by a QH16 act;
    # the prior 'Tỉnh' name/type is evidenced by file 7's đối chiếu columns.
    for r in prov_new:
        name, utype, decree = r["cells"][1], r["cells"][3], r["cells"][4]
        vf = decree_valid_from(decree, decree_docs)
        if vf > REFORM_DATE:
            inter_name = next(
                (r7["dc_prov_name"] for r7 in doichieu if r7["prov_code"] == r["code"] and r7["dc_prov_name"] != r7["prov_name"]),
                None,
            ) or "Tỉnh " + (short_name(name) or name)
            ikey = add_unit(_unit(r["code"], inter_name, "province", "Tỉnh", REFORM_DATE, _day_before(vf), "doichieu" if doichieu else "prov_new"))
            prov_timeline.setdefault(r["code"], []).append((REFORM_DATE, _day_before(vf), ikey))
            prov_upgrades[r["code"]] = (ikey, vf)
        key = add_unit(_unit(r["code"], name, "province", utype, vf, None, "prov_new"))
        prov_timeline.setdefault(r["code"], []).append((vf, None, key))
        prov_current[r["code"]] = key

    for code, (ikey, vf) in prov_upgrades.items():
        cur = prov_at(code, vf)
        relations.append({"from": ikey, "to": cur, "type": "type_changed", "effective": vf, "source_key": "prov_new", "notes": None})

    # ---------------- districts -------------------------------------------------
    dist_key = {}
    for r in dist_old:
        name, utype, prov_name = r["cells"][1], r["cells"][3], r["cells"][6]
        pkey = old_prov_by_name.get(name_norm(prov_name))
        if pkey is None:
            ambiguous.append({"kind": "district_parent_unresolved", "code": r["code"], "name": name, "prov_name": prov_name})
        key = add_unit(_unit(r["code"], name, "district", utype, LEGACY_VALID_FROM, OLD_CUTOFF, "dist_old", pkey))
        dist_key[r["code"]] = key

    # ---------------- old wards --------------------------------------------------
    old_ward_key = {}
    f6_by_dist_name = {}
    for r in ward_old:
        name, utype, qh = r["cells"][1], r["cells"][3], r["cells"][6]
        pkey = dist_key.get(qh)
        if pkey is None:
            ambiguous.append({"kind": "ward_parent_unresolved", "code": r["code"], "name": name, "ma_qh": qh})
        key = add_unit(_unit(r["code"], name, "ward", utype, LEGACY_VALID_FROM, OLD_CUTOFF, "ward_old", pkey))
        old_ward_key[r["code"]] = key
        f6_by_dist_name.setdefault((qh, name_norm(name)), []).append(r["code"])

    # Bắc Giang defect: file 5/6 omit the province's districts and wards; rebuild
    # them from file 1's old side (which carries code+name+district+province).
    for m in mapping["mappings"]:
        dc = _code_in_parens(m["old_district"])
        if dc and dc not in dist_key:
            pv = _code_in_parens(m["old_prov"])
            key = add_unit(
                _unit(
                    dc,
                    strip_trailing_code(m["old_district"]),
                    "district",
                    unit_type_from_name(strip_trailing_code(m["old_district"])),
                    LEGACY_VALID_FROM,
                    OLD_CUTOFF,
                    "mapping",
                    old_prov_key.get(pv),
                )
            )
            dist_key[dc] = key
            report["created_districts_from_mapping"] += 1
    for m in mapping["mappings"]:
        oc = m["old_code"]
        if oc and oc not in old_ward_key:
            dc = _code_in_parens(m["old_district"])
            key = add_unit(
                _unit(oc, m["old_name"], "ward", unit_type_from_name(m["old_name"]), LEGACY_VALID_FROM, OLD_CUTOFF, "mapping", dist_key.get(dc))
            )
            old_ward_key[oc] = key
            report["created_wards_from_mapping"] += 1

    # ---------------- current + intermediate wards -------------------------------
    dc_by_code = {r["code"]: r for r in doichieu}
    map_new_name = {}
    for m in mapping["mappings"]:
        map_new_name.setdefault(m["new_code"], m["new_name"])
    new_ward_units = {}  # code -> (current_key, intermediate_key|None)
    for r in ward_new:
        name, utype, decree, mtp = r["cells"][1], r["cells"][3], r["cells"][4], r["cells"][5]
        dc = dc_by_code.get(r["code"])
        if dc:
            vf = dc["effective"]
        else:
            vf = decree_valid_from(decree, decree_docs)
        ikey = None
        # intermediate record: file 7 shows a different đối-chiếu name for the
        # same code (the xã that preceded a 2026 phường upgrade)
        if vf > REFORM_DATE:
            iname = dc["dc_name"] if dc and dc["dc_code"] == r["code"] and name_norm(dc["dc_name"]) != name_norm(name) else None
            if iname is None:
                m1name = map_new_name.get(r["code"])
                if m1name and name_norm(m1name) != name_norm(name):
                    iname = m1name
            if iname:
                ikey = add_unit(
                    _unit(
                        r["code"],
                        iname,
                        "ward",
                        unit_type_from_name(iname),
                        REFORM_DATE,
                        _day_before(vf),
                        "doichieu" if dc else "ward_new",
                        prov_at(mtp, REFORM_DATE),
                    )
                )
        pkey = prov_current.get(mtp) or prov_at(mtp, vf)
        if pkey is None:
            ambiguous.append({"kind": "new_ward_parent_unresolved", "code": r["code"], "name": name, "ma_tp": mtp})
        key = add_unit(_unit(r["code"], name, "ward", utype, vf, None, "ward_new", pkey))
        if ikey:
            relations.append(
                {"from": ikey, "to": key, "type": "type_changed", "effective": vf, "source_key": "doichieu" if dc else "ward_new", "notes": None}
            )
        new_ward_units[r["code"]] = (key, ikey)

    # ward notes: file 7 'Ghi Chú' narrative attaches to the current unit
    notes_attached = 0
    for r in doichieu:
        target = new_ward_units.get(r["code"])
        if target and r["note"]:
            units[target[0]]["notes"] = r["note"]
            notes_attached += 1
        elif not target:
            ambiguous.append({"kind": "doichieu_code_unresolved", "code": r["code"], "name": r["name"]})
    report["notes_attached"] = notes_attached

    # ---------------- file-1 ward relations ---------------------------------------
    f1_old_code_use = {}
    for m in mapping["mappings"]:
        f1_old_code_use[m["old_code"]] = f1_old_code_use.get(m["old_code"], 0) + 1

    def resolve_old_unit(m, log: bool = True):
        """file-1 old side -> unit key; handles the two code-typo rows."""
        oc = m["old_code"]
        if not oc:
            return None
        unit = old_ward_key.get(oc)
        if unit is None:
            return None  # already materialized from file 1 above when missing
        f6_name = units[unit]["name"]
        if name_norm(m["old_name"]) == name_norm(f6_name):
            return unit
        # same code, different name: spelling variant, or a wrong code/name pair
        if _name_tokens(m["old_name"]) & _name_tokens(f6_name):
            return unit  # minor spelling variant of the same ward
        dc = _code_in_parens(m["old_district"])
        cands = f6_by_dist_name.get((dc, name_norm(m["old_name"])), [])
        cands = [c for c in cands if f1_old_code_use.get(c, 0) == 0]
        if len(cands) == 1:
            if log:
                report["code_corrections"].append(
                    {
                        "kind": "old_code_typo_resolved",
                        "file_code": oc,
                        "file_name": m["old_name"],
                        "resolved_code": cands[0],
                        "resolved_name": units[old_ward_key[cands[0]]]["name"],
                        "row": m["row"],
                    }
                )
            return old_ward_key[cands[0]]
        if log:
            ambiguous.append(
                {
                    "kind": "old_code_name_conflict",
                    "file_code": oc,
                    "file_name": m["old_name"],
                    "code_name": f6_name,
                    "district": m["old_district"],
                    "row": m["row"],
                    "decision": "kept code match" if not cands else "kept code match; name candidate already mapped elsewhere",
                }
            )
        return unit

    rel_seen = set()
    for m in mapping["mappings"]:
        rtype = classify_relation(m["note"])
        if name_norm(m["note"]) == "giu nguyen":
            if name_norm(m["old_name"]) != name_norm(m["new_name"]):
                rtype = "renamed_to"
            else:
                continue
        if rtype is None:
            ambiguous.append(
                {"kind": "unclassified_ghi_chu", "note": m["note"], "row": m["row"], "new_code": m["new_code"], "old_code": m["old_code"]}
            )
            continue
        from_key = resolve_old_unit(m)
        tgt = new_ward_units.get(m["new_code"])
        to_key = (tgt[1] or tgt[0]) if tgt else None
        if from_key is None or to_key is None:
            ambiguous.append({"kind": "relation_endpoint_missing", "row": m["row"], "old_code": m["old_code"], "new_code": m["new_code"]})
            continue
        sig = (from_key, to_key, rtype)
        if sig in rel_seen:
            continue
        rel_seen.add(sig)
        relations.append({"from": from_key, "to": to_key, "type": rtype, "effective": REFORM_DATE, "source_key": "mapping", "notes": m["note"]})
        if rtype == "renamed_to":
            aliases.append(
                {
                    "unit": to_key,
                    "alias": units[from_key]["name"],
                    "alias_norm": units[from_key]["name_norm"],
                    "type": "old_name",
                    "valid_from": units[from_key]["valid_from"],
                    "valid_to": units[from_key]["valid_to"],
                    "source_key": "mapping",
                }
            )

    # partial rows: whole-district -> đặc khu reorganizations (old ward fields
    # empty, 'Sắp xếp toàn bộ huyện X thành đặc khu X') and new units formed from
    # unincorporated land (no predecessor unit -> no relation).
    for m in mapping["partial"]:
        dc = _code_in_parens(m["old_district"])
        tgt = new_ward_units.get(m["new_code"])
        is_district_reorg = m["new_code"] and not m["old_code"] and not m["old_name"] and name_norm(m["note"]).startswith("sap xep toan bo")
        if is_district_reorg and dc in dist_key and tgt:
            relations.append(
                {
                    "from": dist_key[dc],
                    "to": (tgt[1] or tgt[0]),
                    "type": "merged_into",
                    "effective": REFORM_DATE,
                    "source_key": "mapping",
                    "notes": m["note"],
                }
            )
        else:
            ambiguous.append(
                {
                    "kind": "partial_row_no_relation",
                    "row": m["row"],
                    "new_code": m["new_code"],
                    "new_name": m["new_name"],
                    "old_name": m["old_name"],
                    "old_code": m["old_code"],
                    "note": m["note"],
                    "district": m["old_district"],
                }
            )

    # ---------------- aliases -----------------------------------------------------
    for r in prov_old:
        en = r["cells"][2]
        if en:
            aliases.append(
                {
                    "unit": old_prov_key[r["code"]],
                    "alias": en,
                    "alias_norm": name_norm(en),
                    "type": "english",
                    "valid_from": LEGACY_VALID_FROM,
                    "valid_to": OLD_CUTOFF,
                    "source_key": "prov_old",
                }
            )
    for r in dist_old:
        en = r["cells"][2]
        if en:
            aliases.append(
                {
                    "unit": dist_key[r["code"]],
                    "alias": en,
                    "alias_norm": name_norm(en),
                    "type": "english",
                    "valid_from": LEGACY_VALID_FROM,
                    "valid_to": OLD_CUTOFF,
                    "source_key": "dist_old",
                }
            )
    for r in ward_old:
        en = r["cells"][2]
        if en and r["code"] in old_ward_key:
            aliases.append(
                {
                    "unit": old_ward_key[r["code"]],
                    "alias": en,
                    "alias_norm": name_norm(en),
                    "type": "english",
                    "valid_from": LEGACY_VALID_FROM,
                    "valid_to": OLD_CUTOFF,
                    "source_key": "ward_old",
                }
            )
    for r in ward_new:
        en = r["cells"][2]
        if en and r["code"] in new_ward_units:
            key = new_ward_units[r["code"]][0]
            aliases.append(
                {
                    "unit": key,
                    "alias": en,
                    "alias_norm": name_norm(en),
                    "type": "english",
                    "valid_from": units[key]["valid_from"],
                    "valid_to": None,
                    "source_key": "ward_new",
                }
            )
    for r in prov_new:
        en = r["cells"][2]
        if en:
            key = prov_at(r["code"], "9999-12-31")
            aliases.append(
                {
                    "unit": key,
                    "alias": en,
                    "alias_norm": name_norm(en),
                    "type": "english",
                    "valid_from": units[key]["valid_from"],
                    "valid_to": None,
                    "source_key": "prov_new",
                }
            )

    # short_name for every unit whose name carries a known level prefix
    for key, u in units.items():
        sn = short_name(u["name"])
        if sn:
            aliases.append(
                {
                    "unit": key,
                    "alias": sn,
                    "alias_norm": name_norm(sn),
                    "type": "short_name",
                    "valid_from": u["valid_from"],
                    "valid_to": u["valid_to"],
                    "source_key": u["source_key"],
                }
            )
        else:
            report["no_short_name"] += 1

    # old_name evidence: file-1 old-side names that differ from the catalog name
    # (orthographic variants collapse to the same alias_norm and are skipped)
    seen_alias = set()
    for m in mapping["mappings"]:
        ok = resolve_old_unit(m, log=False)
        if ok and name_norm(m["old_name"]) != name_norm(units[ok]["name"]):
            sig = (ok, name_norm(m["old_name"]), "old_name")
            if sig not in seen_alias:
                seen_alias.add(sig)
                aliases.append(
                    {
                        "unit": ok,
                        "alias": m["old_name"],
                        "alias_norm": name_norm(m["old_name"]),
                        "type": "old_name",
                        "valid_from": units[ok]["valid_from"],
                        "valid_to": units[ok]["valid_to"],
                        "source_key": "mapping",
                    }
                )

    # old_name from file-7 narratives 'Đổi tên (từ) X thành Y' and DC-side names
    for r in doichieu:
        tgt = new_ward_units.get(r["code"])
        if not tgt:
            continue
        for hit in re.finditer(r"[Đđ]ổi tên(?:\s+từ)?\s+", r["note"] or ""):
            rest = (r["note"] or "")[hit.end() :]
            cut = rest.find(" thành ")
            if cut <= 0:
                continue
            old = rest[:cut].strip()
            if len(old) >= 6 and " " in old and not re.search(r"[\d%.…]", old):
                sig = (tgt[0], name_norm(old), "old_name")
                if sig not in seen_alias:
                    seen_alias.add(sig)
                    aliases.append(
                        {
                            "unit": tgt[0],
                            "alias": old,
                            "alias_norm": name_norm(old),
                            "type": "old_name",
                            "valid_from": None,
                            "valid_to": _day_before(units[tgt[0]]["valid_from"]),
                            "source_key": "doichieu",
                        }
                    )
        if r["dc_name"] and r["dc_code"] == r["code"] and name_norm(r["dc_name"]) != name_norm(r["name"]):
            ikey = tgt[1]
            sig = (tgt[0], name_norm(r["dc_name"]), "old_name")
            if ikey and sig not in seen_alias:
                seen_alias.add(sig)
                aliases.append(
                    {
                        "unit": tgt[0],
                        "alias": r["dc_name"],
                        "alias_norm": name_norm(r["dc_name"]),
                        "type": "old_name",
                        "valid_from": units[ikey]["valid_from"],
                        "valid_to": units[ikey]["valid_to"],
                        "source_key": "doichieu",
                    }
                )

    dedup = {}
    for a in aliases:
        dedup.setdefault((a["unit"], a["alias_norm"], a["type"]), a)
    return {"units": units, "aliases": list(dedup.values()), "relations": relations}


# -------------------------------------------------------------------- database


def write_db(db_path: Path, model, source_rows):
    """Rebuild the gazetteer in one transaction: failure rolls back cleanly."""
    existed = db_path.exists()
    con = sqlite3.connect(str(db_path))  # NOSONAR(S8706)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        for stmt in ("admin_relation", "admin_alias", "admin_unit", "gazetteer_source"):
            con.execute(f"DROP TABLE IF EXISTS {stmt}")
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                con.execute(stmt)
        sid = {}
        for key, row in source_rows.items():
            cur = con.execute(
                """INSERT INTO gazetteer_source (publisher, source_url, document_ref, effective_as_of, retrieved_at, sha256, license_id, rights_basis, redistribution_status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (PUBLISHER, row["url"], row["file"], row["as_of"], RETRIEVED_AT, row["sha256"], None, RIGHTS_BASIS, "unverified"),
            )
            sid[key] = cur.lastrowid
        uid = {}
        for key, u in model["units"].items():
            cur = con.execute(
                """INSERT INTO admin_unit (authority, code, name, name_norm, level, unit_type, parent_id, valid_from, valid_to, source_id, notes)
                   VALUES ('NSO',?,?,?,?,?,?,?,?,?,?)""",
                (
                    u["code"],
                    u["name"],
                    u["name_norm"],
                    u["level"],
                    u["unit_type"],
                    None,
                    u["valid_from"],
                    u["valid_to"],
                    sid[u["source_key"]],
                    u["notes"],
                ),
            )
            uid[key] = cur.lastrowid
        for key, u in model["units"].items():
            if u["parent_key"] is not None:
                con.execute("UPDATE admin_unit SET parent_id=? WHERE id=?", (uid[u["parent_key"]], uid[key]))
        for a in model["aliases"]:
            con.execute(
                """INSERT INTO admin_alias (unit_id, alias, alias_norm, alias_type, valid_from, valid_to, source_id) VALUES (?,?,?,?,?,?,?)""",
                (uid[a["unit"]], a["alias"], a["alias_norm"], a["type"], a["valid_from"], a["valid_to"], sid[a["source_key"]]),
            )
        for rel in model["relations"]:
            con.execute(
                """INSERT INTO admin_relation (from_unit_id, to_unit_id, relation_type, effective_date, source_id, notes) VALUES (?,?,?,?,?,?)""",
                (uid[rel["from"]], uid[rel["to"]], rel["type"], rel["effective"], sid[rel["source_key"]], rel["notes"]),
            )
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"foreign_key_check failed: {violations[:5]}")
        con.commit()
    except BaseException:
        con.rollback()
        con.close()
        if not existed:
            db_path.unlink(missing_ok=True)  # NOSONAR(S8707)
        raise
    con.close()


# ------------------------------------------------------------------------ main


def import_gazetteer(raw_dir: Path, db_path: Path, report_path: Path | None, allow_partial: bool = False):
    report = {
        "raw_dir": str(raw_dir),
        "db": str(db_path),
        "ambiguous": [],
        "code_corrections": [],
        "created_districts_from_mapping": 0,
        "created_wards_from_mapping": 0,
        "no_short_name": 0,
        "sources": {},
        "counts": {},
    }
    decrees = load_decrees()
    check_reform_milestone(decrees["reform_milestone"])
    raw: dict = {}
    for key, meta in SOURCES.items():
        path = raw_dir / meta["file"]
        if not path.exists():
            if not allow_partial:
                raise FileNotFoundError(f"missing source file: {path} (use --allow-partial to proceed without it)")
            raw[key] = None
            report["sources"][key] = {"file": meta["file"], "status": "missing"}
            continue
        sha = sha256_file(path)
        entry = {"file": meta["file"], "url": meta["url"], "as_of": meta["as_of"], "sha256": sha, "status": "ok"}
        report["sources"][key] = entry

    sources_path = find_manifest(raw_dir)
    if sources_path is not None:
        report["sources_manifest"] = str(sources_path)
        expected = {f["file"]: f["sha256"] for f in json.loads(sources_path.read_text(encoding="utf-8"))["files"]}
        for entry in report["sources"].values():
            exp = expected.get(entry["file"])
            if exp and exp != entry["sha256"]:
                raise ValueError(f"sha256 mismatch for {entry['file']}: file {entry['sha256'][:12]}… vs SOURCES.json {exp[:12]}…")
            if exp:
                entry["sha256_verified"] = True

    if (raw_dir / F_PROV_NEW).exists():
        raw["prov_new"] = parse_list_rows(read_xls(raw_dir / F_PROV_NEW), 5, 2)
    if (raw_dir / F_PROV_OLD).exists():
        raw["prov_old"] = parse_list_rows(read_xls(raw_dir / F_PROV_OLD), 5, 2)
    if (raw_dir / F_DIST_OLD).exists():
        raw["dist_old"] = parse_list_rows(read_xls(raw_dir / F_DIST_OLD), 7, 3)
    if (raw_dir / F_WARD_OLD).exists():
        raw["ward_old"] = parse_list_rows(read_xls(raw_dir / F_WARD_OLD), 9, 5)
    if (raw_dir / F_WARD_NEW).exists():
        raw["ward_new"] = parse_list_rows(read_xls(raw_dir / F_WARD_NEW), 7, 5)
    if (raw_dir / F_MAPPING).exists():
        m, dv, pa = parse_mapping_rows(read_xlsx(raw_dir / F_MAPPING, "không merge"))
        raw["mapping"] = {"mappings": m, "dividers": dv, "partial": pa}
        report["mapping_dividers"] = dv
    if (raw_dir / F_DOI_CHIEU).exists():
        dm = []
        raw["doichieu"] = parse_doichieu_rows(read_xls(raw_dir / F_DOI_CHIEU, dm), dm[0])

    for key in ("prov_new", "prov_old", "dist_old", "ward_old", "ward_new"):
        if raw.get(key) is not None and not raw[key]:
            raise ValueError(f"{SOURCES[key]['file']}: no data rows parsed")

    model = build_model(raw, report, decrees["documents"])
    counts = {"units": {}, "relations": {}, "aliases": {}}
    for u in model["units"].values():
        counts["units"][f"{u['level']}|{u['valid_from'][:4]}"] = counts["units"].get(f"{u['level']}|{u['valid_from'][:4]}", 0) + 1
    for r in model["relations"]:
        counts["relations"][r["type"]] = counts["relations"].get(r["type"], 0) + 1
    for a in model["aliases"]:
        counts["aliases"][a["type"]] = counts["aliases"].get(a["type"], 0) + 1
    counts["units_total"] = len(model["units"])
    counts["relations_total"] = len(model["relations"])
    counts["aliases_total"] = len(model["aliases"])
    report["counts"] = counts

    db_path.parent.mkdir(parents=True, exist_ok=True)
    write_db(db_path, model, {k: v for k, v in report["sources"].items() if v.get("status") == "ok"})
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")  # NOSONAR(S8707)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description="Import NSO administrative gazetteer into data/gazetteer.sqlite3")
    p.add_argument("--raw", type=Path, default=Path("data/gazetteer/raw"))
    p.add_argument("--db", type=Path, default=Path("data/gazetteer.sqlite3"))
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--allow-partial", action="store_true", help="import whatever source files exist (not recommended)")
    a = p.parse_args(argv)
    try:
        rep = import_gazetteer(a.raw, a.db, a.report, a.allow_partial)
        print(
            json.dumps(
                {"ok": True, "counts": rep["counts"], "ambiguous": len(rep["ambiguous"]), "code_corrections": len(rep["code_corrections"])},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
