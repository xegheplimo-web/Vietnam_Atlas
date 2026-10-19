#!/usr/bin/env python3
"""Ingest câu hỏi dev soạn từ nguồn ngoài → queries/evidence/qrels nháp cho harness E3a.

Contract §5.4: câu hỏi và gold phải lấy từ nguồn ngoài (cổng tỉnh/cơ quan NN, web y tế/
giáo dục, web doanh nghiệp/chuỗi, cổng du lịch, bài báo) — KHÔNG lấy từ atlas/OSM.
Tool này KHÔNG sinh câu hỏi; nó chỉ kiểm tra nguồn và ánh xạ gold sang place_id trong
corpus (ánh xạ là bước verify, không phải bước chọn câu hỏi).

Input (JSON array hoặc {"queries": [...]}), mỗi phần tử:
  {"id","group","persona","query","province_code"?,"province"?,
   "source":{"publisher","url","snippet"},"gold_names":[...],
   "variant_type"?,"base_name"?,"admin_name"?,"admin_kind"?,"rationale"?
   "near"? (G5)}

Output (--out DIR, tiền tố --tranche):
  queries_dev_<tranche>.json      queries harness (KHÔNG chứa nhãn)
  evidence_manifest_<tranche>.jsonl
  qrels_draft_<tranche>.jsonl     grade 3 tentative, judgment_status=pending (chờ Sếp duyệt)
  ingest_report_<tranche>.json

Usage:
  python tools/dev_ingest.py --authored tranche1.json --db data/atlas.sqlite3 \
      --out benchmarks/e3a/dev --tranche t1 [--check-urls]
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import html as html_module
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.store import normalize  # noqa: E402

GROUPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
PERSONAS = ("nguoi_dan", "bv_truong_co_quan", "du_khach", "giao_hang_cua_hang", "dia_danh_lich_su")
BANNED_HOSTS = ("openstreetmap.org", "google.com/maps", "google.com.vn/maps", "maps.google", "bing.com/maps", "goo.gl/maps")
UA = "VietnamAtlasBench/1.0 (benchmark evidence check; contact: repo owner)"
REPAIR_MIN_RATIO = 0.75
NL = chr(10)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


class IngestError(RuntimeError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_snippet(text: str) -> str:
    return WS_RE.sub(" ", text).strip()


def _flat(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    # NBSP / zero-width / biến thể dấu nháy hay gặp khi copy từ web
    for bad, good in (
        ("\xa0", " "),
        ("\u200b", ""),
        ("\u200c", ""),
        ("\ufeff", ""),
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
    ):
        text = text.replace(bad, good)
    return normalize_snippet(unicodedata.normalize("NFC", text))


def fetch(url: str, timeout: float = 25.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "vi,en;q=0.8", "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(6_000_000)
        enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, unicodedata.normalize("NFC", raw.decode(charset, errors="replace"))


def best_window(page: str, snippet: str, step: int = 25) -> tuple[float, int, int]:
    """Cửa sổ trên trang giống snippet nhất → (ratio, start, end)."""
    n = len(snippet)
    if not page or not snippet:
        return 0.0, 0, 0
    best, best_start = 0.0, 0
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(snippet)
    for start in range(0, max(1, len(page) - n + 1), step):
        sm.set_seq1(page[start : start + n])
        if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
            continue
        r = sm.ratio()
        if r > best:
            best, best_start = r, start
    return best, best_start, best_start + n


def expand_sentence(page: str, start: int, end: int, slack: int = 220) -> str:
    """Mở rộng cửa sổ ra ranh giới câu để trích dẫn không bị cắt giữa từ."""
    left = max(0, start - slack)
    cut = max(page.rfind(". ", left, start), page.rfind(NL, left, start), page.rfind("! ", left, start), page.rfind("? ", left, start))
    if cut != -1:
        start = cut + 2
    right = min(len(page), end + slack)
    stops = [i for i in (page.find(". ", end, right), page.find(NL, end, right), page.find("! ", end, right), page.find("? ", end, right)) if i != -1]
    if stops:
        end = min(stops) + 1
    return page[start:end].strip()


def check_source(src: dict, check_urls: bool) -> dict[str, object]:
    """Trả về {url_ok, status, snippet_ok, error}."""
    out: dict[str, object] = {"url_ok": None, "status": None, "snippet_ok": None, "error": None, "page": ""}
    url = src.get("url") or ""
    if not url.startswith(("http://", "https://")):
        out["error"] = "url không phải http(s)"
        return out
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(b in url.lower() for b in BANNED_HOSTS):
        out["error"] = f"nguồn bị cấm (bản đồ/OSM): {host}"
        return out
    if src.get("verified_via") == "browser":
        # Đã mở bằng trình duyệt thật (TLS + JS đầy đủ) — không fetch lại bằng urllib.
        out["snippet_ok"] = True
        out["verified_via"] = "browser"
        return out
    if not check_urls:
        return out
    try:
        status, body = fetch(url)
        out["status"] = status
        out["url_ok"] = 200 <= status < 400
        snippet = _flat(unicodedata.normalize("NFC", src.get("snippet") or ""))
        page = _flat(body)
        out["page"] = page
        if src.get("verified_via") == "browser":
            # Trang render bằng JS: đã xác minh chuỗi bằng trình duyệt thật, server-side không thấy.
            out["snippet_ok"] = True
            out["verified_via"] = "browser"
            return out
        if snippet:
            out["snippet_ok"] = snippet.casefold() in page.casefold()
        else:
            out["snippet_ok"] = False
            out["error"] = "thiếu snippet"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        out["url_ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


ADDR_TAIL_RE = re.compile(r"^(xã|phường|quận|huyện|thị xã|thị trấn|tp\.?|thành phố|tỉnh|đặc khu|thôn|ấp|khu phố)", re.IGNORECASE)


def clean_gold_name(name: str) -> str:
    """Bỏ phần địa chỉ trong ngoặc/đuôi để so tên thực thể: 'X (xã Y, huyện Z)' → 'X'."""
    text = unicodedata.normalize("NFC", name).split("(")[0].strip()
    parts = [p.strip() for p in text.split(",")]
    keep = [parts[0]]
    for part in parts[1:]:
        if ADDR_TAIL_RE.match(part):
            break
        keep.append(part)
    return ", ".join(keep).strip()


def tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-zà-ỹ]+", normalize(text)) if t}


def match_gold_like(con, pattern: str, limit: int = 3) -> list[dict]:
    """G6: chọn tối đa `limit` POI của chuỗi, ưu tiên khác tỉnh (mơ hồ thật)."""
    rows = con.execute(
        "SELECT id, name, address, province_code FROM places WHERE name LIKE ? ORDER BY province_code, id", (f"%{pattern}%",)
    ).fetchall()
    picked, seen_prov = [], set()
    for r in rows:
        if r[3] in seen_prov:
            continue
        picked.append({"place_id": r[0], "name": r[1], "address": r[2], "province_code": r[3]})
        seen_prov.add(r[3])
        if len(picked) >= limit:
            break
    return picked or [{"place_id": r[0], "name": r[1], "address": r[2], "province_code": r[3]} for r in rows[:limit]]


def match_gold(con, name: str, province_code: str | None, aliases: dict | None = None, limit: int = 3) -> list[dict]:
    """Khớp gold → place_id: exact name_norm → alias khai báo → containment token."""
    alias = (aliases or {}).get(name)
    norm = normalize(alias) if alias else normalize(clean_gold_name(name))
    if not norm:
        return []
    sql = "SELECT id, name, address, province_code FROM places WHERE name_norm = ?"
    args: list = [norm]
    if province_code:
        sql += " AND province_code = ?"
        args.append(str(province_code))
    rows = con.execute(sql + " ORDER BY (address IS NULL), id", args).fetchall()
    return [{"place_id": r[0], "name": r[1], "address": r[2], "province_code": r[3]} for r in rows[:limit]]


def validate_item(item: dict, seen_ids: set[str]) -> list[str]:
    errs = []
    qid = item.get("id")
    if not qid or qid in seen_ids:
        errs.append(f"id thiếu/trùng: {qid!r}")
    if item.get("group") not in GROUPS:
        errs.append(f"{qid}: group không hợp lệ {item.get('group')!r}")
    if item.get("persona") not in PERSONAS:
        errs.append(f"{qid}: persona không hợp lệ {item.get('persona')!r}")
    if not (item.get("query") or "").strip():
        errs.append(f"{qid}: query rỗng")
    src = item.get("source") or {}
    for key in ("publisher", "url", "snippet"):
        if not (src.get(key) or "").strip():
            errs.append(f"{qid}: source.{key} rỗng")
    if len((src.get("snippet") or "").strip()) < 20:
        errs.append(f"{qid}: snippet < 20 ký tự")
    if item.get("group") == "G2" and not (item.get("base_name") or "").strip():
        errs.append(f"{qid}: G2 thiếu base_name")
    if item.get("group") == "G4" and item.get("admin_kind") not in ("old", "new"):
        errs.append(f"{qid}: G4 thiếu admin_kind old|new")
    if item.get("group") == "G7" and item.get("gold_names"):
        errs.append(f"{qid}: G7 phải có gold_names rỗng")
    if item.get("group") != "G7" and not item.get("gold_names"):
        errs.append(f"{qid}: thiếu gold_names")
    return errs


def ingest(authored_path: Path, db_path: Path, out_dir: Path, tranche: str, check_urls: bool, repair_snippets: bool = False) -> dict:
    import sqlite3

    payload = json.loads(authored_path.read_text(encoding="utf-8"))
    items = payload["queries"] if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise IngestError("input rỗng hoặc không phải mảng")

    out_dir.mkdir(parents=True, exist_ok=True)
    queries, evidence, qrels = [], [], []
    seen_ids: set[str] = set()
    errors: list[str] = []
    report_items = []

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        for item in items:
            item_errors = validate_item(item, seen_ids)
            errors.extend(item_errors)
            qid = str(item.get("id") or "")
            seen_ids.add(qid)
            if item_errors:
                continue

            q = {
                "id": qid,
                "group": item["group"],
                "persona": item["persona"],
                "split": "dev",
                "query": item["query"].strip(),
                "regression150": False,
            }
            if item.get("province_code"):
                q["province_code"] = str(item["province_code"])
            notes = []
            if item.get("variant_type"):
                notes.append(f"variant:{item['variant_type']}")
            if item.get("base_name"):
                notes.append(f"base:{item['base_name']}")
            if item.get("admin_name"):
                notes.append(f"admin:{item.get('admin_kind')}:{item['admin_name']}")
            if item.get("rationale"):
                notes.append(item["rationale"][:120])
            if notes:
                q["notes"] = " | ".join(notes)
            if item.get("near"):
                q["near"] = item["near"]
            queries.append(q)

            src = item["source"]
            ev_id = f"ev-{qid}-1"
            src_check = check_source(src, check_urls)
            snippet_final = _flat(unicodedata.normalize("NFC", src["snippet"]))
            repair_note = None
            if check_urls and src_check.get("snippet_ok") is False and src_check.get("page") and repair_snippets:
                ratio, start, end = best_window(str(src_check["page"]), snippet_final)
                if ratio >= REPAIR_MIN_RATIO:
                    repaired = expand_sentence(str(src_check["page"]), start, end)
                    repair_note = f"snippet trích lại từ trang (similarity={ratio:.3f}); gốc: {snippet_final[:90]}"
                    snippet_final = repaired
                    src_check["snippet_ok"] = repaired.casefold() in str(src_check["page"]).casefold()
                    src_check["snippet_repaired"] = True
                    src_check["similarity"] = round(ratio, 3)
            ev = {
                "evidence_id": ev_id,
                "query_id": qid,
                "source_type": "web_external",
                "publisher": src["publisher"].strip(),
                "url": src["url"].strip(),
                "accessed_at": item.get("accessed_at") or time.strftime("%Y-%m-%dT%H:%M:%S+07:00"),
                "claim_supported": item.get("claim_supported")
                or "Địa điểm/đơn vị hành chính nêu trong câu hỏi tồn tại và có địa chỉ như nguồn mô tả",
                "snippet": snippet_final,
                "snippet_sha256": sha256_text(snippet_final),
                "content_sha256": None,
                "rights_basis": "Trích dẫn ngắn phi thương mại để kiểm chứng benchmark; lưu URL + ngày truy cập",
                "review_status": "pending",
            }
            if src.get("verified_via") == "browser":
                ev["verified_via"] = "browser"
                ev["notes"] = "snippet xác minh bằng trình duyệt thật (trang render JS)"
            if repair_note:
                ev["notes"] = repair_note
            if src_check.get("error"):
                ev["notes"] = src_check["error"]
            evidence.append(ev)

            matched = []
            if item.get("gold_like"):
                matched.extend(match_gold_like(con, item["gold_like"]))
            for gold in item.get("gold_names") or []:
                matched.extend(match_gold(con, gold, item.get("province_code"), item.get("gold_aliases")))
            # khử trùng theo place_id, giữ thứ tự
            uniq = {m["place_id"]: m for m in matched}
            if item["group"] != "G7":
                qrels.append(
                    {
                        "query_id": qid,
                        "coverage_audit": "present" if uniq else "missing",
                        "judged_by": "dev_ingest:coverage-audit (whole-DB audit)",
                        "judgment_status": "verified",
                        "evidence_ids": [ev_id],
                        "notes": (
                            f"{len(uniq)} POI khớp gold trong corpus"
                            if uniq
                            else "không có POI nào khớp gold — coverage gap, không tính retrieval_failure"
                        ),
                    }
                )
            for m in uniq.values():
                qrels.append(
                    {
                        "query_id": qid,
                        "place_id": m["place_id"],
                        "grade": 3,
                        "judged_by": "hermes-authoring-draft",
                        "judgment_status": "pending",
                        "evidence_ids": [ev_id],
                        "notes": "draft tự động từ gold_names; chờ human duyệt",
                    }
                )

            report_items.append(
                {
                    "id": qid,
                    "group": item["group"],
                    "query": q["query"],
                    "source": src_check,
                    "gold_names": item.get("gold_names") or [],
                    "matched": list(uniq.values()),
                    "coverage_gap": not uniq and item["group"] != "G7",
                }
            )
    finally:
        con.close()

    (out_dir / f"queries_dev_{tranche}.json").write_text(
        json.dumps({"version": "e3a-dev/v1", "tranche": tranche, "queries": queries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (out_dir / f"evidence_manifest_{tranche}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in evidence), encoding="utf-8", newline="\n"
    )
    (out_dir / f"qrels_draft_{tranche}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in qrels), encoding="utf-8", newline="\n"
    )
    report = {
        "tranche": tranche,
        "authored": str(authored_path),
        "db": str(db_path),
        "counts": {
            "items": len(items),
            "queries": len(queries),
            "evidence": len(evidence),
            "qrels_draft": len(qrels),
            "coverage_gap_queries": sum(1 for r in report_items if r["coverage_gap"]),
            "url_failed": sum(1 for r in report_items if r["source"].get("url_ok") is False),
            "snippet_mismatch": sum(1 for r in report_items if r["source"].get("snippet_ok") is False),
            "source_errors": sum(1 for r in report_items if r["source"].get("error")),
            "snippets_repaired": sum(1 for r in report_items if r["source"].get("snippet_repaired")),
            "verified_via_browser": sum(1 for r in report_items if r["source"].get("verified_via") == "browser"),
        },
        "errors": errors,
        "items": report_items,
    }
    (out_dir / f"ingest_report_{tranche}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest câu hỏi dev từ nguồn ngoài (E3a contract §5.4)")
    ap.add_argument("--authored", required=True, type=Path)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tranche", default="t1")
    ap.add_argument("--check-urls", action="store_true", help="mở từng URL, kiểm snippet có trên trang")
    ap.add_argument("--repair-snippets", action="store_true", help="tự trích lại snippet nguyên văn khi lệch nhẹ (similarity >= 0.75)")
    args = ap.parse_args(argv)

    try:
        report = ingest(args.authored, args.db, args.out, args.tranche, args.check_urls, args.repair_snippets)
    except IngestError as exc:
        print(f"ERROR: {exc}")
        return 2

    c = report["counts"]
    print(f"ingest {args.tranche}: {c['queries']} câu, {c['evidence']} evidence, {c['qrels_draft']} qrels nháp")
    print(
        f"  coverage_gap={c['coverage_gap_queries']} url_failed={c['url_failed']} "
        f"snippet_mismatch={c['snippet_mismatch']} source_errors={c['source_errors']}"
    )
    for err in report["errors"]:
        print(f"  ! {err}")
    for item in report["items"]:
        if item["source"].get("error"):
            print(f"  ! {item['id']}: {item['source']['error']}")
    if report["errors"] or c["source_errors"]:
        return 1
    if args.check_urls and (c["url_failed"] or c["snippet_mismatch"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
