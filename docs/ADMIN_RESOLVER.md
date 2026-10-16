# Admin Resolver (T-E2b)

Resolve free-text Vietnamese POI queries to authoritative administrative units from the
NSO gazetteer (`data/gazetteer.sqlite3`). Accent-insensitive via FTS5
(`unicode61 remove_diacritics 2`), alias-aware, and timeline-aware (current vs expired
units, `admin_relation` chains, same-code lineage).

## Build the index

```bash
py -3 tools/build_admin_fts.py --db data/gazetteer.sqlite3 [--rebuild]
```

- Creates `admin_fts` (units + aliases), one row per `admin_unit` (`kind='unit'`) and
  one per `admin_alias` (`kind='alias'`).
- Idempotent: a **content fingerprint** (sha256 over the source rows, stored in
  `admin_fts_meta`) plus per-kind row counts decide the no-op — a same-count reimport
  that renamed a unit still rebuilds, and an empty alias table is a stable (not
  always-rebuild) state. An incomplete index self-heals. `--rebuild` forces a full
  reload.
- **Must be rerun after `tools/import_gazetteer.py`** — the importer drops and
  recreates the database file, which drops `admin_fts`.

## CLI

```bash
py -3 tools/resolve_admin.py --db data/gazetteer.sqlite3 "quán ăn phường Hoàn Kiếm" [--as-of 2025-05-01] [--json]
```

`resolve(query, *, db_path=None, as_of=None, level=None, province_code=None)` returns a
`ResolveResult`: `status` (`resolved` | `ambiguous` | `unresolved`), `toponym` +
`toponym_span` (exact char offsets into the ORIGINAL query — NFD-decomposed input
resolves with intact spans), `hint` (level + historical flag from
`cũ`/`cựu`/`trước đây`), `candidates` (code, name, level, validity range,
`is_current`, `match_kind`, score, `province_code`, `chain_current` (primary
successor), `chain_currents` (ALL current successors — split lineages list every
branch), `chain_note`), and `reason` on failure.

Ranking: `exact` > `alias_exact` > `alias_norm` > `token_subset`; current units beat
expired ones unless `historical` or `--as-of` says otherwise. Two or more equally
plausible top candidates → `ambiguous` (never auto-picked). Recognized toponym with no
gazetteer data → `unresolved` / `toponym_without_data`.

## HTTP

- `GET /api/admin/resolve?q=...&as_of=YYYY-MM-DD` → JSON `ResolveResult` (503 when the
  gazetteer DB is unavailable).
- `GET /api/places?search=...&admin=1` → opt-in annotation. Each item gains
  `"admin": {"code", "name", "level", "confidence", "filtered"}`. Narrowing is applied
  as a WHERE clause **before count/pagination** (totals and pages stay consistent):
  when the resolver is `resolved` and a single CURRENT-generation province code is
  evidenced — the top candidate's own province (current unit) or the single province
  shared by ALL its successors (historical unit, e.g. Bắc Giang-24 → Bắc Ninh-24) —
  places with a *contradictory* recorded `province_code` are dropped; places without
  province evidence are kept and marked (`filtered: false`). Ambiguous histories
  (successors split across provinces) and unresolved toponyms never narrow. Without
  `admin=1` the response is byte-identical to before.

## Gate queries (verified against `data/gazetteer.sqlite3`)

| Query | Expected |
|-------|----------|
| `phở Việt Trì` | toponym `Việt Trì`; top = **Phường Việt Trì** (07900, ward, current); history lists expired **Thành phố Việt Trì** (227) |
| `quán ăn thị trấn Neo` | `unresolved`, `reason=toponym_without_data` — "Neo" is absent from the source data (no guessing) |
| `cửa hàng Bắc Giang cũ` | `historical=true`; top = expired **Tỉnh Bắc Giang** (24); `chain_current` = **Thành phố Bắc Ninh** (24) via same-code lineage |
| `bệnh viện Trảng Bom` | top = **Phường Trảng Bom** (26248, current); chain Thị trấn → Xã → Phường with dates; expired **Huyện Trảng Bom** (737) as history |
| `quán ăn phường Hoàn Kiếm` | `hint.level=ward`; top = **Phường Hoàn Kiếm** (00070); **Quận Hoàn Kiếm** (002) only as expired history |

## Known limitations

- **No ward-level polygons.** `/api/places?admin=1` narrows by recorded
  `province_code` evidence only; it cannot spatially filter to a commune/ward.
- **Unlocated POIs are marked, not guessed.** Places without administrative evidence
  are retained with `filtered: false` rather than assigned to a unit.
- **"Neo" has no source data** — neither `admin_unit` nor `admin_alias` contains it;
  resolution awaits an expanded toponym source.
- **FTS must be rebuilt after re-import** (`build_admin_fts.py` is cheap and
  idempotent).
- Tokens that are both level words and name parts (e.g. `phường` inside a name) resolve
  via exact/alias matches when the full name is present.
