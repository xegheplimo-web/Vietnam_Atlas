"""Tests for tools/resolve_admin.py + tools/build_admin_fts.py (T-E2b — admin resolver).

Unit classes run on a synthetic mini gazetteer (schema reused from
tools/import_gazetteer.py) built in a temp DB. TestAcceptanceRealGazetteer runs
the 5 lead-verified gate queries against data/gazetteer.sqlite3 and is skipped
in CI (no raw NSO copies / no gazetteer DB there). TestGoldenSearch replays
tests/fixtures/search_golden.json against data/atlas.sqlite3 — the regression
contract that /api/places output is unchanged unless admin=1 is passed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from server.app import make_handler
from server.store import connect, init_db, list_places, save_place, session

REPO_ROOT = Path(__file__).resolve().parents[1]
GAZ_DB = REPO_ROOT / "data" / "gazetteer.sqlite3"
ATLAS_DB = REPO_ROOT / "data" / "atlas.sqlite3"
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "search_golden.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # dataclass field resolution needs the module registered
    spec.loader.exec_module(module)
    return module


ig = _load("import_gazetteer", REPO_ROOT / "tools" / "import_gazetteer.py")
bfts = _load("build_admin_fts", REPO_ROOT / "tools" / "build_admin_fts.py")
ra = _load("resolve_admin", REPO_ROOT / "tools" / "resolve_admin.py")


# --------------------------------------------------------------- synthetic db


def _mini_gazetteer(db_path: Path) -> None:
    """Tiny gazetteer: 2 old->new provinces, 1 district, wards incl. a duplicated name."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    for stmt in ig.SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.execute(
        "INSERT INTO gazetteer_source (publisher, source_url, effective_as_of, retrieved_at, sha256, redistribution_status)"
        " VALUES ('NSO','u','2025-07-01','2026-10-16','x','unverified')"
    )
    sid = con.execute("SELECT id FROM gazetteer_source").fetchone()[0]
    units = [
        ("90", "Tỉnh Alpha", "province", "Tỉnh", "0001-01-01", "2025-06-30", None),
        ("91", "Tỉnh Beta", "province", "Tỉnh", "2025-07-01", None, None),
        ("92", "Tỉnh Delta", "province", "Tỉnh", "2025-07-01", None, None),
        ("901", "Quận Gamma", "district", "Quận", "0001-01-01", "2025-06-30", "90"),
        ("90100", "Phường Gamma", "ward", "Phường", "2025-07-01", None, "91"),
        ("90101", "Xã Song A", "ward", "Xã", "2025-07-01", None, "91"),
        ("92201", "Xã Song A", "ward", "Xã", "2025-07-01", None, "92"),
        ("90200", "Xã Cũ Lão", "ward", "Xã", "0001-01-01", "2025-06-30", "901"),
        ("90201", "Xã Tân Lão", "ward", "Xã", "2025-07-01", None, "91"),
    ]
    uid = {}
    for code, name, level, utype, vf, vt, _pcode in units:
        cur = con.execute(
            "INSERT INTO admin_unit (authority, code, name, name_norm, level, unit_type, parent_id, valid_from, valid_to, source_id)"
            " VALUES ('NSO',?,?,?,?,?,NULL,?,?,?)",
            (code, name, ig.name_norm(name), level, utype, vf, vt, sid),
        )
        uid[(level, code, vf)] = cur.lastrowid
    prov_id = {"90": uid[("province", "90", "0001-01-01")], "91": uid[("province", "91", "2025-07-01")], "92": uid[("province", "92", "2025-07-01")]}
    for code, _name, level, _utype, vf, _vt, parent_code in units:
        if parent_code is None:
            continue
        pid = uid.get(("district", parent_code, "0001-01-01")) or prov_id[parent_code]
        con.execute("UPDATE admin_unit SET parent_id=? WHERE id=?", (pid, uid[(level, code, vf)]))
    for code, name, level, _utype, vf, vt, _p in units:
        short = ig.short_name(name)
        if short:
            con.execute(
                "INSERT INTO admin_alias (unit_id, alias, alias_norm, alias_type, valid_from, valid_to, source_id) VALUES (?,?,?,?,?,?,?)",
                (uid[(level, code, vf)], short, ig.name_norm(short), "short_name", vf, vt, sid),
            )
    rels = [
        (uid[("province", "90", "0001-01-01")], uid[("province", "91", "2025-07-01")], "merged_into"),
        (uid[("ward", "90200", "0001-01-01")], uid[("ward", "90201", "2025-07-01")], "merged_into"),
    ]
    for fr, to, rt in rels:
        con.execute(
            "INSERT INTO admin_relation (from_unit_id, to_unit_id, relation_type, effective_date, source_id) VALUES (?,?,?,?,?)",
            (fr, to, rt, "2025-07-01", sid),
        )
    con.commit()
    con.close()  # release the write handle before build_admin_fts opens its own
    bfts.build_admin_fts(db_path)


