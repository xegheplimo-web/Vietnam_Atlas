"""Unit tests cho server/geo.py: PIP, bbox, resolve tên tỉnh, load, area sanity, holes."""

import unittest
from pathlib import Path

from server import geo
from server.store import normalize as store_normalize

ROOT = Path(__file__).resolve().parents[1]


class ProvinceAtTest(unittest.TestCase):
    def test_hanoi_hoan_kiem(self):
        province = geo.province_at(21.0285, 105.8542)
        self.assertIsNotNone(province)
        self.assertEqual(province.name, "Hà Nội")

    def test_hcm_ben_thanh(self):
        province = geo.province_at(10.776, 106.700)
        self.assertIsNotNone(province)
        self.assertEqual(province.name, "Hồ Chí Minh")

    def test_quang_ninh_ha_long(self):
        province = geo.province_at(20.95, 107.08)
        self.assertIsNotNone(province)
        self.assertEqual(province.name, "Quảng Ninh")

    def test_sea_point_returns_none(self):
        self.assertIsNone(geo.province_at(16.0, 110.0))

    def test_far_outside_returns_none(self):
        self.assertIsNone(geo.province_at(0.0, 0.0))

    def test_result_always_inside_its_bbox(self):
        # Quét lưới toàn VN: mọi kết quả PIP phải nằm trong bbox của tỉnh trả về.
        for lat10 in range(80, 245, 5):
            for lon10 in range(1020, 1105, 5):
                lat, lon = lat10 / 10, lon10 / 10
                province = geo.province_at(lat, lon)
                if province is not None:
                    west, south, east, north = province.bbox
                    self.assertTrue(south <= lat <= north and west <= lon <= east, (lat, lon, province.name))


class ProvinceBboxTest(unittest.TestCase):
    def test_bbox_by_name_contains_point(self):
        west, south, east, north = geo.province_bbox("Hà Nội")
        self.assertLessEqual(west, 105.85)
        self.assertLessEqual(21.03, north)
        self.assertGreaterEqual(east, 105.85)
        self.assertGreaterEqual(21.03, south)

    def test_bbox_code_equals_name(self):
        self.assertEqual(geo.province_bbox("01"), geo.province_bbox("Hà Nội"))

    def test_bbox_by_gso_code(self):
        self.assertEqual(geo.province_bbox("79"), geo.province_bbox("Hồ Chí Minh"))
        self.assertEqual(geo.province_bbox("04"), geo.province_bbox("Cao Bằng"))
        province = geo.province_at(10.776, 106.700)
        assert province is not None
        self.assertEqual(province.code, "79")

    def test_bbox_short_code_pads(self):
        self.assertEqual(geo.province_bbox("1"), geo.province_bbox("01"))

    def test_bbox_unknown_returns_none(self):
        self.assertIsNone(geo.province_bbox("xyzabc"))
        self.assertIsNone(geo.province_bbox(""))


class ResolveProvinceTest(unittest.TestCase):
    def test_hanoi_variants(self):
        for text in ("hà nội", "ha noi", "Hà Nội", "HÀ NỘI"):
            with self.subTest(text=text):
                self.assertEqual(geo.resolve_province(text).name, "Hà Nội")

    def test_ha_tay_alias(self):
        self.assertEqual(geo.resolve_province("hà tây").name, "Hà Nội")

    def test_sai_gon_alias(self):
        self.assertEqual(geo.resolve_province("sài gòn").name, "Hồ Chí Minh")

    def test_binh_duong_old_name(self):
        self.assertEqual(geo.resolve_province("bình dương").name, "Hồ Chí Minh")

    def test_thua_thien_hue_alias(self):
        self.assertEqual(geo.resolve_province("thừa thiên huế").name, "Huế")

    def test_nam_dinh_old_name(self):
        self.assertEqual(geo.resolve_province("nam định").name, "Ninh Bình")

    def test_unknown_returns_none(self):
        self.assertIsNone(geo.resolve_province("xyzabc"))
        self.assertIsNone(geo.resolve_province(""))


