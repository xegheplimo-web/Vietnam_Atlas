"""Tests for V1-03 admin metadata (provinces_34.json) and OSM address composition."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.vn_osm_dataset import compose_address  # noqa: E402 (import sau sys.path.insert là chủ ý)

PROVINCES = ROOT / "data" / "provinces_34.json"

EXPECTED_NAMES = [
    "An Giang",
    "Bắc Ninh",
    "Cà Mau",
    "Cao Bằng",
    "Đắk Lắk",
    "Điện Biên",
    "Đồng Nai",
    "Đồng Tháp",
    "Gia Lai",
    "Hà Tĩnh",
    "Hải Phòng",
    "Hồ Chí Minh",
    "Huế",
    "Hưng Yên",
    "Khánh Hòa",
    "Lai Châu",
    "Lâm Đồng",
    "Lạng Sơn",
    "Lào Cai",
    "Nghệ An",
    "Ninh Bình",
    "Phú Thọ",
    "Quảng Ngãi",
    "Quảng Ninh",
    "Quảng Trị",
    "Sơn La",
    "Tây Ninh",
    "Thái Nguyên",
    "Thanh Hóa",
    "Tuyên Quang",
    "Vĩnh Long",
    "Đà Nẵng",
    "Hà Nội",
    "Cần Thơ",
]

QH16_TYPE_CHANGES = {
    "Đồng Nai": ("2026-04-30", "30/2026/QH16"),
    "Quảng Ninh": ("2026-09-01", "36/2026/QH16"),
    "Bắc Ninh": ("2026-09-20", "39/2026/QH16"),
}

CITY_TYPE = "Thành phố Trung ương"
PROVINCE_TYPE = "Tỉnh"


GAZETTEER = ROOT / "data" / "gazetteer.sqlite3"


class AdminTypeMetadata(unittest.TestCase):
    """Card V1-03: tên/level giữ nguyên; thêm unit_type + mốc hiệu lực có nguồn."""

    def setUp(self):
        self.entries = json.loads(PROVINCES.read_text(encoding="utf-8"))

    def test_dataset_has_34_units_with_expected_names(self):
        self.assertEqual(len(self.entries), 34)
        self.assertEqual([e["name"] for e in self.entries], EXPECTED_NAMES)

    def test_level_stays_province_for_all_units(self):
        for entry in self.entries:
            self.assertEqual(entry["level"], "province", entry["name"])
            self.assertEqual(entry["effective_from"], "2025-07-01", entry["name"])  # ngày gốc bảo toàn

    def test_unit_type_field_is_normalized(self):
        for entry in self.entries:
            self.assertIn(entry["unit_type"], (PROVINCE_TYPE, CITY_TYPE), entry["name"])
            self.assertRegex(entry["type_effective_from"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(entry["type_source"], entry["name"])

    def test_nine_cities_and_25_provinces(self):
        cities = [e["name"] for e in self.entries if e["unit_type"] == CITY_TYPE]
        provinces = [e["name"] for e in self.entries if e["unit_type"] == PROVINCE_TYPE]
        self.assertEqual(len(cities), 9)
        self.assertEqual(len(provinces), 25)

    def test_qh16_type_change_dates_match_decrees(self):
        by_name = {e["name"]: e for e in self.entries}
        for name, (date, doc) in QH16_TYPE_CHANGES.items():
            entry = by_name[name]
            self.assertEqual(entry["unit_type"], CITY_TYPE)
            self.assertEqual(entry["type_effective_from"], date, name)
            self.assertEqual(entry["type_source"], doc, name)

    def test_reform_units_keep_2025_07_01(self):
        for entry in self.entries:
            if entry["name"] in QH16_TYPE_CHANGES:
                continue
            self.assertEqual(entry["type_effective_from"], "2025-07-01", entry["name"])
            self.assertEqual(entry["type_source"], "19/2025/QĐ-TTg", entry["name"])

    @unittest.skipUnless(GAZETTEER.exists(), "gazetteer DB absent (không đóng gói trong repo)")
    def test_sync_tool_check_passes(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "sync_province_types.py"), "--check"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ComposeAddress(unittest.TestCase):
    """Display address = số + tên đường, phường/xã, quận/huyện, thành phố."""

    def test_full_components(self):
        tags = {
            "addr:housenumber": "34",
            "addr:street": "Hàng Giấy",
            "addr:district": "Phường Hoàn Kiếm",
            "addr:city": "Hà Nội",
        }
        self.assertEqual(compose_address(tags), "34 Hàng Giấy, Phường Hoàn Kiếm, Hà Nội")

    def test_subdistrict_then_district_then_province(self):
        tags = {
            "addr:housenumber": "17",
            "addr:street": "Đường Huyện",
            "addr:subdistrict": "Phú Thạnh",
            "addr:district": "Tân Phú",
            "addr:province": "Tiền Giang",
        }
        self.assertEqual(compose_address(tags), "17 Đường Huyện, Phú Thạnh, Tân Phú, Tiền Giang")

    def test_empty_return_for_no_addr_tags(self):
        self.assertEqual(compose_address({}), "")

    def test_overlapping_city_province_keeps_fuller_form(self):
        tags = {
            "addr:housenumber": "99",
            "addr:street": "Trần Phú",
            "addr:city": "Huế",
            "addr:province": "Thành phố Huế",
        }
        self.assertEqual(compose_address(tags), "99 Trần Phú, Thành phố Huế")

    def test_street_only(self):
        self.assertEqual(compose_address({"addr:street": "Hàng Giấy"}), "Hàng Giấy")

    def test_housenumber_only(self):
        self.assertEqual(compose_address({"addr:housenumber": "12"}), "12")

    def test_city_and_province_distinct_keeps_city(self):
        tags = {"addr:street": "Nguyễn Trãi", "addr:city": "Từ Liêm", "addr:province": "Hà Nội"}
        # city≠province theo nghĩa nội dung (chồng nhau đứng cạnh tầng khác)
        self.assertEqual(compose_address(tags), "Nguyễn Trãi, Từ Liêm, Hà Nội")

    def test_adjacent_duplicate_dropped(self):
        tags = {"addr:street": "Hàng Giấy", "addr:ward": "Phường Hoàn Kiếm", "addr:district": "Hoàn Kiếm"}
        self.assertEqual(compose_address(tags), "Hàng Giấy, Phường Hoàn Kiếm")


if __name__ == "__main__":
    unittest.main()