class TestTokenizeClassify(unittest.TestCase):
    def toks(self, query):
        t = ra._tokenize(query)
        ra._classify(t)
        return t

    def test_stopwords_and_level_words(self):
        t = self.toks("quán ăn phường Hoàn Kiếm")
        self.assertEqual([x["cls"] for x in t], ["stop", "stop", "level", "content", "content"])
        self.assertEqual(t[2]["lvl"], "ward")

    def test_quan_diacritics_disambiguation(self):
        self.assertEqual(self.toks("quán ăn")[0]["cls"], "stop")
        self.assertEqual(self.toks("quận hoàn kiếm")[0]["cls"], "level")
        self.assertEqual(self.toks("quan hoan kiem")[0]["cls"], "level")  # ascii 'quan' -> level

    def test_history_markers(self):
        t = self.toks("cửa hàng Bắc Giang cũ")
        self.assertEqual(t[-1]["cls"], "marker")
        self.assertEqual(t[-1]["mark"], "hist")
        self.assertTrue(any(x.get("mark") == "hist" for x in self.toks("tỉnh Củ Chi trước đây")))
        t = self.toks("thị trấn Chợ Mới")
        self.assertEqual(t[-1]["cls"], "marker")  # 'mới' excluded in pass 1, retried as content
        self.assertEqual(t[-1]["mark"], "new")

    def test_offsets_track_original(self):
        t = self.toks("phở Việt Trì")
        self.assertEqual((t[1]["start"], t[2]["end"]), (4, 12))


