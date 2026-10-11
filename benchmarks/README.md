# Search-quality benchmark

`search_queries.json` (version `round-b-150`) pins 150 labelled Vietnamese queries
against the Atlas SQLite database (Hit@10 / MRR@10 / Recall@10 / p95 latency):
40 exact, 30 no-diacritics, 25 alias, 25 category-in-place, 20 admin-legacy
(pre-merger province names), 10 no-match. The 31 round-A queries are locked at the
top of the file. Expected IDs reference real places of the 2026-10-09 full OSM
snapshot, so the benchmark needs that data to reproduce.

`admin_legacy` entries whose snapshot has no truly-matching POIs carry
`"coverage_gap": true` + `coverage_note` + empty `expected_ids`: they are reported
separately (`coverage_gap_queries` / `coverage_gap_returning_results`) and never
counted in the headline hit-rate. `no_match` queries pass when top-10 is empty;
any returned row counts as a false positive.

## Reproduce

1. Fetch + import the OSM source into a staging DB (full scope, no `--limit`):

   ```bash
   python tools/fetch_vn_geofabrik.py --out data/vietnam-latest.osm.pbf
   python tools/vn_osm_dataset.py data/vietnam-latest.osm.pbf --db data/osm_vietnam.sqlite3 --source-date 2026-10-09
   python tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --min-active 90000 --require-full
   ```

2. Sync the staging data into the Atlas DB (insert-only, verified rows untouched):

   ```bash
   python tools/sync_osm_into_atlas.py --apply
   ```

3. Run the benchmark:

   ```bash
   python tools/search_benchmark.py --atlas-db data/atlas.sqlite3 --json-out benchmarks/after.json
   ```

A different snapshot changes which place IDs exist; keep `search_queries.json`
and the `benchmarks/*.json` evidence in sync with the snapshot you used.
`baseline_before.json` is the pre-BM25 baseline (5k pilot), `after_full_import.json`
is the round-A result on the full 98,617-POI import, and `after_round_b.json` is
the round-B result on the 150-query set (with Recall@10 and coverage-gap reporting).

## Ground-truth audit (2026-10-11, review PR #8)

Devin review found that the expected-ID matcher for `category_place` /
`admin_legacy` queries matched tokens diacritic-insensitively, so `phở` matched
`phố` (Thành phố) — e.g. `phở hồ chí minh` was labelled with 50 unrelated
schools/offices instead of phở restaurants. Fixed by requiring the **first
(accented) query token to match with diacritics** on the place name:

- 14 queries had 155 bogus IDs removed (e.g. `phở nam định` 42→10).
- 3 queries were regenerated from the DB by category token + bounding box
  (`phở hồ chí minh` 50 real phở POIs, `quán ăn hải phòng` 25, `phở việt trì` 10).
- `quán ăn Bắc Kạn` has no matching POIs in the snapshot → coverage gap.

Headline round-B numbers with the audited ground truth: Hit@10 132/136
(97.06%), MRR@10 0.871, Recall@10 0.842, no-match false positives 0/10, p95
14.2 ms. The remaining 4 misses are all `category_place` queries where the
place qualifier (`hồ chí minh`, `hải phòng`, `việt trì`, `vũng tàu`) is not in
the POI name — the AND-token engine cannot combine a category token with a
geo qualifier. Tracked for round C (geo-aware category search).

## Round C — geo-aware province search (2026-10-12, `after_round_c.json`)

Province polygons (round C · T1) are now wired into search. `places` gained a
`province_code` column (GSO code, `province_at(lat, lon)` point-in-polygon);
`tools/backfill_province_codes.py` assigned 96,893 of 98,617 POIs and reports
1,724 POIs outside all polygons (islands/coastal edge) — kept, never deleted.

A query containing a province name — current (`hồ chí minh`) or legacy alias
(`bắc kạn`, `vũng tàu` ⊆ `bà rịa–vũng tàu`; min 2-token subspans only, longest
match wins) — scopes to `(province_code = <code> OR province text match)`. The
scoped ranking is a two-branch UNION: the legacy pool (full-expression match ∧
province text) keeps full-token BM25, then in-polygon rows matching the
remaining tokens (accent-faithful names first, then the rest). This preserves
round-B ordering for text-labelled POIs while adding POIs that only exist
geographically (empty `province`/`address` in the snapshot).

Headline round-C numbers on the same 150 queries / labels:

- Hit@10 **135/136 (99.26%)** (round B: 132/136 = 97.06%) — zero regressions.
- MRR@10 **0.914** (was 0.871), Recall@10 **0.866** (was 0.842).
- Fixed: `phở hồ chí minh`, `quán ăn hải phòng`, `cơm vũng tàu` (all hit@10=1).
- Still missing: `phở việt trì` — Việt Trì is a city in Phú Thọ, not a
  province; province polygons cannot scope to it. Needs ward/city-level data.
- `coverage_gap` queries are still excluded from the headline rate; the new
  secondary metric `geo_hit` (any top-10 POI resolving to the requested new
  province by coordinates) reports **4/4**: `cơm Phú Yên`→Đắk Lắk,
  `quán ăn Bình Thuận`→Lâm Đồng, `khách sạn Đắk Nông`→Lâm Đồng,
  `quán ăn Bắc Kạn`→Thái Nguyên.
- `geo_resolved_queries`: 50 queries carry a province reference.
- no-match false positives: 0/10. p50 8.2 ms, p95 45.2 ms (union runs two FTS
  matches — roughly 2× round-B latency, still well under the 200 ms budget).
