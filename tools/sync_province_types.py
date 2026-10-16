#!/usr/bin/env python3
"""Sync provinces_34.json admin-type metadata (card V1-03) from the gazetteer.

Idempotent: adds ``unit_type`` / ``type_effective_from`` / ``type_source`` to
each entry, driven by the gazetteer's current province rows (2045-level truth)
and DECREES.json for QH16 type-change dates. NEVER edits names, ``level``,
``effective_from``, ``source`` or ``note`` — card V1-03 keeps level=province
(it is the admin hierarchy level; the "thành phố" distinction lives in
unit_type only) and touches no polygon or admin code.

``--check`` verifies only (exit 1 when the file differs from the expected sync).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVINCES = ROOT / "data" / "provinces_34.json"
GAZETTEER = ROOT / "data" / "gazetteer.sqlite3"
DECREES = ROOT / "data" / "gazetteer" / "DECREES.json"

CITY_TYPE = "Thành phố Trung ương"
PROVINCE_TYPE = "Tỉnh"


def _bare_name(gaz_name: str) -> str:
    """'Tỉnh An Giang' / 'Thành phố Hồ Chí Minh' -> 'An Giang' / 'Hồ Chí Minh'."""
    for prefix in ("Thành phố ", "Tỉnh "):
        if gaz_name.startswith(prefix):
            return gaz_name[len(prefix) :].strip()
    return gaz_name.strip()


def load_current_provinces() -> dict[str, dict]:
    """Gazetteer current province rows keyed by the bare display name."""
    mapping: dict[str, dict] = {}
    with sqlite3.connect(str(GAZETTEER)) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute("SELECT code, name, unit_type, valid_from FROM admin_unit WHERE level='province' AND valid_to IS NULL"):
            bare = _bare_name(row["name"])
            if bare in mapping:
                raise SystemExit(f"Nghi ngờ gazetteer: 2 đơn vị cấp tỉnh cùng tên {bare!r}")
            mapping[bare] = dict(row)
    return mapping


def load_qh16_documents() -> dict[tuple[str, str], str]:
    """{(province_code, effective_on) -> document_id} cho QH16 type changes."""
    docs = json.loads(DECREES.read_text(encoding="utf-8")).get("documents", [])
    out: dict[tuple[str, str], str] = {}
    for doc in docs:
        for code in doc.get("province_codes", []):
            out[(code, doc["effective_on"])] = doc["document_id"]
    return out


def expected_metadata() -> dict[str, dict]:
    """Expected unit_type fields per existing provinces_34.json entry name."""
    gaz = load_current_provinces()
    qh16 = load_qh16_documents()
    reform = json.loads(DECREES.read_text(encoding="utf-8")).get("reform_milestone", {})
    reform_doc = reform.get("document_id")
    reform_date = reform.get("effective_on")
    out: dict[str, dict] = {}
    for bare, row in gaz.items():
        unit_type = row["unit_type"]
        if unit_type not in (PROVINCE_TYPE, CITY_TYPE):
            raise SystemExit(f"unit_type ngoài quy định: {bare!r} -> {unit_type!r}")
        meta = {
            "unit_type": unit_type,
            "type_effective_from": row["valid_from"],
            "type_source": qh16.get((row["code"], row["valid_from"])) or reform_doc,
        }
        if reform_date and meta["type_source"] == reform_doc and meta["type_effective_from"] != reform_date:
            raise SystemExit(f"Mốc không trùng reform_milestone cho {bare!r}: {meta['type_effective_from']} != {reform_date}")
        out[bare] = meta
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="chỉ kiểm tra, không ghi")
    args = parser.parse_args()

    expected = expected_metadata()
    entries = json.loads(PROVINCES.read_text(encoding="utf-8"))
    if len(entries) != 34:
        raise SystemExit(f"provinces_34.json phải có 34 đơn vị, thấy {len(entries)}")

    untouched: list[tuple] = []
    mismatch: list[str] = []
    for entry in entries:
        name = entry["name"]
        untouched.append((name, entry.get("level"), entry.get("effective_from"), entry.get("source"), entry.get("note")))
        want = expected.get(name)
        if want is None:
            mismatch.append(f"{name}: không khớp đơn vị nào trong gazetteer hiện hành")
            continue
        got = (entry.get("unit_type"), entry.get("type_effective_from"), entry.get("type_source"))
        if got != (want["unit_type"], want["type_effective_from"], want["type_source"]):
            mismatch.append(f"{name}: got={got} want={(want['unit_type'], want['type_effective_from'], want['type_source'])}")

    if len(untouched) != 34:
        raise SystemExit("số entry không hợp")
    if mismatch:
        print("Cần sync:")
        for line in mismatch:
            print(" -", line)
        if args.check:
            return 1

    if args.check:
        if mismatch:
            return 1
        print("CHECK OK: metadata 34 đơn vị khớp gazetteer + DECREES.")
        return 0

    for entry in entries:
        want = expected[entry["name"]]
        entry["unit_type"] = want["unit_type"]
        entry["type_effective_from"] = want["type_effective_from"]
        entry["type_source"] = want["type_source"]
    PROVINCES.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Đã ghi {len(entries)} entry vào {PROVINCES.name} (tên/level/ngày gốc giữ nguyên).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
