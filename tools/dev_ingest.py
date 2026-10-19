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
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.store import normalize  # noqa: E402

GROUPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")
PERSONAS = ("nguoi_dan", "bv_truong_co_quan", "du_khach", "giao_hang_cua_hang", "dia_danh_lich_su")
BANNED_HOSTS = ("openstreetmap.org", "google.com/maps", "google.com.vn/maps", "maps.google", "bing.com/maps", "goo.gl/maps")
UA = "VietnamAtlasBench/1.0 (benchmark evidence check; contact: repo owner)"
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
    return normalize_snippet(text)


def fetch(url: str, timeout: float = 25.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(4_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, raw.decode(charset, errors="replace")


def check_source(src: dict, check_urls: bool) -> dict[str, object]:
    """Trả về {url_ok, status, snippet_ok, error}."""
    out: dict[str, object] = {"url_ok": None, "status": None, "snippet_ok": None, "error": None}
    url = src.get("url") or ""
    if not url.startswith(("http://", "https://")):
        out["error"] = "url không phải http(s)"
        return out
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(b in url.lower() for b in BANNED_HOSTS):
        out["error"] = f"nguồn bị cấm (bản đồ/OSM): {host}"
        return out
    if not check_urls:
        return out
    try:
        status, body = fetch(url)
        out["status"] = status
        out["url_ok"] = 200 <= status < 400
        snippet = normalize_snippet(src.get("snippet") or "")
        if snippet:
            out["snippet_ok"] = snippet.casefold() in _flat(body).casefold()
        else:
            out["snippet_ok"] = False
            out["error"] = "thiếu snippet"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        out["url_ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def match_gold(con, name: str, province_code: str | None, limit: int = 3) -> list[dict]:
    norm = normalize(name)
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


def ingest(authored_path: Path, db_path: Path, out_dir: Path, tranche: str, check_urls: bool) -> dict:
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
            ev = {
                "evidence_id": ev_id,
                "query_id": qid,
                "source_type": "web_external",
                "publisher": src["publisher"].strip(),
                "url": src["url"].strip(),
                "accessed_at": item.get("accessed_at") or time.strftime("%Y-%m-%dT%H:%M:%S+07:00"),
                "claim_supported": item.get("claim_supported")
                or "Địa điểm/đơn vị hành chính nêu trong câu hỏi tồn tại và có địa chỉ như nguồn mô tả",
                "snippet": normalize_snippet(src["snippet"]),
                "snippet_sha256": sha256_text(normalize_snippet(src["snippet"])),
                "content_sha256": None,
                "rights_basis": "Trích dẫn ngắn phi thương mại để kiểm chứng benchmark; lưu URL + ngày truy cập",
                "review_status": "pending",
            }
            src_check = check_source(src, check_urls)
            if src_check.get("error"):
                ev["notes"] = src_check["error"]
            evidence.append(ev)

            matched = []
            for gold in item.get("gold_names") or []:
                matched.extend(match_gold(con, gold, item.get("province_code")))
            # khử trùng theo place_id, giữ thứ tự
            uniq = {m["place_id"]: m for m in matched}
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
    args = ap.parse_args(argv)

    try:
        report = ingest(args.authored, args.db, args.out, args.tranche, args.check_urls)
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
