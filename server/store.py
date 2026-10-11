"""SQLite-backed persistent place memory with provenance and change history.

No third-party Python dependencies. All database queries use bound parameters.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import geo

FIELDS = ("name", "category", "address", "province", "phone", "website", "lat", "lon", "notes", "source", "source_id", "status")
VALID_STATUSES = {"unverified", "verified", "inactive", "closed"}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text):
    s = str(text or "").lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


# Seed aliases commonly typed by Vietnamese users (normalized, no diacritics).
# Keep entries short and unambiguous; expansions are space-separated token phrases.
ALIAS_SEED = (
    ("bv", "benh vien"),
    ("ks", "khach san"),
    ("dh", "dai hoc"),
    ("tp", "thanh pho"),
    ("tx", "thi xa"),
    ("tt", "thi tran"),
    ("dv", "dich vu"),
    ("ubnd", "uy ban nhan dan"),
)


def search_alias_map(con):
    """Return {alias: expansion} used to widen FTS queries; empty when the table is absent."""
    try:
        return {row["alias"]: row["expansion"] for row in con.execute("SELECT alias, expansion FROM search_aliases")}
    except sqlite3.OperationalError:
        return {}


_AND = " AND "

_PROVINCE_MERGERS = Path(__file__).resolve().parents[1] / "data" / "province_mergers_2025.json"


def _phrase_tokens(text):
    return tuple(re.findall(r"[\w]+", normalize(text)))


@lru_cache(maxsize=1)
def province_phrase_aliases():
    """Map legacy province phrase (normalized tokens) -> new province tokens.

    One-directional (old -> new): queries naming a pre-merger province also match
    POIs recorded under the 2025 unified name. Built from the merger table plus
    historical aliases in data/province_mergers_2025.json; cached per process.
    """
    aliases = {}
    try:
        data = json.loads(_PROVINCE_MERGERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for new, olds in (data.get("new_to_old") or {}).items():
        new_tokens = _phrase_tokens(new)
        for old in olds:
            old_tokens = _phrase_tokens(old)
            if old_tokens != new_tokens:
                aliases[old_tokens] = new_tokens
    for old, new in (data.get("historical_aliases") or {}).items():
        old_tokens, new_tokens = _phrase_tokens(old), _phrase_tokens(new)
        if old_tokens != new_tokens:
            aliases[old_tokens] = new_tokens
    return aliases


def _expand_token(token, aliases):
    """Prefix expression for one token, OR-ing its token-level expansion when present."""
    expansion = aliases.get(token)
    if expansion:
        return '("' + token + '"* OR "' + expansion + '"*)'
    return '"' + token + '"*'


def _token_segments(tokens, phrase_aliases):
    """Split tokens into (token,) segments, or (old_tokens, new_tokens) when a phrase alias spans them."""
    spans = sorted(phrase_aliases, key=len, reverse=True)  # longest phrase wins
    segments = []
    i = 0
    while i < len(tokens):
        old = next((span for span in spans if tuple(tokens[i : i + len(span)]) == span), None)
        if old:
            segments.append((old, phrase_aliases[old]))
            i += len(old)
        else:
            segments.append((tokens[i],))
            i += 1
    return segments


def build_match_query(tokens, aliases, phrase_aliases=None):
    """FTS5 MATCH expression: tokens are prefix-matched; alias tokens also OR their
    expansion phrase. A legacy province phrase found on token boundaries becomes an
    OR-group keeping BOTH spellings: (("sai" AND "gon") OR ("ho" AND "chi" AND "minh")).
    Token-level expansions still apply inside the old-spelling branch, so a query like
    'tp hcm' keeps matching records that spell out 'thanh pho hcm'."""
    parts = []
    for segment in _token_segments(tokens, phrase_aliases or {}):
        if len(segment) == 2:
            old, new = segment
            old_group = _AND.join(_expand_token(t, aliases) for t in old)
            new_group = _AND.join('"' + t + '"*' for t in new)
            parts.append("((" + old_group + ") OR (" + new_group + "))")
        else:
            parts.append(_expand_token(segment[0], aliases))
    # Explicit AND: FTS5 rejects an implicit conjunction right after a parenthesised group.
    return _AND.join(parts)


def connect(dbpath):
    con = sqlite3.connect(str(dbpath), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def session(dbpath):
    con = connect(dbpath)
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db(dbpath):
    Path(dbpath).parent.mkdir(parents=True, exist_ok=True)
    with session(dbpath) as con:
        con.execute("PRAGMA journal_mode=WAL")
        fts_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='places_fts'").fetchone() is not None
        con.executescript("""
        CREATE TABLE IF NOT EXISTS places (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
          address TEXT NOT NULL DEFAULT '', province TEXT NOT NULL DEFAULT '',
          phone TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
          lat REAL NOT NULL, lon REAL NOT NULL, notes TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT 'user', source_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'unverified', search_text TEXT NOT NULL DEFAULT '',
          name_norm TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          CHECK(lat BETWEEN -90 AND 90), CHECK(lon BETWEEN -180 AND 180)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_identity
           ON places(source, source_id) WHERE source_id <> '';
        CREATE INDEX IF NOT EXISTS idx_location ON places(lat, lon);
        CREATE INDEX IF NOT EXISTS idx_province ON places(province);
        CREATE INDEX IF NOT EXISTS idx_update ON places(updated_at);
        CREATE TABLE IF NOT EXISTS changes (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, place_id TEXT NOT NULL,
          action TEXT NOT NULL, prior_json TEXT, current_json TEXT,
          changed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_change_place ON changes(place_id, seq DESC);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version','1');
        CREATE TABLE IF NOT EXISTS search_aliases (
          alias TEXT PRIMARY KEY, expansion TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_records (
          id TEXT PRIMARY KEY, place_id TEXT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
          confidence REAL NOT NULL DEFAULT 1,
          observed_at TEXT, created_at TEXT NOT NULL,
          CHECK(confidence BETWEEN 0 AND 1)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_place ON memory_records(place_id,created_at);
        CREATE TABLE IF NOT EXISTS place_media (
          id TEXT PRIMARY KEY, place_id TEXT NOT NULL REFERENCES places(id) ON DELETE CASCADE,
          url TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL, rights_basis TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_place ON place_media(place_id);
        """)
        con.executemany("INSERT OR IGNORE INTO search_aliases(alias, expansion) VALUES(?,?)", ALIAS_SEED)
        # Migration: name_norm backs the SQL-side exact-name boost (databases created before it get ALTER + backfill).
        columns = {row["name"] for row in con.execute("PRAGMA table_info(places)")}
        if "name_norm" not in columns:
            con.execute("ALTER TABLE places ADD COLUMN name_norm TEXT NOT NULL DEFAULT ''")
        missing = con.execute("SELECT id, name FROM places WHERE name_norm = ''").fetchall()
        if missing:
            con.executemany("UPDATE places SET name_norm = ? WHERE id = ?", [(normalize(row["name"]), row["id"]) for row in missing])
        con.execute("CREATE INDEX IF NOT EXISTS idx_places_name_norm ON places(name_norm)")
        # Migration: province_code = mã GSO từ point-in-polygon tọa độ (geo-aware search).
        # Backfill qua tools/backfill_province_codes.py — save_place tự gán cho bản ghi mới.
        if "province_code" not in columns:
            con.execute("ALTER TABLE places ADD COLUMN province_code TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_places_province_code ON places(province_code)")
        # Search indexes are optional for SQLite builds without FTS5.
        try:
            con.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(
                search_text, content='places', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
            CREATE TRIGGER IF NOT EXISTS places_fts_ai AFTER INSERT ON places BEGIN
                INSERT INTO places_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
            END;
            CREATE TRIGGER IF NOT EXISTS places_fts_ad AFTER DELETE ON places BEGIN
                INSERT INTO places_fts(places_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
            END;
            CREATE TRIGGER IF NOT EXISTS places_fts_au AFTER UPDATE ON places BEGIN
                INSERT INTO places_fts(places_fts,rowid,search_text) VALUES('delete',old.rowid,old.search_text);
                INSERT INTO places_fts(rowid,search_text) VALUES(new.rowid,new.search_text);
            END;
            """)
            if not fts_exists:
                con.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass


def as_dict(row):
    return dict(row) if row is not None else None


def clean(payload):
    if not isinstance(payload, dict):
        raise ValueError("Bản ghi phải là object JSON")
    p = {}
    for field in FIELDS:
        if field in ("lat", "lon"):
            try:
                n = float(payload[field])
            except (ValueError, TypeError, KeyError):
                raise ValueError(f"Tọa độ {field} không hợp lệ") from None
            if not math.isfinite(n) or (field == "lat" and not -90 <= n <= 90) or (field == "lon" and not -180 <= n <= 180):
                raise ValueError(f"Tọa độ {field} ngoài giới hạn WGS84")
            p[field] = n
        else:
            value = payload.get(field, "")
            value = str(value if value is not None else "").strip()
            if len(value) > (10000 if field == "notes" else 1000):
                raise ValueError(f"{field} vượt quá độ dài cho phép")
            p[field] = value
    if not p["name"]:
        raise ValueError("Tên địa điểm không được trống")
    p["status"] = p["status"] or "unverified"
    if p["status"] not in VALID_STATUSES:
        raise ValueError("Trạng thái không hợp lệ")
    p["source"] = p["source"] or "user"
    p["search_text"] = normalize(" ".join(p.get(k, "") for k in ("name", "category", "address", "province", "phone", "notes")))
    p["name_norm"] = normalize(p["name"])
    province = geo.province_at(p["lat"], p["lon"])
    p["province_code"] = province.code if province is not None else None
    return p


def get_place(con, place_id):
    place = as_dict(con.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone())
    if place is not None:
        place.pop("name_norm", None)  # internal ranking column, not part of the public record
    return place


def save_place(con, payload, place_id=None):
    p = clean(payload)
    if place_id is None:
        place_id = str(payload.get("id") or uuid.uuid4())
    if not isinstance(place_id, str) or len(place_id) > 128 or not re.fullmatch(r"[a-zA-Z0-9_.:/-]+", place_id):
        raise ValueError("ID địa điểm không hợp lệ")
    if p["source_id"]:
        other = con.execute("SELECT id FROM places WHERE source = ? AND source_id = ?", (p["source"], p["source_id"])).fetchone()
        if other:
            place_id = other["id"]
    old = get_place(con, place_id)
    now = utcnow()
    if old:
        columns = [k for k in FIELDS] + ["search_text", "name_norm", "province_code", "updated_at"]
        vals = [p[k] for k in FIELDS] + [p["search_text"], p["name_norm"], p["province_code"], now, place_id]
        con.execute("UPDATE places SET " + ",".join(f"{k}=?" for k in columns) + " WHERE id=?", vals)
        action = "update"
    else:
        columns = ["id", *list(FIELDS), "search_text", "name_norm", "province_code", "created_at", "updated_at"]
        vals = [place_id] + [p[k] for k in FIELDS] + [p["search_text"], p["name_norm"], p["province_code"], now, now]
        con.execute("INSERT INTO places (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", vals)
        action = "create"
    current = get_place(con, place_id)
    if old is None or any(old.get(k) != current.get(k) for k in FIELDS):
        con.execute(
            "INSERT INTO changes(place_id,action,prior_json,current_json,changed_at) VALUES(?,?,?,?,?)",
            (place_id, action, json.dumps(old, ensure_ascii=False) if old else None, json.dumps(current, ensure_ascii=False), now),
        )
    return current, action


def delete_place(con, place_id):
    old = get_place(con, place_id)
    if not old:
        return False
    con.execute("DELETE FROM places WHERE id=?", (place_id,))
    con.execute(
        "INSERT INTO changes(place_id,action,prior_json,current_json,changed_at) VALUES(?,?,?,?,?)",
        (place_id, "delete", json.dumps(old, ensure_ascii=False), None, utcnow()),
    )
    return True


def add_memory(con, place_id, payload):
    if not get_place(con, place_id):
        raise LookupError("Không tìm thấy địa điểm")
    if not isinstance(payload, dict):
        raise ValueError("Bộ nhớ phải là một JSON object")
    kind = str(payload.get("kind") or "note").strip()[:80]
    content = str(payload.get("content") or "").strip()
    source = str(payload.get("source") or "user").strip()[:240]
    try:
        confidence = float(payload.get("confidence", 1))
    except (ValueError, TypeError):
        raise ValueError("Độ tin cậy không hợp lệ") from None
    if not content or len(content) > 10000:
        raise ValueError("Nội dung bộ nhớ phải từ 1-10000 ký tự")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Độ tin cậy phải từ 0 đến 1")
    observed_at = str(payload.get("observed_at") or "").strip()[:80] or None
    mid = str(uuid.uuid4())
    now = utcnow()
    con.execute(
        "INSERT INTO memory_records(id,place_id,kind,content,source,confidence,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (mid, place_id, kind, content, source, confidence, observed_at, now),
    )
    return dict(con.execute("SELECT * FROM memory_records WHERE id=?", (mid,)).fetchone())


def add_media(con, place_id, payload):
    if not get_place(con, place_id):
        raise LookupError("Không tìm thấy địa điểm")
    if not isinstance(payload, dict):
        raise ValueError("Thông tin ảnh phải là JSON object")
    url = str(payload.get("url") or "").strip()
    rights = str(payload.get("rights_basis") or "").strip()[:500]
    source = str(payload.get("source") or "").strip()[:240]
    caption = str(payload.get("caption") or "").strip()[:500]
    if not url.startswith("https://") or len(url) > 3000:
        raise ValueError("URL ảnh bắt buộc HTTPS")
    if not rights or not source:
        raise ValueError("Ảnh cần nguồn và căn cứ quyền sử dụng")
    mid = str(uuid.uuid4())
    now = utcnow()
    con.execute(
        "INSERT INTO place_media(id,place_id,url,caption,source,rights_basis,created_at) VALUES(?,?,?,?,?,?,?)",
        (mid, place_id, url, caption, source, rights, now),
    )
    return dict(con.execute("SELECT * FROM place_media WHERE id=?", (mid,)).fetchone())


def _has_province_code(con):
    return any(row["name"] == "province_code" for row in con.execute("PRAGMA table_info(places)"))


def _like_group(tokens, aliases):
    """AND-group of search_text LIKE predicates for tokens (token-level alias expands to phrase)."""
    parts = []
    args = []
    for token in tokens:
        expansion = aliases.get(token)
        if expansion:
            parts.append("(places.search_text LIKE ? OR places.search_text LIKE ?)")
            args.extend(("%" + token + "%", "%" + expansion + "%"))
        else:
            parts.append("places.search_text LIKE ?")
            args.append("%" + token + "%")
    return "(" + _AND.join(parts) + ")", args


def _geo_scope(con, search, tokens):
    """Detect a province reference in the query → (province, span, kind, span_start) or None.

    Requires the province_code column (init_db migration); pre-migration DBs keep
    pure text behaviour. The span tokens are consumed by the geo filter, not by
    the text match — "phở hồ chí minh" searches "phở" inside the HCM polygon.
    """
    if not _has_province_code(con):
        return None
    detail = geo.resolve_province_detail(search)
    if detail is None:
        return None
    province, span, kind = detail
    n = len(span)
    start = next((i for i in range(len(tokens) - n + 1) if tuple(tokens[i : i + n]) == span), None)
    if start is None:
        return None
    return province, span, kind, start


def _search_clause(con, search, where, args):
    """Extend where/args with the search predicate; return (join fragment, ranked flag, geo scope).

    Geo scope: a province name (new or legacy alias) plus other tokens scopes the
    query to (province_code = resolved GSO code) OR the legacy text behaviour —
    POIs whose text literally contains the province reference (covers POIs outside
    all polygons / not yet backfilled). A query that is ONLY a province name keeps
    the old all-token text match.
    """
    raw_tokens = re.findall(r"[\w]+", str(search or "").lower())
    tokens = [normalize(t) for t in raw_tokens]  # per-token: stays position-aligned with raw_tokens
    scope = _geo_scope(con, search, tokens)
    if scope is not None:
        province, span, kind, start = scope
        text_tokens = (tokens[:start] + tokens[start + len(span) :])[:8]
        raw_text_tokens = (raw_tokens[:start] + raw_tokens[start + len(span) :])[:8]
        if not text_tokens:  # query is only a province name → keep legacy behaviour
            scope = None
    if scope is None:
        text_tokens = tokens[:8]
        raw_text_tokens = raw_tokens[:8]
    fts = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='places_fts'").fetchone()
    if scope is not None:
        aliases = search_alias_map(con)
        province, span, kind, _start = scope
        groups = [span]
        name_tokens = _phrase_tokens(province.name_norm)
        if name_tokens != span:
            groups.append(name_tokens)
        likes = [_like_group(group, aliases) for group in groups]
        textmatch_sql = "(" + " OR ".join(group_sql for group_sql, _group_args in likes) + ")"
        textmatch_args = [arg for _group_sql, group_args in likes for arg in group_args]
        # Accent-faithful name boost: diacritic-typed tokens prefer diacritic names
        # ("phở" over "phố"). Only a ranking signal, never a candidate filter.
        accent = [raw for norm, raw in zip(text_tokens, raw_text_tokens, strict=True) if norm != raw]
        scope = {
            "province": province,
            "kind": kind,
            "textmatch": (textmatch_sql, textmatch_args),
            "accent": ("(" + _AND.join("lower(places.name) LIKE ?" for _ in accent) + ")", ["%" + a + "%" for a in accent]) if accent else None,
        }
    if text_tokens and fts:
        if scope is not None:
            # Union path: list_places builds a two-branch query — the legacy pool
            # (full-expression match AND province text) keeps BM25 over all tokens,
            # while the geo pool (remaining-token match AND province_code) adds the
            # in-polygon rows that lack the province text. WHERE/args stay untouched
            # here; list_places assembles them.
            scope["match_rem"] = build_match_query(text_tokens, search_alias_map(con), province_phrase_aliases())
            scope["match_full"] = build_match_query(tokens[:8], search_alias_map(con), province_phrase_aliases())
            return "", True, scope
        join = " JOIN places_fts ON places.rowid=places_fts.rowid"
        # Candidate set: AND-of-prefixes (+ alias OR-expansions). Exact-name priority is a
        # SQL-side ranking boost (name_norm), NOT a candidate filter — reordered or aliased
        # matches must stay findable.
        where.append("places_fts MATCH ?")
        args.append(build_match_query(text_tokens, search_alias_map(con), province_phrase_aliases()))
        return join, True, scope
    if scope is not None:
        textmatch_sql, textmatch_args = scope["textmatch"]
        where.append("(places.province_code = ? OR " + textmatch_sql + ")")
        args.append(scope["province"].code)
        args.extend(textmatch_args)
    for segment in _token_segments(text_tokens, province_phrase_aliases()):
        if len(segment) == 2:
            old, new = segment
            old_like = "(" + _AND.join("places.search_text LIKE ?" for _ in old) + ")"
            new_like = "(" + _AND.join("places.search_text LIKE ?" for _ in new) + ")"
            where.append("(" + old_like + " OR " + new_like + ")")
            args.extend("%" + t + "%" for t in old)
            args.extend("%" + t + "%" for t in new)
        else:
            where.append("places.search_text LIKE ?")
            args.append("%" + segment[0] + "%")
    return "", False, scope


def _bbox_clause(bbox, where, args):
    """Extend where/args with the viewport bbox predicate (handles antimeridian wrap)."""
    w, s, e, n = bbox
    if w <= e:
        where.append("places.lon BETWEEN ? AND ?")
        args.extend((w, e))
    else:
        where.append("(places.lon >= ? OR places.lon <= ?)")
        args.extend((w, e))
    where.append("places.lat BETWEEN ? AND ?")
    args.extend((s, n))


def _category_clause(category, where, args):
    """Extend where/args with an OR of OSM category-prefix predicates (e.g. 'amenity:cafe|shop:').

    LIKE metacharacters are escaped so a literal '_' (common in OSM categories)
    is not treated as a single-character wildcard.
    """
    prefixes = [p for p in category.split("|") if p][:20]
    if prefixes:
        where.append("(" + " OR ".join("places.category LIKE ? || '%' ESCAPE '\\'" for _ in prefixes) + ")")
        args.extend(p.replace("\\", "\\\\").replace("_", "\\_") for p in prefixes)


# Great-circle distance in km (haversine), unrounded so ORDER BY stays precise — needs
# SQLite math functions (sin/cos/asin/sqrt/radians), available in CPython's bundled
# SQLite. Display rounding to 0.1 happens in Python (_display_distance).
_DISTANCE_SQL = (
    "12742.0 * ASIN(MIN(1.0, SQRT(POWER(SIN(RADIANS(places.lat - ?) / 2.0), 2)"
    " + COS(RADIANS(?)) * COS(RADIANS(places.lat)) * POWER(SIN(RADIANS(places.lon - ?) / 2.0), 2))))"
)


def _display_distance(item):
    """Round the computed distance for output while ORDER BY uses the raw value."""
    if item.get("distance_km") is not None:
        item["distance_km"] = round(item["distance_km"], 1)
    return item


def _province_param(con, value):
    """Resolve a province filter value to a Province: GSO code ("79"/"7") or name/alias."""
    if not _has_province_code(con):
        return None
    text = str(value or "").strip()
    for province in geo.all_provinces():
        if province.code == text or (text.isdigit() and len(text) <= 2 and province.code == text.zfill(2)):
            return province
    return geo.resolve_province(text)


def _geo_union_places(con, scope, where, args, distance_sql, head_args, search_norm, limit, offset):
    """Geo-scoped search via a two-branch UNION (FTS path).

    Branch 1 (grp 0) is the legacy candidate pool — full-expression match plus the
    province text — so BM25 keeps the full-token signal that ranked these rows
    before geo-scoping existed. Branch 2 adds in-polygon rows (province_code)
    matching the remaining tokens; grp tiers order code+accent → code → rest.
    Accent-faithful names lead inside grp 0 as a ranking signal.
    """
    code = scope["province"].code
    textmatch_sql, textmatch_args = scope["textmatch"]
    accent = scope["accent"]
    af_sel = accent[0] if accent else "0"
    af_args = list(accent[1]) if accent else []
    common = (" AND " + _AND.join(where)) if where else ""
    inner1 = (
        "SELECT places.id AS id, 0 AS grp, bm25(places_fts) AS sc, " + af_sel + " AS af, places.updated_at AS upd"
        " FROM places JOIN places_fts ON places_fts.rowid = places.rowid"
        " WHERE places_fts MATCH ? AND " + textmatch_sql + common
    )
    a1 = [*af_args, scope["match_full"], *textmatch_args, *args]
    if accent:
        grp_expr = "CASE WHEN places.province_code = ? AND " + accent[0] + " THEN 1 WHEN places.province_code = ? THEN 2 ELSE 3 END"
        grp_args = [code, *accent[1], code]
    else:
        grp_expr = "CASE WHEN places.province_code = ? THEN 2 ELSE 3 END"
        grp_args = [code]
    inner2 = (
        "SELECT places.id, " + grp_expr + ", bm25(places_fts), " + af_sel + ", places.updated_at"
        " FROM places JOIN places_fts ON places_fts.rowid = places.rowid"
        " WHERE places_fts MATCH ? AND (places.province_code = ? OR " + textmatch_sql + ")" + common
    )
    a2 = [*grp_args, *af_args, scope["match_rem"], code, *textmatch_args, *args]
    derived = (
        "SELECT t.id, MIN(t.grp) g, MIN(CASE WHEN t.grp = 0 THEN t.sc END) s0, MIN(t.sc) sany,"
        " MAX(t.af) af, MAX(t.upd) upd FROM (" + inner1 + " UNION ALL " + inner2 + ") t GROUP BY t.id"
    )
    uargs = a1 + a2
    count = con.execute("SELECT COUNT(*) FROM (" + derived + ")", uargs).fetchone()[0]
    order = (
        " ORDER BY (CASE WHEN places.name_norm = ? THEN 0 ELSE 1 END), j.g, j.af DESC,"
        " COALESCE(j.s0, j.sany), places.updated_at DESC, places.id LIMIT ? OFFSET ?"
    )
    rows = con.execute(
        "SELECT places.*" + distance_sql + " FROM (" + derived + ") j JOIN places ON places.id = j.id" + order,
        [*head_args, *uargs, search_norm, limit, offset],
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item.pop("name_norm", None)
        out.append(_display_distance(item))
    return out, count


def list_places(con, search="", province="", bbox=None, limit=500, offset=0, category="", near=None):
    where = []
    args = []
    join = ""
    ranked = False
    scope = None
    if province:
        resolved = _province_param(con, province)
        if resolved is not None:
            # Geo code wins when assigned; the legacy text label is the fallback for
            # POIs outside every polygon / not yet backfilled.
            where.append("(places.province_code = ? OR (places.province_code IS NULL AND places.province = ?))")
            args.extend((resolved.code, province))
        else:
            where.append("places.province = ?")
            args.append(province)
    if category:
        _category_clause(category, where, args)
    if search:
        join, ranked, scope = _search_clause(con, search, where, args)
    if bbox:
        _bbox_clause(bbox, where, args)
    # distance_km is SELECT-side, so its bound params precede the WHERE args.
    distance_sql = (", " + _DISTANCE_SQL + " AS distance_km") if near else ""
    head_args = [near[0], near[0], near[1]] if near else []
    if ranked and scope is not None and scope.get("match_rem"):
        return _geo_union_places(con, scope, where, args, distance_sql, head_args, normalize(search).strip(), limit, offset)
    base = " FROM places" + join
    predicate = (" WHERE " + _AND.join(where)) if where else ""
    count = con.execute("SELECT COUNT(*)" + base + predicate, args).fetchone()[0]
    if ranked:
        # Relevance order fully in SQL (stable across pages, no Python materialization of
        # deep offsets): exact normalized-name match first, then BM25, then recency.
        order = " ORDER BY (CASE WHEN places.name_norm = ? THEN 0 ELSE 1 END)"
        order_args = [normalize(search).strip()]
        order += ", search_rank, places.updated_at DESC, places.id LIMIT ? OFFSET ?"
        order_args.extend((limit, offset))
        rows = con.execute(
            "SELECT places.*, bm25(places_fts) AS search_rank" + distance_sql + base + predicate + order,
            [*head_args, *args, *order_args],
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.pop("search_rank", None)
            item.pop("name_norm", None)
            out.append(_display_distance(item))
        return out, count
    order = " ORDER BY distance_km, places.id" if near else " ORDER BY places.updated_at DESC, places.id"
    rows = con.execute(
        "SELECT places.*" + distance_sql + base + predicate + order + " LIMIT ? OFFSET ?", [*head_args, *args, limit, offset]
    ).fetchall()
    return [_display_distance(as_dict(r)) for r in rows], count


def stats(con):
    total = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    by_status = {r["status"]: r["n"] for r in con.execute("SELECT status,COUNT(*) n FROM places GROUP BY status")}
    province_count = con.execute("SELECT COUNT(DISTINCT province) FROM places WHERE province != ''").fetchone()[0]
    updates = con.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    return {"total_places": total, "provinces_with_places": province_count, "history_events": updates, "by_status": by_status}


def geometry_feature(p):
    attrs = {k: v for k, v in p.items() if k not in ("lat", "lon", "search_text", "name_norm")}
    return {"type": "Feature", "id": p["id"], "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": attrs}


def feature_to_payload(f):
    if f.get("type") != "Feature" or (f.get("geometry") or {}).get("type") != "Point":
        raise ValueError("Chỉ hỗ trợ GeoJSON Point")
    coords = f["geometry"].get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        raise ValueError("GeoJSON thiếu tọa độ")
    props = dict(f.get("properties") or {})
    props["lon"], props["lat"] = coords[0], coords[1]
    if "id" not in props and f.get("id") is not None:
        props["id"] = str(f["id"])
    return props
