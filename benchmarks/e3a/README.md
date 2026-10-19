# E3a — Independent search benchmark harness (C2)

`tools/benchmark_e3a.py` đo chất lượng search của Vietnam Atlas theo
`contracts/E3A_CONTRACT.md`. Stdlib only — không dependency runtime, không gọi
mạng ngoài loopback, không LLM trong đường chạy.

Files:

- `schema_v1.json` — JSON Schema (draft 2020-12, subset) cho `query`, `qrels`,
  `run`, `evidence`, `manifest`. `required`/`enum` trong file này là nguồn chân lý;
  validator của harness enforce đúng các luật đó + luật ngữ nghĩa.
- `rubric_v1.md` — thang điểm 0–3, 37 error labels (6 nhóm), ví dụ chấm.
- `reports/` — kết quả chạy (trong git = bằng chứng release).

## Cách ly corpus (B1/G8 — bắt buộc)

`--db` trỏ **master đã khoá**. Harness **không bao giờ ghi** master:

1. Master mở `<path.resolve().as_uri()>?mode=ro` + `PRAGMA query_only=ON`
   (nhánh in-process chạy trực tiếp trên master read-only).
2. Working copy cho HTTP server tạo bằng **SQLite Online Backup API**
   (`sqlite3.Connection.backup()`). **KHÔNG** `VACUUM INTO` — `places_fts` là
   FTS5 external-content trỏ `places.rowid`, VACUUM có thể desync index.
   Copy xong kiểm `integrity_check` + `places`/`places_fts` tồn tại.
3. Server do harness spawn: `python -m server.app --host 127.0.0.1 --port <trống>
   --db <working-copy>`, env `ATLAS_GAZETTEER_DB=<gazetteer working copy>`.
   Chỉ bind `127.0.0.1`.
4. SHA-256 của mọi master (atlas + gazetteer) ghi **trước và sau** run; khác nhau
   ⇒ `master_unchanged=false`, exit 1, report phải bỏ.
5. Harness chỉ phát `GET` — `http_json` từ chối POST/PUT/DELETE.
6. Query encode bằng `urllib.parse.urlencode` (UTF-8). `--debug` đối chiếu top-ids
   HTTP↔inprocess để bắt lỗi codepage (sự cố "Ho"→HCM đã ghi nhận).

## Adapters

| `--adapter` | Đường đo | Vai trò |
|---|---|---|
| `legacy-http` | `GET /api/places?search=…&limit=10` (không `admin=1`) | **primary** — đường UI thật |
| `admin-http` | `GET /api/places?search=…&admin=1&limit=10` | secondary — resolver opt-in |
| `inprocess` | `server.store.list_places()` trên master read-only | **chẩn đoán**, không áp ngưỡng GO-LIVE |
| `all` | cả ba trong cùng một phiên | convenience |

## Latency protocol (B2 — hằng số, không cấu hình)

10 warm-up không tính điểm · **5 repeats/câu/adapter** · concurrency 1 · timeout
2 s/request ⇒ `run_error` (không loại khỏi gate). Report p50/p95 **riêng từng
adapter** + fingerprint môi trường (OS, CPU, Python, SQLite, SHA DB).
`--quick` chạy smoke profile (0 warm-up, 1 repeat) → `protocol.official=false`.

## Workflow

```bash
# 1. Validate inputs (offline)
python tools/benchmark_e3a.py validate --queries benchmarks/e3a/queries_dev.jsonl \
    --qrels benchmarks/e3a/qrels_dev.jsonl --evidence benchmarks/e3a/evidence_manifest.jsonl

# 2. Run (frozen master KHÔNG bao giờ ghi)
python tools/benchmark_e3a.py run --adapter all \
    --queries benchmarks/e3a/queries_dev.jsonl \
    --db data/atlas.sqlite3 --gazetteer-db data/gazetteer.sqlite3 \
    --out benchmarks/e3a/reports/run_dev.json

# 3. Nhập judgments (JSONL do người/LLM tạo — harness không gọi LLM)
python tools/benchmark_e3a.py judge-import --judgments judgments_raw.jsonl \
    --queries benchmarks/e3a/queries_dev.jsonl --out benchmarks/e3a/qrels_dev.jsonl

# 4. Score
python tools/benchmark_e3a.py score --run benchmarks/e3a/reports/run_dev.json \
    --queries benchmarks/e3a/queries_dev.jsonl --qrels benchmarks/e3a/qrels_dev.jsonl \
    --evidence benchmarks/e3a/evidence_manifest.jsonl \
    --out benchmarks/e3a/reports/baseline_summary.json

# 5. Compare (gate C5) + report
python tools/benchmark_e3a.py compare --baseline benchmarks/e3a/reports/baseline_summary.json \
    --candidate benchmarks/e3a/reports/candidate_summary.json --out compare.md
python tools/benchmark_e3a.py report --score benchmarks/e3a/reports/baseline_summary.json
```

