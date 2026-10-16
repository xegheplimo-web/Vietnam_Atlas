"""Tests for tools/import_gazetteer.py (T-E2a — NSO gazetteer import).

Unit tests exercise the pure parsers/model on fabricated rows; the integration
class runs the real 7-file import only when data/gazetteer/raw exists (skipped
in CI, which has no raw NSO copies).
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "gazetteer" / "raw"

_SPEC = importlib.util.spec_from_file_location("import_gazetteer", REPO_ROOT / "tools" / "import_gazetteer.py")
ig = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ig)


class TestPureHelpers(unittest.TestCase):
    def test_cell_text(self):
        self.assertEqual(ig.cell_text(None), "")
        self.assertEqual(ig.cell_text(4.0), "4")
        self.assertEqual(ig.cell_text("  Phường \n Hoàn  Kiếm "), "Phường Hoàn Kiếm")

    def test_norm_code_zero_padding(self):
        self.assertEqual(ig.norm_code("00004", 5), "00004")
        self.assertEqual(ig.norm_code(2494.0, 5), "02494")
        self.assertEqual(ig.norm_code("1", 3), "001")
        self.assertEqual(ig.norm_code("", 5), "")
        self.assertEqual(ig.norm_code("abc", 5), "")
        self.assertEqual(ig.norm_code("123456", 5), "")

    def test_name_norm(self):
        self.assertEqual(ig.name_norm("Phường Ba Đình"), "phuong ba dinh")
        self.assertEqual(ig.name_norm("Thành phố Đà Nẵng"), "thanh pho da nang")
        self.assertEqual(ig.name_norm("Đắk Lắk"), "dak lak")
        self.assertEqual(ig.name_norm("Phường \nHoàn Kiếm"), "phuong hoan kiem")
        self.assertEqual(ig.name_norm("Xã Ea H'Leo"), ig.name_norm("Xã Ea H\u2019Leo"))  # curly apostrophe
        self.assertEqual(ig.name_norm("Xã Trung Giã"), ig.name_norm("Xã Trung Giã"))  # NFD vs NFC

    def test_short_name(self):
        self.assertEqual(ig.short_name("Thành phố Hà Nội"), "Hà Nội")
        self.assertEqual(ig.short_name("Phường Ba Đình"), "Ba Đình")
        self.assertEqual(ig.short_name("Tỉnh Cà Mau"), "Cà Mau")
        self.assertEqual(ig.short_name("Đặc khu Bạch Long Vĩ"), "Bạch Long Vĩ")
        self.assertEqual(ig.short_name("Thị trấn Rạch Gốc"), "Rạch Gốc")
        self.assertEqual(ig.short_name("Quận 1"), "1")
        self.assertEqual(ig.short_name("Bão Đảo"), "")

    def test_find_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            self.assertIsNone(ig.find_manifest(raw))
            (root / "SOURCES.json").write_text("{}", encoding="utf-8")
            self.assertEqual(ig.find_manifest(raw), root / "SOURCES.json")
            (raw / "SOURCES.json").write_text("{}", encoding="utf-8")
            self.assertEqual(ig.find_manifest(raw), raw / "SOURCES.json")  # beside-raw wins

    def test_unit_type_from_name(self):
        self.assertEqual(ig.unit_type_from_name("Thị Xã Chũ"), "Thị xã")
        self.assertEqual(ig.unit_type_from_name("Thị trấn Vôi"), "Thị trấn")
        self.assertEqual(ig.unit_type_from_name("Đặc khu Cồn Cỏ"), "Đặc khu")
        self.assertEqual(ig.unit_type_from_name("Xã Bố Hạ"), "Xã")
        self.assertEqual(ig.unit_type_from_name("Phường 15"), "Phường")

    def test_excel_serial_to_iso(self):
        self.assertEqual(ig.excel_serial_to_iso(45839.0, 0), "2025-07-01")
        self.assertEqual(ig.excel_serial_to_iso(46285.0, 0), "2026-09-20")
        self.assertEqual(ig.excel_serial_to_iso(46142.0, 0), "2026-04-30")

    def test_parse_decree_date(self):
        self.assertEqual(ig.parse_decree_date("36/2026/QH16 - 24/08/2026"), "2026-08-24")
        self.assertEqual(ig.parse_decree_date("Số: 1656/NQ-UBTVQH15; Ngày: 16/06/2025"), "2025-06-16")
        self.assertEqual(ig.parse_decree_date(""), "")

    def test_is_2026_decree(self):
        self.assertTrue(ig.is_2026_decree("39/2026/QH16 - 24/08/2026"))
        self.assertTrue(ig.is_2026_decree("Số: 388/NQ-UBTVQH16; Ngày: 04/08/2026"))
        self.assertFalse(ig.is_2026_decree("202/2025/QH15 - 12/06/2025"))
        self.assertFalse(ig.is_2026_decree("Số: 1656/NQ-UBTVQH15; Ngày: 16/06/2025"))

    def test_classify_relation(self):
        self.assertEqual(ig.classify_relation("Nhập toàn bộ"), "merged_into")
        self.assertEqual(ig.classify_relation("Nhập toàn bộ diện tích"), "merged_into")
        self.assertEqual(ig.classify_relation("Nhập một phần"), "partially_into")
        self.assertEqual(ig.classify_relation("Nhập một phần diện tích, toàn bộ dân số"), "partially_into")
        self.assertEqual(ig.classify_relation("Nhâp một phần diện tích"), "partially_into")
        self.assertEqual(ig.classify_relation("Phần còn lại sau khi tách một phần vào xã Canh Vinh"), "partially_into")
        self.assertEqual(ig.classify_relation("Đổi tên"), "renamed_to")
        self.assertEqual(ig.classify_relation("Đổi loại hình"), "type_changed")
        self.assertEqual(ig.classify_relation("Đổi tên từ một phần Phường 15"), "partially_into")
        self.assertEqual(ig.classify_relation("Sắp xếp toàn bộ huyện Cồn Cỏ thành đặc khu Cồn Cỏ"), "merged_into")
        self.assertIsNone(ig.classify_relation("Giữ nguyên"))
        self.assertIsNone(ig.classify_relation(""))


class TestDecreeMetadata(unittest.TestCase):
    """T-E2c: DECREES.json (ngày hiệu lực) drives validity, not ban hành."""

    DOCS: ClassVar[list] = [
        {"document_id": "30/2026/QH16", "effective_on": "2026-04-30"},
        {"document_id": "36/2026/QH16", "effective_on": "2026-09-01"},
        {"document_id": "39/2026/QH16", "effective_on": "2026-09-20"},
    ]

    def test_decree_document_id(self):
        self.assertEqual(ig.decree_document_id("39/2026/QH16 - 24/08/2026"), "39/2026/QH16")
        self.assertEqual(ig.decree_document_id("36/2026/QH16 - 24/08/2026"), "36/2026/QH16")
        self.assertEqual(ig.decree_document_id("Số: 388/NQ-UBTVQH16; Ngày: 04/08/2026"), "388/NQ-UBTVQH16")
        self.assertEqual(ig.decree_document_id("202/2025/QH15 - 12/06/2025"), "202/2025/QH15")
        self.assertEqual(ig.decree_document_id(""), "")

    def test_load_decrees_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = ig.load_decrees(Path(tmp) / "none.json")
        self.assertEqual(data["documents"], [])
        self.assertIsNone(data["reform_milestone"])

    def test_load_decrees_tmp_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "DECREES.json"
            p.write_text('{"documents": [{"document_id": "1/2026/QH16", "effective_on": "2026-05-01"}]}', encoding="utf-8")
            data = ig.load_decrees(p)
        self.assertEqual(data["documents"][0]["effective_on"], "2026-05-01")

    def test_load_decrees_reads_committed_file(self):
        data = ig.load_decrees()
        ids = [d["document_id"] for d in data["documents"]]
        self.assertEqual(ids, ["30/2026/QH16", "36/2026/QH16", "39/2026/QH16"])
        self.assertEqual(data["reform_milestone"]["effective_on"], ig.REFORM_DATE)

    def test_check_reform_milestone(self):
        ig.check_reform_milestone(None)  # missing file -> ok
        ig.check_reform_milestone({"effective_on": ig.REFORM_DATE})
        with self.assertRaises(ValueError):
            ig.check_reform_milestone({"effective_on": "2025-08-01"})

    def test_find_decrees_and_effective_on(self):
        self.assertEqual(ig.find_decrees("39/2026/QH16", self.DOCS)[0]["effective_on"], "2026-09-20")
        self.assertEqual(ig.find_decrees("388/NQ-UBTVQH16", self.DOCS), [])
        self.assertEqual(ig.find_decrees("", self.DOCS), [])
        self.assertEqual(ig.decree_effective_on("36/2026/QH16", self.DOCS), "2026-09-01")
        self.assertEqual(ig.decree_effective_on("388/NQ-UBTVQH16", self.DOCS), "")

    def test_decree_valid_from_prefers_metadata(self):
        self.assertEqual(ig.decree_valid_from("30/2026/QH16 - 24/04/2026", self.DOCS), "2026-04-30")
        self.assertEqual(ig.decree_valid_from("36/2026/QH16 - 24/08/2026", self.DOCS), "2026-09-01")
        self.assertEqual(ig.decree_valid_from("39/2026/QH16 - 24/08/2026", self.DOCS), "2026-09-20")
        # unknown id -> historical issued-date parse (ban hành)
        self.assertEqual(ig.decree_valid_from("Số: 388/NQ-UBTVQH16; Ngày: 04/08/2026", self.DOCS), "2026-08-04")
        # non-2026 decree / empty cell -> reform date
        self.assertEqual(ig.decree_valid_from("202/2025/QH15 - 12/06/2025", self.DOCS), ig.REFORM_DATE)
        self.assertEqual(ig.decree_valid_from("", self.DOCS), ig.REFORM_DATE)


class TestParsers(unittest.TestCase):
    def test_list_rows_skips_header_and_footer(self):
        rows = [
            ["Mã", "Tên", "Tên Tiếng Anh", "Cấp", "Nghị định"],
            ["01", "Thành phố Hà Nội", "", "Thành phố Trung ương", ""],
            ["", "Số lượng : 34", "", "", ""],
        ]
        out = ig.parse_list_rows(rows, 5, 2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "01")
        self.assertEqual(out[0]["cells"][1], "Thành phố Hà Nội")

    def test_list_rows_pads_numeric_codes(self):
        rows = [["Mã", "Tên"], [4.0, "Tỉnh Cao Bằng"], [None, "Số lượng : 1", None]]
        out = ig.parse_list_rows(rows, 2, 2)
        self.assertEqual(out[0]["code"], "04")

    def test_mapping_rows(self):
        rows = [
            ["Đơn vị hành chính từ ngày 01/7/2025", None, None, None, None, None, None, None],
            ["Tỉnh, thành phố", "Tên Xã mới", "Mã Xã mới", "Tên Xã cũ", "Mã Xã cũ", "Ghi chú", "Quận/huyện", "Tỉnh cũ"],
            ["THÀNH PHỐ HÀ NỘI  ", None, None, None, None, None, None, None],
            [
                "Thành phố Hà Nội (01)",
                "Phường Ba Đình",
                "00004",
                "Phường Trúc Bạch",
                "00004",
                "Nhập toàn bộ",
                "Quận Ba Đình (001)",
                "Thành phố Hà Nội (01)",
            ],
            [
                "Thành phố Hải Phòng (31)",
                "Đặc khu Bạch Long Vĩ",
                "11948",
                "",
                "",
                "Sắp xếp toàn bộ huyện Bạch Long Vĩ thành đặc khu Bạch Long Vĩ",
                "Huyện Bạch Long Vĩ (318)",
                "Thành phố Hải Phòng (31)",
            ],
            [None] * 8,
        ]
        m, dv, pa = ig.parse_mapping_rows(rows)
        self.assertEqual(dv, 1)
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["new_code"], "00004")
        self.assertEqual(m[0]["old_code"], "00004")
        self.assertEqual(len(pa), 1)
        self.assertEqual(pa[0]["new_code"], "11948")
        self.assertEqual(pa[0]["old_code"], "")

    def test_doichieu_rows_serial_dates(self):
        rows = [
            [
                "Tỉnh",
                "Tên Tỉnh",
                "Xã",
                "Tên Xã",
                "Nghị định",
                "Ngày hiệu lực",
                "Tên Xã DC",
                "Xã DC",
                "Nghị định",
                "Ngày hiệu lực",
                "Tên Tỉnh DC",
                "Tỉnh DC",
                "Ghi Chú",
            ],
            [
                "24",
                "Thành phố Bắc Ninh",
                "07294",
                "Phường Bố Hạ",
                "Số: 388/NQ-UBTVQH16; Ngày: 04/08/2026",
                46285.0,
                "Xã Bố Hạ",
                "07294",
                "Số: 1658/NQ-UBTVQH15; Ngày: 16/06/2025",
                45839.0,
                "Tỉnh Bắc Ninh",
                "24",
                "Đổi loại hình...",
            ],
        ]
        out = ig.parse_doichieu_rows(rows, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["effective"], "2026-09-20")
        self.assertEqual(out[0]["dc_effective"], "2025-07-01")
        self.assertEqual(out[0]["dc_name"], "Xã Bố Hạ")


def _synthetic_raw():
    """A minimal fabricated dataset covering QH16 upgrade + Bắc Giang-style rebuild."""
    return {
        "prov_old": [
            {"code": "24", "cells": ["24", "Tỉnh Bắc Giang", "", "Tỉnh", ""]},
            {"code": "04", "cells": ["04", "Tỉnh Cao Bằng", "", "Tỉnh", ""]},
        ],
        "prov_new": [
            {"code": "24", "cells": ["24", "Thành phố Bắc Ninh", "", "Thành phố Trung ương", "39/2026/QH16 - 24/08/2026"]},
            {"code": "01", "cells": ["01", "Thành phố Hà Nội", "", "Thành phố Trung ương", ""]},
        ],
        "dist_old": [
            {"code": "256", "cells": ["256", "Thành phố Bắc Ninh", "Bac Ninh City", "Thành phố", "", "24", "Tỉnh Bắc Ninh"]},
            {"code": "049", "cells": ["049", "Huyện Quảng Hòa", "", "Huyện", "", "04", "Tỉnh Cao Bằng"]},
        ],
        "ward_old": [
            {"code": "09163", "cells": ["09163", "Phường Vũ Ninh", "", "Phường", "", "27", "256", "Thành phố Bắc Ninh", "Tỉnh Bắc Ninh"]},
            {"code": "01615", "cells": ["01615", "Xã Hồng Quang", "", "Xã", "", "04", "049", "Huyện Quảng Hòa", "Tỉnh Cao Bằng"]},
            {"code": "01651", "cells": ["01651", "Xã Mỹ Hưng", "", "Xã", "", "04", "049", "Huyện Quảng Hòa", "Tỉnh Cao Bằng"]},
        ],
        "ward_new": [
            {"code": "07294", "cells": ["07294", "Phường Bố Hạ", "", "Phường", "Số: 388/NQ-UBTVQH16; Ngày: 04/08/2026", "24", "Thành phố Bắc Ninh"]},
            {"code": "00004", "cells": ["00004", "Phường Ba Đình", "", "Phường", "Số: 1656/NQ-UBTVQH15; Ngày: 16/06/2025", "01", "Thành phố Hà Nội"]},
            {"code": "01615", "cells": ["01615", "Xã Bế Văn Đàn", "", "Xã", "", "04", "Tỉnh Cao Bằng"]},
            {"code": "09163", "cells": ["09163", "Phường Vũ Ninh", "", "Phường", "", "24", "Thành phố Bắc Ninh"]},
        ],
        "mapping": {
            "dividers": 1,
            "partial": [],
            "mappings": [
                {
                    "row": 4,
                    "new_prov": "Tỉnh Bắc Ninh (24)",
                    "new_name": "Xã Bố Hạ",
                    "new_code": "07294",
                    "old_name": "Thị trấn Bố Hạ",
                    "old_code": "07294",
                    "note": "Nhập toàn bộ",
                    "old_district": "Huyện Yên Thế (215)",
                    "old_prov": "Tỉnh Bắc Giang (24)",
                },
                {
                    "row": 5,
                    "new_prov": "Thành phố Hà Nội (01)",
                    "new_name": "Phường Ba Đình",
                    "new_code": "00004",
                    "old_name": "Phường Trúc Bạch",
                    "old_code": "00004",
                    "note": "Đổi tên",
                    "old_district": "Quận Ba Đình (001)",
                    "old_prov": "Thành phố Hà Nội (01)",
                },
                {
                    "row": 6,
                    "new_prov": "Tỉnh Cao Bằng (04)",
                    "new_name": "Xã Bế Văn Đàn",
                    "new_code": "01615",
                    "old_name": "Xã Mỹ Hưng",
                    "old_code": "01615",
                    "note": "Nhập toàn bộ",
                    "old_district": "Huyện Quảng Hòa (049)",
                    "old_prov": "Tỉnh Cao Bằng (04)",
                },
            ],
        },
        "doichieu": [
            {
                "prov_code": "24",
                "prov_name": "Thành phố Bắc Ninh",
                "code": "07294",
                "name": "Phường Bố Hạ",
                "decree": "Số: 388/NQ-UBTVQH16; Ngày: 04/08/2026",
                "effective": "2026-09-20",
                "dc_name": "Xã Bố Hạ",
                "dc_code": "07294",
                "dc_decree": "Số: 1658/NQ-UBTVQH15; Ngày: 16/06/2025",
                "dc_effective": "2025-07-01",
                "dc_prov_name": "Tỉnh Bắc Ninh",
                "dc_prov_code": "24",
                "note": "Đổi loại hình, hợp nhất thị trấn Bố Hạ... Thành lập phường Bố Hạ từ xã Bố Hạ",
            },
            {
                "prov_code": "01",
                "prov_name": "Thành phố Hà Nội",
                "code": "00004",
                "name": "Phường Ba Đình",
                "decree": "Số: 1656/NQ-UBTVQH15; Ngày: 16/06/2025",
                "effective": "2025-07-01",
                "dc_name": "Phường Ba Đình",
                "dc_code": "00004",
                "dc_decree": "",
                "dc_effective": "2025-07-01",
                "dc_prov_name": "Thành phố Hà Nội",
                "dc_prov_code": "01",
                "note": "Đổi tên từ phường Trúc Bạch thành phường Ba Đình, hợp nhất...",
            },
        ],
    }


def _report_stub():
    return {
        "ambiguous": [],
        "code_corrections": [],
        "created_districts_from_mapping": 0,
        "created_wards_from_mapping": 0,
        "no_short_name": 0,
        "sources": {},
        "counts": {},
    }


class TestBuildModelSynthetic(unittest.TestCase):
    def setUp(self):
        self.report = _report_stub()
        self.model = ig.build_model(_synthetic_raw(), self.report)

    def unit(self, level, code, vf):
        return self.model["units"][(level, code, vf)]

    def test_qh16_province_chain(self):
        inter = self.unit("province", "24", "2025-07-01")
        self.assertEqual(inter["name"], "Tỉnh Bắc Ninh")
        self.assertEqual(inter["unit_type"], "Tỉnh")
        self.assertEqual(inter["valid_to"], "2026-09-19")
        cur = self.unit("province", "24", "2026-09-20")
        self.assertEqual(cur["name"], "Thành phố Bắc Ninh")
        self.assertEqual(cur["unit_type"], "Thành phố Trung ương")
        self.assertIsNone(cur["valid_to"])
        rel = [r for r in self.model["relations"] if r["type"] == "type_changed" and self.model["units"][r["from"]]["level"] == "province"]
        self.assertEqual(len(rel), 1)
        self.assertEqual(rel[0]["effective"], "2026-09-20")

    def test_decree_metadata_beats_issued_date(self):
        # hiệu lực (DECREES.json) != ngày ban hành (cell): '39/2026/QH16 -
        # 24/08/2026' must land on 2026-09-20, never on parse_decree_date's 2026-08-24.
        cur = self.unit("province", "24", "2026-09-20")
        self.assertEqual(cur["valid_from"], "2026-09-20")
        self.assertNotEqual(cur["valid_from"], ig.parse_decree_date("39/2026/QH16 - 24/08/2026"))

    def test_ward_three_versions(self):
        old = self.unit("ward", "07294", "0001-01-01")
        self.assertEqual(old["name"], "Thị trấn Bố Hạ")
        self.assertEqual(old["valid_to"], "2025-06-30")
        # created-from-mapping ward hangs under the also-created district 215
        dist = self.unit("district", "215", "0001-01-01")
        self.assertEqual(old["parent_key"], ("district", "215", "0001-01-01"))
        self.assertEqual(dist["parent_key"], ("province", "24", "0001-01-01"))
        inter = self.unit("ward", "07294", "2025-07-01")
        self.assertEqual(inter["name"], "Xã Bố Hạ")
        self.assertEqual(inter["valid_to"], "2026-09-19")
        self.assertEqual(inter["parent_key"], ("province", "24", "2025-07-01"))
        cur = self.unit("ward", "07294", "2026-09-20")
        self.assertEqual(cur["name"], "Phường Bố Hạ")
        self.assertEqual(cur["parent_key"], ("province", "24", "2026-09-20"))

    def test_relations(self):
        types = sorted((self.model["units"][r["from"]]["name"], r["type"], self.model["units"][r["to"]]["name"]) for r in self.model["relations"])
        self.assertIn(("Thị trấn Bố Hạ", "merged_into", "Xã Bố Hạ"), types)
        self.assertIn(("Xã Bố Hạ", "type_changed", "Phường Bố Hạ"), types)
        self.assertIn(("Tỉnh Bắc Ninh", "type_changed", "Thành phố Bắc Ninh"), types)
        # 'Đổi tên' on code 00004: old ward was missing from ward_old so it is
        # rebuilt from file 1 and renamed_to links to the current unit
        self.assertIn(("Phường Trúc Bạch", "renamed_to", "Phường Ba Đình"), types)

    def test_notes_and_aliases(self):
        cur = self.unit("ward", "07294", "2026-09-20")
        self.assertIn("Đổi loại hình", cur["notes"])
        alias_norms = {(a["unit"], a["alias_norm"], a["type"]) for a in self.model["aliases"]}
        self.assertIn((("ward", "07294", "2026-09-20"), "xa bo ha", "old_name"), alias_norms)
        self.assertIn((("ward", "00004", "2025-07-01"), "ba dinh", "short_name"), alias_norms)
        self.assertIn((("district", "256", "0001-01-01"), "bac ninh city", "english"), alias_norms)

    def test_reform_ward_parent_tracks_province_upgrade(self):
        # A ward surviving the 2026 province upgrade unchanged must point at the
        # CURRENT province version (TP TW, open-ended) — not the expired 'Tỉnh'
        # version (regression: wards kept expired parents after QH16).
        cur = self.unit("ward", "09163", "2025-07-01")
        self.assertIsNone(cur["valid_to"])
        self.assertEqual(cur["parent_key"], ("province", "24", "2026-09-20"))

    def test_ward_fallback_branch_uses_decree_metadata(self):
        # no file-7 row for 07294 -> the decree branch fires; with metadata for
        # '388/NQ-UBTVQH16' the ward lands on hiệu lực, not ban hành.
        docs = [{"document_id": "388/NQ-UBTVQH16", "effective_on": "2026-09-20"}]
        raw = _synthetic_raw()
        raw["doichieu"] = []
        model = ig.build_model(raw, _report_stub(), decree_docs=docs)
        cur = model["units"][("ward", "07294", "2026-09-20")]
        self.assertEqual(cur["name"], "Phường Bố Hạ")

    def test_ward_fallback_branch_without_metadata_parses_issued_date(self):
        raw = _synthetic_raw()
        raw["doichieu"] = []
        model = ig.build_model(raw, _report_stub(), decree_docs=[])
        cur = model["units"][("ward", "07294", "2026-08-04")]
        self.assertEqual(cur["name"], "Phường Bố Hạ")
        rel = [r for r in model["relations"] if r["type"] == "type_changed" and model["units"][r["from"]]["code"] == "07294"]
        self.assertEqual(rel[0]["effective"], "2026-08-04")

    def test_typo_correction_redirects_alias_and_relation(self):
        corr = self.report["code_corrections"]
        self.assertEqual(len(corr), 1)
        self.assertEqual(corr[0]["file_code"], "01615")
        self.assertEqual(corr[0]["resolved_code"], "01651")
        # the alias pass follows the same correction: no 'Xã Mỹ Hưng' old_name
        # smeared onto the wrong ward 01615 (Xã Hồng Quang)
        pairs = {(a["unit"], a["alias_norm"], a["type"]) for a in self.model["aliases"]}
        self.assertNotIn((("ward", "01615", "0001-01-01"), "my hung", "old_name"), pairs)
        rels = {(self.model["units"][r["from"]]["name"], r["type"], self.model["units"][r["to"]]["name"]) for r in self.model["relations"]}
        self.assertIn(("Xã Mỹ Hưng", "merged_into", "Xã Bế Văn Đàn"), rels)


@unittest.skipUnless(RAW_DIR.is_dir(), "raw NSO files not present (dev machine only)")
class TestRealImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.db = cls.tmp / "gaz.sqlite3"
        rep = ig.import_gazetteer(RAW_DIR, cls.db, cls.tmp / "report.json")
        cls.report = rep
        cls.con = sqlite3.connect(str(cls.db))
        cls.con.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls._tmp.cleanup()

    def q(self, sql, *args):
        return self.con.execute(sql, args).fetchall()

    def test_counts(self):
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_unit WHERE level='province' AND valid_to IS NULL")[0]["c"], 34)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_unit WHERE level='province' AND valid_to='2025-06-30'")[0]["c"], 63)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_unit WHERE level='district'")[0]["c"], 696)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_unit WHERE level='ward' AND valid_to='2025-06-30'")[0]["c"], 10035)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_unit WHERE level='ward' AND valid_to IS NULL")[0]["c"], 3321)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM gazetteer_source")[0]["c"], 7)

    def test_no_duplicates(self):
        dup = self.q("SELECT authority, level, code, valid_from, COUNT(*) c FROM admin_unit GROUP BY 1,2,3,4 HAVING c>1")
        self.assertEqual(dup, [])
        drel = self.q("SELECT from_unit_id, to_unit_id, relation_type, COUNT(*) c FROM admin_relation GROUP BY 1,2,3 HAVING c>1")
        self.assertEqual(drel, [])

    def test_relations_sources_not_null(self):
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_relation WHERE source_id IS NULL")[0]["c"], 0)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM admin_relation WHERE from_unit_id IS NULL OR to_unit_id IS NULL")[0]["c"], 0)

    def test_acceptance_qh16_provinces(self):
        # Gate T-E2c: hiệu lực from DECREES.json, not the ban-hành date in the
        # NSO cells: 75 -> 2026-04-30, 22 -> 2026-09-01, 24 -> 2026-09-20.
        for code, expected_vf in (("22", "2026-09-01"), ("24", "2026-09-20"), ("75", "2026-04-30")):
            rows = self.q(
                "SELECT name, unit_type, valid_from FROM admin_unit WHERE code=? AND level='province'"
                " AND valid_from<='2026-10-16' AND (valid_to IS NULL OR valid_to>='2026-10-16')",
                code,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["unit_type"], "Thành phố Trung ương")
            self.assertEqual(rows[0]["valid_from"], expected_vf)
            self.assertTrue(rows[0]["name"].startswith("Thành phố "))
            inter = self.q(
                "SELECT unit_type, valid_from, valid_to FROM admin_unit WHERE code=? AND level='province' AND valid_from='2025-07-01'",
                code,
            )
            self.assertEqual(len(inter), 1)
            self.assertEqual(inter[0]["unit_type"], "Tỉnh")
            self.assertEqual(inter[0]["valid_from"], "2025-07-01")
            self.assertEqual(inter[0]["valid_to"], (date.fromisoformat(expected_vf) - timedelta(days=1)).isoformat())

    def test_acceptance_qh16_province_as_of(self):
        # as_of before the hiệu lực milestone resolves to the intermediate
        # 'Tỉnh' version; on/after it resolves to the TP TW version.
        for code, eff in (("75", "2026-04-30"), ("22", "2026-09-01"), ("24", "2026-09-20")):
            before = self.q(
                "SELECT unit_type FROM admin_unit WHERE code=? AND level='province' AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
                code,
                "2026-01-01",
                "2026-01-01",
            )
            self.assertEqual(before[0]["unit_type"], "Tỉnh")
            at = self.q(
                "SELECT unit_type, valid_from FROM admin_unit WHERE code=? AND level='province'"
                " AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
                code,
                eff,
                eff,
            )
            self.assertEqual(at[0]["unit_type"], "Thành phố Trung ương")
            self.assertEqual(at[0]["valid_from"], eff)
            day_before = (date.fromisoformat(eff) - timedelta(days=1)).isoformat()
            prev = self.q(
                "SELECT unit_type FROM admin_unit WHERE code=? AND level='province' AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
                code,
                day_before,
                day_before,
            )
            self.assertEqual(prev[0]["unit_type"], "Tỉnh")

    def test_acceptance_qh16_province_relations(self):
        # type_changed relations carry the same hiệu lực milestone.
        for code, eff in (("75", "2026-04-30"), ("22", "2026-09-01"), ("24", "2026-09-20")):
            rows = self.q(
                """SELECT re.effective_date FROM admin_relation re
                   JOIN admin_unit ou ON re.from_unit_id = ou.id
                   WHERE ou.code=? AND ou.level='province' AND re.relation_type='type_changed'""",
                code,
            )
            self.assertEqual([r["effective_date"] for r in rows], [eff])

    def test_acceptance_ward_00004(self):
        rows = self.q(
            """SELECT ou.valid_to ovt, nu.valid_from nvf, re.relation_type, re.notes
               FROM admin_relation re JOIN admin_unit ou ON re.from_unit_id=ou.id JOIN admin_unit nu ON re.to_unit_id=nu.id
               WHERE ou.code='00004' AND ou.level='ward' AND ou.valid_to='2025-06-30'"""
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0]["nvf"], "2025-07-01")
        self.assertTrue(rows[0]["notes"])
        notes = self.q("SELECT notes FROM admin_unit WHERE code='00004' AND level='ward' AND valid_from='2025-07-01'")[0]["notes"]
        self.assertIn("Trúc Bạch", notes)

    def test_acceptance_alias_lookup(self):
        rows = self.q("SELECT DISTINCT u.id FROM admin_alias a JOIN admin_unit u ON a.unit_id=u.id WHERE a.alias_norm='ba dinh'")
        self.assertTrue(rows)
        qn = self.q(
            "SELECT DISTINCT u.code FROM admin_alias a JOIN admin_unit u ON a.unit_id=u.id WHERE a.alias_norm='quang ninh' AND u.level='province'"
        )
        self.assertEqual([r["code"] for r in qn], ["22"])

    def test_current_wards_point_at_current_province_version(self):
        # regression: after the 2026 TP TW upgrades, current wards in 22/24/75
        # must not keep an expired 'Tỉnh' province version as parent.
        bad = self.q(
            """SELECT COUNT(*) c FROM admin_unit w JOIN admin_unit p ON p.id = w.parent_id
               WHERE w.level='ward' AND w.valid_to IS NULL AND p.level='province' AND p.valid_to IS NOT NULL"""
        )[0]["c"]
        self.assertEqual(bad, 0)
        good = self.q(
            """SELECT COUNT(*) c FROM admin_unit w JOIN admin_unit p ON p.id = w.parent_id
               WHERE w.level='ward' AND w.valid_to IS NULL AND p.level='province'"""
        )[0]["c"]
        self.assertGreater(good, 3000)

    def test_typo_correction_does_not_smear_aliases(self):
        # regression: the two corrected old codes must not carry the other
        # ward's old_name alias ('Xã Mỹ Hưng' on 01615, 'Xã Phú Thịnh' on 02485)
        rows = self.q(
            """SELECT u.code FROM admin_alias a JOIN admin_unit u ON u.id = a.unit_id
               WHERE u.code IN ('01615','02485') AND a.alias_norm IN ('my hung','phu thinh') AND a.alias_type = 'old_name'"""
        )
        self.assertEqual(rows, [])

    def test_idempotent_reimport(self):
        before = {t: self.q(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in ("admin_unit", "admin_alias", "admin_relation")}
        ig.import_gazetteer(RAW_DIR, self.db, None)
        after = {t: self.q(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in ("admin_unit", "admin_alias", "admin_relation")}
        self.assertEqual(before, after)

    def test_rollback_on_model_failure(self):
        good = self.tmp / "rollback.sqlite3"
        shutil.copy(self.db, good)
        expected_units = self.q("SELECT COUNT(*) c FROM admin_unit")[0]["c"]
        expected_rels = self.q("SELECT COUNT(*) c FROM admin_relation")[0]["c"]
        broken = {
            "units": {},
            "aliases": [],
            "relations": [
                {
                    "from": ("ward", "ZZZZZ", "x"),
                    "to": ("ward", "ZZZZY", "x"),
                    "type": "merged_into",
                    "effective": "2025-07-01",
                    "source_key": "mapping",
                    "notes": None,
                }
            ],
        }
        with self.assertRaises(KeyError):
            ig.write_db(good, broken, {"mapping": {"file": "f", "url": "u", "as_of": "2025-07-01", "sha256": "x"}})
        con = sqlite3.connect(str(good))
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("admin_unit", "admin_relation")}
        con.close()
        self.assertEqual(counts["admin_unit"], expected_units)
        self.assertEqual(counts["admin_relation"], expected_rels)


@unittest.skipUnless(RAW_DIR.is_dir(), "raw NSO files not present (dev machine only)")
class TestCliErrors(unittest.TestCase):
    def test_missing_raw_dir_fails(self):
        with tempfile.TemporaryDirectory() as td:
            code = ig.main(["--raw", str(Path(td) / "nope"), "--db", str(Path(td) / "g.sqlite3")])
            self.assertEqual(code, 1)
            self.assertFalse((Path(td) / "g.sqlite3").exists())

    def test_allow_partial_writes_db(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw"
            raw.mkdir()
            shutil.copy(RAW_DIR / "Danh sách cấp tỉnh __16_10_2026.xls", raw)
            code = ig.main(["--raw", str(raw), "--db", str(Path(td) / "g.sqlite3"), "--allow-partial"])
            self.assertEqual(code, 0)
            con = sqlite3.connect(str(Path(td) / "g.sqlite3"))
            n = con.execute("SELECT COUNT(*) FROM admin_unit WHERE valid_to IS NULL").fetchone()[0]
            con.close()
            self.assertEqual(n, 34)


if __name__ == "__main__":
    unittest.main()