class SubspanResolveTest(unittest.TestCase):
    """Contained multi-token phrases of old province names resolve; single tokens don't."""

    def test_vung_tau_resolves_hcm(self):
        self.assertEqual(geo.resolve_province("vũng tàu").name, "Hồ Chí Minh")
        self.assertEqual(geo.resolve_province("cơm vũng tàu").name, "Hồ Chí Minh")
        self.assertEqual(geo.resolve_province("vung tau").name, "Hồ Chí Minh")

    def test_single_token_fragments_do_not_resolve(self):
        for text in ("tàu", "phú", "đắk"):
            with self.subTest(text=text):
                self.assertIsNone(geo.resolve_province(text))

    def test_detail_returns_consumed_span(self):
        detail = geo.resolve_province_detail("phở hồ chí minh")
        self.assertEqual(detail[0].name, "Hồ Chí Minh")
        self.assertEqual(detail[1], ("ho", "chi", "minh"))
        detail = geo.resolve_province_detail("quán ăn bắc kạn")
        self.assertEqual(detail[0].name, "Thái Nguyên")
        self.assertEqual(detail[1], ("bac", "kan"))

    def test_longest_span_wins(self):
        detail = geo.resolve_province_detail("nhà hàng thừa thiên huế")
        self.assertEqual(detail[1], ("thua", "thien", "hue"))

    def test_generic_subspans_do_not_resolve(self):
        # "thành phố" là cụm con của alias "Thành phố Hồ Chí Minh" nhưng nghĩa là "city".
        for text in ("bệnh viện thành phố", "khách sạn thành phố", "thành phố", "ubnd thành phố"):
            with self.subTest(text=text):
                self.assertIsNone(geo.resolve_province(text))
        # Cụm có token định danh vẫn resolve bình thường.
        self.assertEqual(geo.resolve_province("bệnh viện thành phố hà nội").name, "Hà Nội")
        self.assertEqual(geo.resolve_province("cơm vũng tàu").name, "Hồ Chí Minh")


class LoadProvincesTest(unittest.TestCase):
    def test_34_provinces(self):
        provinces = geo.load_provinces()
        self.assertEqual(len(provinces), 34)
        self.assertEqual(len({p.code for p in provinces}), 34)

    def test_gso_codes(self):
        # code = mã đơn vị hành chính GSO (không phải matinh 01..34 của dataset GIS).
        by_code = {p.code: p.name for p in geo.load_provinces()}
        self.assertEqual(by_code["01"], "Hà Nội")
        self.assertEqual(by_code["04"], "Cao Bằng")
        self.assertEqual(by_code["79"], "Hồ Chí Minh")
        self.assertEqual(by_code["38"], "Thanh Hóa")
        self.assertEqual(by_code["56"], "Khánh Hòa")

    def test_bboxes_within_vietnam(self):
        for province in geo.all_provinces():
            with self.subTest(province=province.name):
                west, south, east, north = province.bbox
                self.assertTrue(8 <= south and north <= 24, province.name)
                self.assertTrue(102 <= west and east <= 110, province.name)

    def test_missing_file_returns_empty(self):
        missing = ROOT / "data" / "provinces" / "khong_ton_tai.geojson"
        self.assertEqual(geo.load_provinces(path=str(missing)), [])


class AreaSanityTest(unittest.TestCase):
    def test_hanoi_area_range(self):
        hanoi = geo.resolve_province("Hà Nội")
        self.assertGreaterEqual(hanoi.area_km2, 2000)
        self.assertLessEqual(hanoi.area_km2, 5000)

    def test_total_area_range(self):
        total = sum(p.area_km2 for p in geo.all_provinces())
        self.assertGreaterEqual(total, 300_000)
        self.assertLessEqual(total, 380_000)


