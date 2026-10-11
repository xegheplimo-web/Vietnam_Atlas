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
