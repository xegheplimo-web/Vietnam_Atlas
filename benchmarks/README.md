# Search-quality benchmark

`search_queries.json` pins 31 labelled Vietnamese queries against the Atlas SQLite
database (Hit@10 / MRR@10 / p95 latency). Expected IDs reference real places of the
2026-10-09 full OSM snapshot, so the benchmark needs that data to reproduce.

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
is the round-A result on the full 98,617-POI import.
