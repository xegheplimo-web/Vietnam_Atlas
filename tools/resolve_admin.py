#!/usr/bin/env python3
"""Resolve Vietnamese administrative toponyms inside free-text queries.

Reads the NSO gazetteer (admin_unit / admin_alias / admin_relation) plus the
admin_fts FTS5 index built by tools/build_admin_fts.py — importable as a module
(`from tools.resolve_admin import resolve`) and usable as a CLI:

    py -3 tools/resolve_admin.py --db data/gazetteer.sqlite3 "quán ăn phường Hoàn Kiếm" [--as-of 2025-05-01] [--json]

Algorithm (deterministic; never guesses a random winner):
  1. The query is normalized with name_norm() from tools/import_gazetteer.py
     (same rule the gazetteer was built with) and split into tokens that keep
     their character offsets in the original string.
  2. Tokens are classified: stopwords (quán ăn, cửa hàng, bệnh viện, phở,
     cà phê, nhà, chợ, trường, khách sạn, đường, ...), level words
     (phường/xã/đặc khu/thị trấn -> ward; quận/huyện/thị xã -> district;
     tỉnh/thành phố -> province-ish) and history markers (cũ/cựu/trước đây ->
     hint.historical; mới -> excluded from toponym spans). 'quán' vs 'quận' is
     told apart by the raw diacritics; bare ASCII 'quan' counts as a level word.
  3. The LONGEST token span (<= 5) matching admin_fts wins the toponym. Spans
     made only of stopwords/level words are skipped. If a query carrying
     history markers finds nothing, markers are re-tried as content
     ('thị trấn Chợ Mới' keeps working).
  4. Match kinds: exact (span == unit name_norm) > alias_exact (span ==
     alias_norm) > alias_norm (span inside an alias) > token_subset (span
     inside the unit name). Scoring adds: currentness (+20 valid_to IS NULL,
     or valid at --as-of; flipped to expired units when hint.historical),
     a small administrative-prominence bonus (province 6 / district 3 /
     ward 1) and the level hint (+10/-10). A secondary probe on the toponym
     minus its leading level prefix (e.g. 'phường hoàn kiếm' -> 'hoàn kiếm')
     surfaces same-named units of other levels at a -30 score penalty.
  5. status: 'resolved' (unique top score), 'ambiguous' (>= 2 DIFFERENT units
     share the top score — all candidates returned with their differences,
     never silently picked), 'unresolved' (no match: reason 'no_admin_match',
     or 'toponym_without_data' when a toponym was recognized but the gazetteer
     has no such unit).
  6. chain_current: for an expired candidate the CURRENT successor unit —
     via admin_relation (priority type_changed > renamed_to > merged_into >
     split_into > partially_into), else the unit continuing the same
     (level, code) — e.g. Tỉnh Bắc Giang (code 24, expired 2025-06-30) ->
     Thành phố Bắc Ninh (code 24, current).

CLI exit codes: 0 resolve ran (any status), 1 database/usage error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "gazetteer.sqlite3"


def _load_name_norm():
    """Reuse the gazetteer's own normalization (single source of truth).

    Import order covers: package context (`tools.import_gazetteer`), sibling
    script context (`py -3 tools/resolve_admin.py`), and explicit file loading
    (server/test harness) where neither sys.path entry exists.
    """
    try:
        from tools.import_gazetteer import name_norm as fn

        return fn
    except ImportError:
        pass
    try:
        from import_gazetteer import name_norm as fn

        return fn
    except ImportError:
        spec = importlib.util.spec_from_file_location("import_gazetteer", Path(__file__).with_name("import_gazetteer.py"))
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules["import_gazetteer"] = module
        spec.loader.exec_module(module)  # safe: importer guards its main()
        return module.name_norm


name_norm = _load_name_norm()

_WORD = re.compile(r"[\w]+")
MAX_SPAN = 5
MAX_CANDIDATES = 15

# Venue/filler words — they may sit inside a toponym span (a name can contain
# 'hàng', 'cửa', ...) but can never BE the toponym. 'phở'/'phố' share the norm
# 'pho' and are both non-administrative; 'thành phố' is a level PAIR instead.
STOPWORDS = {
    "quan",
    "an",
    "cua",
    "hang",
    "benh",
    "vien",
    "pho",
    "ca",
    "phe",
    "nha",
    "cho",
    "truong",
    "khach",
    "san",
    "gan",
    "o",
    "tai",
    "duong",
}
LEVEL_PAIRS = {
    ("thanh", "pho"): "district",  # refined to 'province' post-match when a province answers
    ("thi", "xa"): "district",
    ("thi", "tran"): "ward",
    ("dac", "khu"): "ward",
}
LEVEL_SINGLE = {
    "phuong": "ward",
    "xa": "ward",
    "quan": "district",
    "huyen": "district",
    "tinh": "province",
    "tp": "district",
}
HIST_WORDS = {"cũ", "cựu"}  # raw forms; ascii 'cu'/'cuu' stay content ('Củ Chi'...)
NEW_WORDS = {"mới"}
HIST_PAIR = ("trước", "đây")

_MATCH_RANK = {"exact": 0, "alias_exact": 1, "alias_norm": 2, "token_subset": 3}
_BASE_SCORE = {"exact": 100.0, "alias_exact": 85.0, "alias_norm": 70.0, "token_subset": 60.0}
_LEVEL_BONUS = {"province": 6.0, "district": 3.0, "ward": 1.0}
_CURRENT_BONUS = 20.0
_HINT_BONUS = 10.0
_SUBSPAN_PENALTY = 30.0

RELATION_PRIORITY = ("type_changed", "renamed_to", "merged_into", "split_into", "partially_into")


# --------------------------------------------------------------------- result


@dataclass
class Candidate:
    unit_id: int
    code: str
    name: str
    level: str
    unit_type: str
    valid_from: str
    valid_to: str | None
    is_current: bool
    match_kind: str
    score: float
    province_code: str | None
    chain_current: dict | None
    chain_note: str | None


@dataclass
class ResolveResult:
    status: str  # resolved | ambiguous | unresolved
    toponym: str | None
    toponym_span: tuple | None  # (start, end) char offsets in the ORIGINAL query
    hint: dict  # {"level": str|None, "historical": bool}
    candidates: list  # list[Candidate], best first
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "toponym": self.toponym,
            "toponym_span": list(self.toponym_span) if self.toponym_span else None,
            "hint": self.hint,
            "candidates": [asdict(c) for c in self.candidates],
            "reason": self.reason,
        }


# --------------------------------------------------------------------- tokens


def _tokenize(query: str) -> list:
    """Query -> tokens [{raw, norm, start, end, cls}]; offsets into the lowered NFC string."""
    text = unicodedata.normalize("NFC", str(query or "")).lower()
    return [{"raw": m.group(0), "norm": name_norm(m.group(0)), "start": m.start(), "end": m.end(), "cls": "content"} for m in _WORD.finditer(text)]


def _classify(tokens: list) -> None:
    """Mark each token 'level' / 'stop' / 'marker' (else stays 'content'); sets 'lvl'/'mark'."""
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if (a["raw"], b["raw"]) == HIST_PAIR:
            a["cls"] = b["cls"] = "marker"
            a["mark"] = b["mark"] = "hist"
        elif (a["norm"], b["norm"]) in LEVEL_PAIRS:
            lvl = LEVEL_PAIRS[(a["norm"], b["norm"])]
            a["cls"] = b["cls"] = "level"
            a["lvl"] = b["lvl"] = lvl
    for t in tokens:
        if t["cls"] != "content":
            continue
        raw, norm = t["raw"], t["norm"]
        if raw in HIST_WORDS:
            t["cls"], t["mark"] = "marker", "hist"
        elif raw in NEW_WORDS:
            t["cls"], t["mark"] = "marker", "new"
        elif norm in LEVEL_SINGLE and not (norm == "quan" and raw == "quán"):
            t["cls"], t["lvl"] = "level", LEVEL_SINGLE[norm]
        elif norm in STOPWORDS:
            t["cls"] = "stop"


def _runs(tokens: list, include_markers: bool) -> list:
    """Maximal contiguous token runs; history/new markers break runs unless retried as content."""
    runs, cur = [], []
    for t in tokens:
        if t["cls"] == "marker" and not include_markers:
            if cur:
                runs.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        runs.append(cur)
    return runs


def _span_has_content(span: list) -> bool:
    return any(t["cls"] in ("content", "marker") for t in span)


# ------------------------------------------------------------------- matching


def _has_fts(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_fts'").fetchone() is not None


def _match_span_fts(con, norm: str, n_tokens: int) -> list:
    """admin_fts hits for one span -> [(sqlite3.Row, match_kind, contiguous)]."""
    out = []
    for r in con.execute("SELECT rowid, * FROM admin_fts WHERE text_norm = ?", (norm,)):
        out.append((r["rowid"], r, "exact" if r["kind"] == "unit" else "alias_exact", True))
    quoted = '"' + norm + '"'
    for r in con.execute("SELECT rowid, * FROM admin_fts WHERE admin_fts MATCH ?", ("text_norm : " + quoted,)):
        out.append((r["rowid"], r, "token_subset" if r["kind"] == "unit" else "alias_norm", True))
    if n_tokens >= 2:
        conjuncts = " AND ".join('"' + t + '"' for t in norm.split())
        for r in con.execute("SELECT rowid, * FROM admin_fts WHERE admin_fts MATCH ?", ("text_norm : (" + conjuncts + ")",)):
            out.append((r["rowid"], r, "token_subset" if r["kind"] == "unit" else "alias_norm", False))
    return out


def _load_all_rows(con) -> list:
    """Union view equivalent to admin_fts for gazetteer DBs without the index (fallback)."""
    rows = [
        dict(r)
        for r in con.execute(
            "SELECT 'unit' AS kind, name AS text, name_norm AS text_norm, id AS unit_id, code, level, unit_type, valid_from, valid_to FROM admin_unit"
        )
    ]
    rows += [
        dict(r)
        for r in con.execute(
            """SELECT 'alias' AS kind, a.alias AS text, a.alias_norm AS text_norm, u.id AS unit_id,
                      u.code, u.level, u.unit_type, u.valid_from, u.valid_to
                 FROM admin_alias a JOIN admin_unit u ON u.id = a.unit_id"""
        )
    ]
    return rows


def _contiguous_subseq(small: list, big: list) -> bool:
    return any(big[i : i + len(small)] == small for i in range(len(big) - len(small) + 1))


def _match_span_rows(rows: list, norm: str) -> list:
    """Python fallback matcher (no admin_fts): same hits as _match_span_fts."""
    span = norm.split()
    out = []
    for i, r in enumerate(rows):
        rt = r["text_norm"].split()
        if rt == span:
            out.append((i, r, "exact" if r["kind"] == "unit" else "alias_exact", True))
        elif _contiguous_subseq(span, rt):
            out.append((i, r, "token_subset" if r["kind"] == "unit" else "alias_norm", True))
        elif len(span) >= 2 and set(span) <= set(rt):
            out.append((i, r, "token_subset" if r["kind"] == "unit" else "alias_norm", False))
    return out


def _match_span(con, norm: str, n_tokens: int, fallback_rows: list | None) -> list:
    if fallback_rows is not None:
        return _match_span_rows(fallback_rows, norm)
    return _match_span_fts(con, norm, n_tokens)


def _find_toponym(con, tokens: list, fallback_rows: list | None) -> tuple | None:
    """Longest matching span wins; ties -> more content tokens, stronger match, leftmost.

    Two passes: first with history markers splitting runs ('Bắc Giang cũ' ->
    'Bắc Giang'), then — only when nothing matched — with markers demoted to
    plain tokens ('thị trấn Chợ Mới' keeps its name).
    Returns ((span, hits), used_pass2) or None.
    """
    for include_markers in (False, True):
        if include_markers and not any(t["cls"] == "marker" for t in tokens):
            break
        runs = _runs(tokens, include_markers)
        for length in range(MAX_SPAN, 0, -1):
            hits_at_len = []
            for run in runs:
                for start in range(len(run) - length + 1):
                    span = run[start : start + length]
                    if not _span_has_content(span):
                        continue
                    norm = " ".join(t["norm"] for t in span)
                    hits = _match_span(con, norm, length, fallback_rows)
                    if span[0]["cls"] == "stop" or span[-1]["cls"] == "stop":
                        # A stopword on the toponym edge is only legit when the span IS a
                        # real name ('Cửa Lò', 'Hàng Bông'): subset matches like
                        # 'phở việt trì' ⊆ 'thành phố việt trì' are spurious.
                        hits = [h for h in hits if h[2] in ("exact", "alias_exact")]
                    if hits:
                        hits_at_len.append((span, hits))
            if hits_at_len:
                best = max(
                    hits_at_len,
                    key=lambda sh: (
                        sum(1 for t in sh[0] if t["cls"] in ("content", "marker")),
                        -min(_MATCH_RANK[h[2]] for h in sh[1]),
                        -sh[0][0]["start"],
                    ),
                )
                return best, include_markers
    return None


def _strip_level_prefix(span: list) -> list:
    i = 0
    while i < len(span) and span[i]["cls"] == "level":
        i += 1
    return span[i:]


def _fallback_toponym(tokens: list) -> list | None:
    """Longest contiguous run holding >=1 content token, stop-trimmed at the left edge."""
    best = None
    for run in _runs(tokens, True):
        trimmed = run
        while trimmed and trimmed[0]["cls"] == "stop":
            trimmed = trimmed[1:]
        if trimmed and any(t["cls"] in ("content", "marker") for t in trimmed):
            if best is None or len(trimmed) > len(best):
                best = trimmed
    return best


# ------------------------------------------------------------- unit enrichment


def _unit_rows(con, unit_ids: list) -> dict:
    if not unit_ids:
        return {}
    marks = ",".join("?" for _ in unit_ids)
    return {r["id"]: r for r in con.execute(f"SELECT * FROM admin_unit WHERE id IN ({marks})", unit_ids)}


def _province_code(con, units: dict, unit_id: int) -> str | None:
    seen = set()
    cur = units.get(unit_id)
    while cur is not None and cur["id"] not in seen:
        if cur["level"] == "province":
            return cur["code"]
        seen.add(cur["id"])
        parent = cur["parent_id"]
        if parent is None:
            break
        if parent not in units:
            units.update(_unit_rows(con, [parent]))
        cur = units.get(parent)
    return None


def _same_code_lineage(con, level: str, code: str) -> list:
    return list(
        con.execute(
            "SELECT * FROM admin_unit WHERE level = ? AND code = ? ORDER BY valid_from",
            (level, code),
        )
    )


def _follow_relations(con, unit_id: int):
    """BFS to a current unit; edges ordered type_changed > renamed_to > merged_into > ..."""
    seen, queue, steps = {unit_id}, [unit_id], []
    while queue and len(steps) < 8:
        edges = list(
            con.execute(
                "SELECT to_unit_id, relation_type, effective_date FROM admin_relation WHERE from_unit_id = ?",
                (queue.pop(0),),
            )
        )
        edges.sort(key=lambda e: RELATION_PRIORITY.index(e["relation_type"]) if e["relation_type"] in RELATION_PRIORITY else 9)
        nxt = None
        for e in edges:
            if e["to_unit_id"] not in seen:
                nxt = e
                break
        if nxt is None:
            continue
        row = con.execute("SELECT * FROM admin_unit WHERE id = ?", (nxt["to_unit_id"],)).fetchone()
        if row is None:
            continue
        seen.add(row["id"])
        steps.append((nxt["relation_type"], row))
        if row["valid_to"] is None:
            return row, steps
        queue.append(row["id"])
    return None, steps


def _chain(con, unit) -> tuple:
    """(chain_current {code,name,level}|None, chain_note str|None) for one candidate unit."""
    if unit["valid_to"] is None:
        lineage = _same_code_lineage(con, unit["level"], unit["code"])
        if len(lineage) > 1:
            note = " → ".join(f"{r['name']} ({r['valid_from']}→{r['valid_to'] or 'nay'})" for r in lineage)
            return {"code": unit["code"], "name": unit["name"], "level": unit["level"]}, f"mã {unit['code']}: {note}"
        return {"code": unit["code"], "name": unit["name"], "level": unit["level"]}, None
    current, steps = _follow_relations(con, unit["id"])
    if current is not None:
        note = " → ".join([unit["name"], *[f"{t} → {r['name']} (từ {r['valid_from']})" for t, r in steps]])
        return {"code": current["code"], "name": current["name"], "level": current["level"]}, note
    lineage = _same_code_lineage(con, unit["level"], unit["code"])
    current = next((r for r in lineage if r["valid_to"] is None), None)
    if current is not None and current["id"] != unit["id"]:
        note = " → ".join(f"{r['name']} ({r['valid_from']}→{r['valid_to'] or 'nay'})" for r in lineage)
        return {"code": current["code"], "name": current["name"], "level": current["level"]}, f"kế thừa mã {unit['code']}: {note}"
    return None, None


# -------------------------------------------------------------------- resolve


def _level_hint(tokens: list, span: list | None, candidate_levels: set) -> str | None:
    """Level hint: token adjacent-left of the toponym wins; 'thành phố' upgrades when a province answers."""
    hint = None
    if span:
        for t in reversed(tokens):
            if t["end"] <= span[0]["start"]:
                hint = t.get("lvl") if t["cls"] == "level" else None
                break
    if hint is None:
        hint = next((t["lvl"] for t in tokens if t["cls"] == "level"), None)
    if hint == "district" and any(t["norm"] in ("thanh", "tp") for t in tokens if t["cls"] == "level"):
        if "province" in candidate_levels:
            return "province"
    return hint


def _score(unit, kind: str, contiguous: bool, penalty: float, historical: bool, as_of: str | None, hint_level) -> float:
    score = _BASE_SCORE[kind] + (5.0 if contiguous and kind in ("token_subset", "alias_norm") else 0.0)
    if historical:
        score += _CURRENT_BONUS if unit["valid_to"] is not None else 0.0
    elif as_of:
        current = unit["valid_from"] <= as_of and (unit["valid_to"] is None or as_of <= unit["valid_to"])
        score += _CURRENT_BONUS if current else 0.0
    else:
        score += _CURRENT_BONUS if unit["valid_to"] is None else 0.0
    score += _LEVEL_BONUS.get(unit["level"], 0.0)
    if hint_level:
        score += _HINT_BONUS if unit["level"] == hint_level else -_HINT_BONUS
    return score - penalty


def resolve(query: str, *, db_path=None, as_of: str | None = None, level: str | None = None, province_code: str | None = None) -> ResolveResult:
    """Resolve the admin toponym inside `query` against the gazetteer.

    as_of 'YYYY-MM-DD' re-anchors 'current'; level/province_code restrict the
    candidate pool. Raises FileNotFoundError when the gazetteer DB is absent.
    """
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        raise FileNotFoundError(f"gazetteer DB not found: {db}")
    if as_of:
        from datetime import date

        date.fromisoformat(as_of)  # validates format -> ValueError on garbage

    tokens = _tokenize(query)
    _classify(tokens)
    historical = any(t.get("mark") == "hist" for t in tokens)
    if not tokens:
        return ResolveResult("unresolved", None, None, {"level": None, "historical": historical}, [], "no_admin_match")

    con = sqlite3.connect(str(db))  # NOSONAR(S8706)
    con.row_factory = sqlite3.Row
    try:
        fallback_rows = None if _has_fts(con) else _load_all_rows(con)
        found = _find_toponym(con, tokens, fallback_rows)
        raw_hits = []  # (row, kind, contiguous, penalty)
        span = None
        if found is not None:
            (span, hits), _pass2 = found
            raw_hits += [(r, k, c, 0.0) for _rid, r, k, c in hits]
            sub = _strip_level_prefix(span)
            if 0 < len(sub) < len(span):
                sub_norm = " ".join(t["norm"] for t in sub)
                raw_hits += [(r, k, c, _SUBSPAN_PENALTY) for _rid, r, k, c in _match_span(con, sub_norm, len(sub), fallback_rows)]

        if not raw_hits:
            fb = _fallback_toponym(tokens)
            if fb:
                return ResolveResult(
                    "unresolved",
                    str(query or "")[fb[0]["start"] : fb[-1]["end"]],
                    (fb[0]["start"], fb[-1]["end"]),
                    {"level": _level_hint(tokens, fb, set()), "historical": historical},
                    [],
                    "toponym_without_data",
                )
            return ResolveResult("unresolved", None, None, {"level": None, "historical": historical}, [], "no_admin_match")

        # dedupe rows -> best match kind per unit
        best = {}
        for r, kind, contig, pen in raw_hits:
            uid = r["unit_id"]
            cur = best.get(uid)
            if cur is None or (_MATCH_RANK[kind], -contig, pen) < (_MATCH_RANK[cur[1]], -cur[2], cur[3]):
                best[uid] = (r, kind, contig, pen)

        units = _unit_rows(con, list(best))
        cands = []
        for uid, (_row, kind, contig, pen) in best.items():
            u = units.get(uid)
            if u is None:
                continue
            pcode = u["code"] if u["level"] == "province" else _province_code(con, units, uid)
            cands.append({"u": u, "kind": kind, "contig": contig, "pen": pen, "pcode": pcode})

        if level:
            cands = [c for c in cands if c["u"]["level"] == level]
        if province_code:
            cands = [c for c in cands if c["pcode"] == province_code]

        hint_level = _level_hint(tokens, span, {c["u"]["level"] for c in cands})
        hint = {"level": hint_level, "historical": historical}
        toponym = str(query or "")[span[0]["start"] : span[-1]["end"]] if span else None
        span_xy = (span[0]["start"], span[-1]["end"]) if span else None
        if not cands:
            return ResolveResult("unresolved", toponym, span_xy, hint, [], "no_admin_match")

        for c in cands:
            c["score"] = _score(c["u"], c["kind"], c["contig"], c["pen"], historical, as_of, hint_level)
        cands.sort(key=lambda c: (-c["score"], -_LEVEL_BONUS.get(c["u"]["level"], 0), c["u"]["code"]))

        top = cands[0]["score"]
        tied = [c for c in cands if c["score"] == top]
        shown = cands[:MAX_CANDIDATES]
        out = []
        for c in shown:
            u = c["u"]
            current = u["valid_from"] <= as_of <= (u["valid_to"] or "9999") if as_of else u["valid_to"] is None
            chain_cur, chain_note = _chain(con, u)
            out.append(
                Candidate(
                    unit_id=u["id"],
                    code=u["code"],
                    name=u["name"],
                    level=u["level"],
                    unit_type=u["unit_type"],
                    valid_from=u["valid_from"],
                    valid_to=u["valid_to"],
                    is_current=current,
                    match_kind=c["kind"],
                    score=round(c["score"], 2),
                    province_code=c["pcode"],
                    chain_current=chain_cur,
                    chain_note=chain_note,
                )
            )
        if len(tied) > 1:
            return ResolveResult("ambiguous", toponym, span_xy, hint, out, "multiple_top_matches")
        return ResolveResult("resolved", toponym, span_xy, hint, out, None)
    finally:
        con.close()


# ------------------------------------------------------------------------ cli


def _print_human(result: ResolveResult) -> None:
    print(f"status: {result.status}   toponym: {result.toponym!r}   span: {result.toponym_span}")
    print(f"hint: level={result.hint.get('level')} historical={result.hint.get('historical')}")
    if result.reason:
        print(f"reason: {result.reason}")
    for c in result.candidates:
        cur = "current" if c.is_current else (f"expired {c.valid_to}" if c.valid_to else "not current at --as-of")
        print(f"  {c.score:7.1f}  {c.name} [{c.level}/{c.unit_type}] code={c.code} {cur} via={c.match_kind} prov={c.province_code}")
        if c.chain_current:
            print(f"           chain-> {c.chain_current['name']} ({c.chain_current['code']}) {c.chain_note or ''}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Resolve admin toponyms in free text (NSO gazetteer)")
    p.add_argument("query", nargs="+", help="free-text query, e.g. 'quán ăn phường Hoàn Kiếm'")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--as-of", default=None, help="anchor 'current' at YYYY-MM-DD")
    p.add_argument("--level", choices=["province", "district", "ward"], default=None)
    p.add_argument("--province-code", default=None)
    p.add_argument("--json", action="store_true", help="emit the ResolveResult JSON")
    a = p.parse_args(argv)
    query = " ".join(a.query)
    try:
        result = resolve(query, db_path=a.db, as_of=a.as_of, level=a.level, province_code=a.province_code)
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