class TestSyntheticResolve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls._tmp.name) / "gaz.sqlite3"
        _mini_gazetteer(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def res(self, q, **kw):
        return ra.resolve(q, db_path=self.db, **kw)

    def test_level_hinted_exact_beats_same_name_district(self):
        r = self.res("quán ăn phường Gamma")
        self.assertEqual(r.status, "resolved")
        self.assertEqual(r.hint["level"], "ward")
        self.assertEqual(r.toponym, "phường Gamma")
        self.assertEqual(r.candidates[0].name, "Phường Gamma")
        self.assertEqual(r.candidates[0].code, "90100")
        self.assertEqual(r.candidates[0].match_kind, "exact")
        self.assertTrue(r.candidates[0].is_current)
        self.assertEqual(r.candidates[1].name, "Quận Gamma")  # historical, never top
        self.assertFalse(r.candidates[1].is_current)

    def test_ambiguous_same_name_wards_never_autopicked(self):
        r = self.res("xã Song A")
        self.assertEqual(r.status, "ambiguous")
        self.assertEqual(r.reason, "multiple_top_matches")
        self.assertEqual(len(r.candidates), 2)
        self.assertEqual({c.province_code for c in r.candidates}, {"91", "92"})

    def test_historical_prefers_expired_and_chains_current(self):
        r = self.res("nhà hàng tỉnh Alpha cũ")
        self.assertTrue(r.hint["historical"])
        self.assertEqual(r.status, "resolved")
        top = r.candidates[0]
        self.assertEqual(top.name, "Tỉnh Alpha")
        self.assertEqual(top.valid_to, "2025-06-30")
        self.assertEqual(top.chain_current, {"code": "91", "name": "Tỉnh Beta", "level": "province"})
        self.assertIn("merged_into", top.chain_note)

    def test_relation_chain_for_expired_ward(self):
        r = self.res("xã Cũ Lão cũ")
        top = r.candidates[0]
        self.assertEqual(top.name, "Xã Cũ Lão")
        self.assertEqual(top.chain_current["name"], "Xã Tân Lão")

    def test_unresolved_variants(self):
        r = self.res("bệnh viện Zztopia")
        self.assertEqual(r.status, "unresolved")
        self.assertEqual(r.reason, "toponym_without_data")
        self.assertEqual(r.toponym, "Zztopia")
        r = self.res("quán ăn")
        self.assertEqual(r.status, "unresolved")
        self.assertEqual(r.reason, "no_admin_match")
        self.assertIsNone(r.toponym)

    def test_as_of_reanchors_current(self):
        r = self.res("xã Cũ Lão", as_of="2025-01-01")
        self.assertEqual(r.candidates[0].name, "Xã Cũ Lão")
        self.assertTrue(r.candidates[0].is_current)

    def test_level_and_province_filters(self):
        r = self.res("gamma", level="district")
        self.assertEqual([c.name for c in r.candidates], ["Quận Gamma"])
        r = self.res("xã Song A", province_code="92")
        self.assertEqual(r.status, "resolved")
        self.assertEqual(r.candidates[0].province_code, "92")

    def test_missing_db_raises(self):
        with self.assertRaises(FileNotFoundError):
            ra.resolve("x", db_path=self.db.parent / "nope.sqlite3")

    def test_fallback_matcher_without_fts(self):
        db2 = self.db.parent / "nofts.sqlite3"
        shutil.copy(self.db, db2)
        con = sqlite3.connect(str(db2))
        con.execute("DROP TABLE admin_fts")
        con.commit()
        con.close()
        r = ra.resolve("quán ăn phường gamma", db_path=db2)
        self.assertEqual(r.status, "resolved")
        self.assertEqual(r.candidates[0].name, "Phường Gamma")


class TestBuilder(unittest.TestCase):
    def test_idempotent_and_self_healing(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "g.sqlite3"
            _mini_gazetteer(db)
            self.assertEqual(bfts.build_admin_fts(db)["status"], "noop")
            con = sqlite3.connect(str(db))
            con.execute("DELETE FROM admin_fts WHERE kind='alias'")
            con.commit()
            con.close()
            out = bfts.build_admin_fts(db)
            self.assertEqual(out["status"], "rebuilt")
            self.assertEqual(out["counts"]["alias"], 9)


# ------------------------------------------------- acceptance on the real gazetteer


@unittest.skipIf(os.environ.get("CI") or not GAZ_DB.exists(), "real gazetteer DB absent or CI")
class TestAcceptanceGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bfts.build_admin_fts(GAZ_DB)  # no-op when already built
        cls.db = GAZ_DB

    def res(self, q, **kw):
        return ra.resolve(q, db_path=self.db, **kw)

    def top(self, r):
        return r.candidates[0]

    def test_gate1_pho_viet_tri(self):
        r = self.res("phở Việt Trì")
        self.assertEqual(r.toponym, "Việt Trì")  # food token must not enter the toponym
        self.assertEqual(r.status, "resolved")
        top = self.top(r)
        self.assertEqual((top.name, top.code, top.level), ("Phường Việt Trì", "07900", "ward"))
        self.assertTrue(top.is_current)
        self.assertIsNone(top.valid_to)
        old = next(c for c in r.candidates if c.code == "227")
        self.assertEqual((old.name, old.level, old.valid_to), ("Thành phố Việt Trì", "district", "2025-06-30"))

    def test_gate2_thi_tran_neo_unresolved_no_guessing(self):
        r = self.res("quán ăn thị trấn Neo")
        self.assertEqual(r.status, "unresolved")
        self.assertEqual(r.reason, "toponym_without_data")
        self.assertEqual(r.toponym, "thị trấn Neo")
        self.assertEqual(r.candidates, [])

    def test_gate3_bac_giang_cu(self):
        r = self.res("cửa hàng Bắc Giang cũ")
        self.assertTrue(r.hint["historical"])
        self.assertNotIn("cũ", r.toponym)  # marker excluded from toponym
        self.assertEqual(r.status, "resolved")
        top = self.top(r)
        self.assertEqual((top.name, top.code, top.level), ("Tỉnh Bắc Giang", "24", "province"))
        self.assertEqual(top.valid_to, "2025-06-30")
        self.assertEqual(top.chain_current, {"code": "24", "name": "Thành phố Bắc Ninh", "level": "province"})
        codes = {c.code for c in r.candidates}
        self.assertIn("07210", codes)  # Phường Bắc Giang (current) as a subordinate candidate

    def test_gate4_trang_bom_chain(self):
        r = self.res("bệnh viện Trảng Bom")
        self.assertEqual(r.toponym, "Trảng Bom")
        top = self.top(r)
        self.assertEqual((top.name, top.code, top.level), ("Phường Trảng Bom", "26248", "ward"))
        self.assertEqual(top.valid_from, "2026-04-30")
        self.assertTrue(top.is_current)
        self.assertIn("Thị trấn Trảng Bom", top.chain_note)
        self.assertIn("Xã Trảng Bom", top.chain_note)
        self.assertIn("Phường Trảng Bom", top.chain_note)
        huyen = next(c for c in r.candidates if c.code == "737")
        self.assertEqual((huyen.name, huyen.level, huyen.valid_to), ("Huyện Trảng Bom", "district", "2025-06-30"))

    def test_gate5_phuong_hoan_kiem(self):
        r = self.res("quán ăn phường Hoàn Kiếm")
        self.assertEqual(r.hint["level"], "ward")
        self.assertEqual(r.status, "resolved")
        top = self.top(r)
        self.assertEqual((top.name, top.code, top.valid_to), ("Phường Hoàn Kiếm", "00070", None))
        self.assertEqual(top.match_kind, "exact")
        quan = next((c for c in r.candidates if c.code == "002"), None)
        if quan is not None:  # the old Quận may appear as history but never as top
            self.assertGreater(top.score, quan.score)
            self.assertFalse(quan.is_current)


# --------------------------------------------------------- regression + endpoints


@unittest.skipUnless(ATLAS_DB.exists() and GOLDEN.exists(), "atlas DB or golden fixture absent")
class TestGoldenSearch(unittest.TestCase):
    """list_places() default output must match the pre-resolver golden snapshot."""

    def test_golden_top5_unchanged(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["queries"]
        con = connect(ATLAS_DB)
        try:
            for q, exp in golden.items():
                items, total = list_places(con, q)
                self.assertEqual([x["id"] for x in items[:5]], exp["top5_ids"], f"query {q!r} regressed")
                self.assertEqual(total, exp["total"], f"query {q!r} total regressed")
        finally:
            con.close()


class TestAdminEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.atlas = tmp / "atlas.sqlite3"
        cls.gaz = tmp / "gaz.sqlite3"
        init_db(cls.atlas)
        _mini_gazetteer(cls.gaz)
        with session(cls.atlas) as c:
            # save_place keeps search_text/name_norm consistent for FTS; province_code is
            # overridden afterwards so the admin narrowing test controls the evidence.
            p0, _ = save_place(c, {"name": "Quán Ăn Alpha", "lat": 21.0, "lon": 105.0, "source": "test", "source_id": "adm-a0"})
            p1, _ = save_place(c, {"name": "Cà Phê Alpha", "lat": 21.0, "lon": 105.0, "source": "test", "source_id": "adm-a1"})
            c.execute("UPDATE places SET province_code='90' WHERE id=?", (p0["id"],))
            c.execute("UPDATE places SET province_code='91' WHERE id=?", (p1["id"],))
            cls.ids = (p0["id"], p1["id"])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.atlas, gazetteer_db=cls.gaz))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls._tmp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def test_resolve_endpoint(self):
        data = self.get("/api/admin/resolve?q=nh%C3%A0%20h%C3%A0ng%20t%E1%BB%89nh%20Alpha%20c%C5%A9")
        self.assertEqual(data["status"], "resolved")
        self.assertTrue(data["hint"]["historical"])
        self.assertEqual(data["candidates"][0]["name"], "Tỉnh Alpha")
        self.assertEqual(data["candidates"][0]["chain_current"]["name"], "Tỉnh Beta")

    def test_resolve_endpoint_bad_as_of(self):
        with self.assertRaises(HTTPError) as cm:
            urlopen(self.base + "/api/admin/resolve?q=x&as_of=01-05-2025", timeout=10)
        self.assertEqual(cm.exception.code, 400)

    def test_places_admin_flag_narrows_with_evidence(self):
        data = self.get("/api/places?search=alpha&admin=1")
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["id"], self.ids[0])
        self.assertEqual(item["admin"]["confidence"], "resolved")
        self.assertEqual(item["admin"]["code"], "90")
        self.assertTrue(item["admin"]["filtered"])

    def test_places_without_admin_flag_is_byte_identical(self):
        base = self.get("/api/places?search=alpha")
        admin_off = self.get("/api/places?search=alpha&admin=0")
        self.assertEqual(base, admin_off)
        for item in base["items"]:
            self.assertNotIn("admin", item)


if __name__ == "__main__":
    unittest.main()