## Định nghĩa metric (cố định)

- `hit@10` / `mrr@10` / `recall@10` / `ndcg@10` — trên positive queries đã judge đủ
  (`hit`|`retrieval_failure`); gain exponential `2^grade−1`. `unjudged` **không**
  bị coi là grade 0; query còn item top-10 chưa chấm → `judgment_incomplete`.
- `no_match_false_positive` — tỉ lệ G7 trả ≥1 kết quả (G7 không cần qrels).
- `coverage_gap_rate` = positive audit `missing` / tổng positive (280 ở bộ đủ —
  mọi `unjudged` vẫn nằm trong mẫu số). Audit chưa đủ ⇒ chỉ xuất
  `provisional_coverage_rate` (mẫu số = positive đã audit). `retrieval_failure`
  KHÔNG được suy thành coverage gap.
- Breakdown theo `group` G1…G7 và `panel` `pilot_random_85`/`pilot_hard_15`.

## qrels & judgments

- `judge-import` input JSONL: `{query_id, place_id, grade 0–3, judged_by,
  judgment_status, error_labels[], evidence_ids[]}` hoặc record coverage:
  `{query_id, coverage_audit: "present"|"missing"|"unjudged", …}`.
- Chỉ `judgment_status="verified"` **và** có `evidence_ids` mới effective —
  thiếu evidence ⇒ `unjudged` (rubric §1), scorer không ép về 0.
- `error_labels` ⊆ 37 nhãn trong schema; entry là string hoặc
  `{label, severity}` (severity ∈ info|minor|major|critical).

## Evidence rules (validate FAIL khi)

- Thiếu trường bắt buộc (`evidence_id … review_status` — xem schema).
- `accessed_at` không phải ISO-8601 có timezone.
- `snippet_sha256` ≠ sha256(snippet đã chuẩn hoá: NFC + whitespace gộp).
- Query G5 không có evidence mang `lat/lon/crs/accuracy_m/coordinate_method`,
  hoặc record tọa độ thiếu một trong các trường đó.
- `query_id` không tồn tại trong queries; `content_sha256` ≠ 64-hex khi có giá trị.

## Guardrails compare (C5, exit ≠ 0 khi vi phạm)

`regression150_lost_hits=0` · `exact_name_lost_hits=0` ·
`dev_positive_false_rejection≤0.01` (mẫu số = positive baseline-hit) ·
`critical_errors=0` · `http_p95_ms ≤ 200` mỗi HTTP adapter · corpus_sha256 phải
khớp giữa baseline và candidate.

## Fixtures & tests

`tests/fixtures/e3a/` chứa `queries.json`, `qrels.jsonl`, `evidence.jsonl`,
`places.json` (29 POI tổng hợp) + `build_fixture_db.py` — build
`atlas_fixture.sqlite3`/`gazetteer_fixture.sqlite3` (qua `server.store.init_db`,
đúng schema + FTS5 + `province_code`; **không** chép corpus thật).
`run_sample.json`/`score_sample.json` là run/score mẫu sinh từ fixture.

```bash
python tests/fixtures/e3a/build_fixture_db.py          # build DBs vào fixtures dir
python tools/benchmark_e3a.py run --adapter legacy-http \
    --queries tests/fixtures/e3a/queries.json \
    --db tests/fixtures/e3a/atlas_fixture.sqlite3 \
    --gazetteer-db tests/fixtures/e3a/gazetteer_fixture.sqlite3 --quick
python -m unittest tests.test_benchmark_e3a -v         # offline, DB tạm
```