class HoleTest(unittest.TestCase):
    def test_point_inside_hole_is_outside_polygon(self):
        # Synthetic: outer vuông 4x4, hole vuông 2x2 ở giữa — cover branch hole
        # không phụ thuộc dataset (dataset hiện không có hole).
        outer = geo._make_ring([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)])
        hole = geo._make_ring([(1.0, 1.0), (1.0, 3.0), (3.0, 3.0), (3.0, 1.0)])
        poly = geo.Polygon(outer=outer, holes=(hole,))
        self.assertTrue(geo._polygon_contains(poly, 0.5, 0.5))
        self.assertFalse(geo._polygon_contains(poly, 2.0, 2.0))
        self.assertFalse(geo._polygon_contains(poly, 5.0, 5.0))

    def test_point_inside_hole_returns_none(self):
        found = self._find_hole_point()
        if found is None:
            self.skipTest("dữ liệu polygon GSO không có hole — synthetic test phía trên đã cover branch")
        province, lon, lat = found
        self.assertIsNone(geo.province_at(lat, lon), f"{province.name}: điểm trong hole phải là None")

    @staticmethod
    def _find_hole_point():
        for province in geo.all_provinces():
            for poly in province.polygons:
                for hole in poly.holes:
                    point = HoleTest._point_in_ring(hole)
                    if point is not None:
                        return province, point[0], point[1]
        return None

    @staticmethod
    def _point_in_ring(ring):
        # Thử centroid, trung điểm cạnh, rồi nội suy về phía centroid cho tới khi
        # được 1 điểm chắc chắn nằm trong hole (hole lồi thường vẫn khớp).
        points = ring.points
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        candidates = [(cx, cy)]
        for i in range(len(points)):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % len(points)]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            candidates.append((mx, my))
            for t in (0.25, 0.5, 0.75):
                candidates.append((mx + (cx - mx) * t, my + (cy - my) * t))
        for lon, lat in candidates:
            if geo._ring_contains(ring, lon, lat):
                return lon, lat
        return None


class BoundaryTest(unittest.TestCase):
    def test_point_on_sloped_edge_is_outside(self):
        # Diamond (0,1),(1,0),(2,1),(1,2): (0.5,0.5) nằm chính xác trên cạnh xiên
        # (0,1)-(1,0) — ray casting thuần có thể lật inside qua cạnh khác; on-segment
        # check phải trả False (biên = ngoài, strict).
        ring = geo._make_ring([(0.0, 1.0), (1.0, 0.0), (2.0, 1.0), (1.0, 2.0)])
        self.assertFalse(geo._ring_contains(ring, 0.5, 0.5))
        self.assertFalse(geo._ring_contains(ring, 1.0, 0.0))  # đỉnh dưới
        self.assertFalse(geo._ring_contains(ring, 0.0, 1.0))  # đỉnh trái
        self.assertTrue(geo._ring_contains(ring, 1.0, 1.0))  # tâm
        self.assertTrue(geo._ring_contains(ring, 1.5, 1.0))  # trong, không trên cạnh


class MalformedDataTest(unittest.TestCase):
    def test_malformed_docs_return_empty_list(self):
        # Contract: file JSON hợp lệ nhưng cấu trúc lỗi → [] (không crash server).
        import json as jsonlib
        import tempfile

        docs = [
            {"features": [{"geometry": {"type": "MultiPolygon", "coordinates": []}}]},
            {"features": [{"geometry": {"coordinates": [[]]}}]},
            {"features": None},
            [1, 2, 3],
        ]
        for doc in docs:
            with tempfile.NamedTemporaryFile("w", suffix=".geojson", delete=False, encoding="utf-8") as fh:
                jsonlib.dump(doc, fh)
                path = fh.name
            try:
                self.assertEqual(geo.load_provinces(path), [], f"doc={doc!r}")
            finally:
                Path(path).unlink()


class NormalizeSyncTest(unittest.TestCase):
    def test_normalize_matches_store(self):
        for text in ("Hà Nội", "Đắk Lắk", "Hồ Chí Minh", "X", "", None):
            with self.subTest(text=text):
                self.assertEqual(geo.normalize(text), store_normalize(text))


if __name__ == "__main__":
    unittest.main()
