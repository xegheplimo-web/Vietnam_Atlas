# E3A CONTRACT — Independent Search Benchmark v1 (Frozen Baseline)

- **Version:** v1.0 (APPROVED WITH CHANGES — 6 sửa đổi của GPT đã áp dụng, 2026-10-16)
- **Base commit:** `f2fb61aab31b2101cc5fe552c689ec4554b0f706` (main, round E) — **KHÔNG phải baseline cuối**: sau V1-03 phải re-pin commit + SHA mới
- **Owner:** Hermes · **Approver:** GPT (review ✓) + human signoff cho GT cuối
- **Freeze chưa hoàn tất** cho tới khi: 6 sửa đổi ↓ đã áp dụng (✓), regression-150 chạy trên snapshot đã pin, `freeze_manifest.json` được ký.

## 1. Pinned artifacts (SHA-256 nội dung file — KHÔNG phải git blob SHA)

| Artifact | SHA-256 | Ghi chú |
|---|---|---|
| `tools/search_benchmark.py` | `100c4a811c27cad3c757192615cad7af83cda1968ce2b44a0a5bbb21c1a00be7` | git blob sha = `8778a530e0b7783b9099dd417804d1e945c12d98` |
| `benchmarks/search_queries.json` | `a5492db74bae751272d15c2f50e81e96ac4cf56dd4778ae98150be0785a74610` | round-b-150 (locked regression) |
| `server/app.py` | `3b803ea25a2c3f443ee6288ef61d81734d7e6c0b655c8db3cfc8dc386729094b` | API contract §4 |
| `server/store.py` | `3456aeadf6728805eaf40fe6f1520aebe756db1aa36569e093ef2b1ac79029f5` | `list_places()` |
| `server/geo.py` | `4959b210f6c25885401858d8ccebbe5f9b824a81274c836c0816e931ae77d95b` | point-in-polygon |
| `data/atlas.sqlite3` | `d23479288d9a4e1d5cf715c79a05bc3cc1649970906b1e1e161c53909be72853` | 98.938 POI (live — freeze bằng online backup §3) |
| `data/gazetteer.sqlite3` | `4fd25c5f920ceb6c6a85793e4c0d4f77eb9a608ad5eff928098ab8e95110c8c3` | 14.174 unit + FTS 30.236 (sau E2c) |
| `data/provinces_34.json` | `bfa682830f9507d09c506a74e696bea146391f33898b37f005729e699e09775d` | CHƯA sync 3 TP — V1-03 bump + **re-pin** |
| `data/gazetteer/DECREES.json` | `75df658b7628de2306421c6b6cb3eb3cfd274f398d985441f2e79e30d796ef72` | cam kết ngày hiệu lực |
| `data/province_mergers_2025.json` | (pin khi freeze) | ảnh hưởng search alias cũ→mới |
| polygon data (`data/provinces/`) + `PROVENANCE.json` | (pin khi freeze) | ảnh hưởng geo scope |
| adapter config (near/bbox/category defaults) | (pin khi freeze) | ảnh hưởng kết quả run |

## 2. Actual paths — drift record (spec cũ → thực tế)

| Spec cũ | Thực tế | Quyết định |
|---|---|---|
| `search_benchmark.py` sha `f90f627a…` | `100c4a81…` | Pin SHA thực |
| `contracts/` (verify existing) | Không tồn tại | Tạo mới `contracts/E3A_CONTRACT.md` |
| benchmarks tạo từ đầu | `benchmarks/` đã có: 150 câu + 6 mốc | Giữ nguyên làm regression evidence |
| `data/admin_outline_34.json`, `admin_units_34.json`, `data/db/vietnam_fulltext.db`, `data/raw/` | Không tồn tại | Dùng path thực |
| server "chưa rõ" | `server/app.py` + `store.py` + `geo.py` + `live.py` | Dùng nguyên |
| 99,26% là baseline | Snapshot 98.617 POI cũ | KHÔNG so; chạy lại regression trên snapshot mới |

## 3. Snapshot & freeze procedure (sửa theo review)

1. **Tạo bản copy nhất quán TRƯỚC mọi thứ**: SQLite online backup API cho Atlas + Gazetteer (đúng WAL; KHÔNG copy nóng). Frozen master **tuyệt đối không ghi** — mọi thao tác trên working copy.
2. Regression 150 (`search_benchmark.py`) chạy **trên bản copy**, kiểm tra ID cũ còn tồn tại → `benchmarks/e3a/reports/regression_150.json`. **Không** so điểm trực tiếp trên corpus mới với điểm lịch sử để kết luận regression code.
3. Pin SHA-256 snapshot + dependency địa lý (mục 1) **trước** mọi thay đổi; mọi report ghi kèm SHA.
4. **G8 — SAFETY (gate, không phải khuyến nghị):** `server.app.main()` gọi `init_db()` khi khởi động → CẤM chạy HTTP server trực tiếp trên frozen master. Server benchmark chỉ chạy trên working copy riêng của snapshot, bind 127.0.0.1, khóa đường ghi ở harness, và **so hash frozen master trước/sau chạy**.
5. E3a KHÔNG sửa ranking / KHÔNG thêm bbox/RTree / KHÔNG semantic AI / KHÔNG query live production.

## 4. API contract (server @ `3b803ea2`)

- `python -m server.app --host 127.0.0.1 --port 8765` (chỉ trên working copy — G8).
- `GET /api/places`: `search`, `province` (GSO/tên/alias), `category` (1–20 prefix OSM `|`), `near=lat,lon`, `bbox`, `admin=1` → narrowing resolver (`admin_province_code`: giữ rows khớp + KHÔNG có geo evidence; chỉ loại rows mâu thuẫn; áp trước count/pagination — `store.list_places` L661–666).
- `GET /api/admin/resolve`: `q`, `as_of` (tùy chọn).
- Các route khác: `/api/provinces`, `/api/stats`, `/api/health`, `/api/live`, `/api/export.geojson|.csv`, `/api/places/<id>`.
- **Adapters (cả 2 bắt buộc):** `legacy` = `list_places()` trực tiếp; `admin` = đường `?admin=1` qua HTTP 127.0.0.1 trên server working-copy (hoặc adapter tái dùng handler).
- **Near ≠ bộ lọc bán kính (sửa theo review):** code hiện tại nhận `near` để TÍNH khoảng cách; không search → sort distance-first; có search → ranking relevance-first; **API CHƯA có tham số radius**. E3a đo đúng behavior hiện có: bán kính là điều kiện của ORACLE (chấm điểm), evaluator **không** thêm filter bán kính để làm engine trông tốt hơn. Engine trả POI ngoài bán kính → ghi `outside_radius` / `radius_filter_unsupported`.

## 5. Benchmark schema v1 (sửa theo review — tách topic khỏi nhãn)

### 5.1 Topic (JSONL — `queries_dev.jsonl`) — KHÔNG chứa nhãn đánh giá
`id`, `group` (G1–G7), `persona`, `split`, `query` (chuỗi tự nhiên; được sai dấu/lỗi gõ/viết tắt), `near` (chỉ G5 — §5.5).
→ `expected_ids[]`, `coverage_gap`, `evidence[]` **thuộc qrels/evidence** (không phải input truy vấn): `qrels_dev.jsonl` = `(query_id, place_id, grade)`; `evidence_manifest.jsonl` = nguồn bằng chứng + trạng thái coverage.

### 5.2 Phân bổ bắt buộc (cố định)
| Nhóm | Tổng | Dev | Blind |
|---|---|---|---|
| G1 — Tên POI cụ thể | 60 | 40 | 20 |
| G2 — Không dấu, lỗi gõ, viết tắt | 50 | 33 | 17 |
| G3 — Loại POI + địa danh | 70 | 47 | 23 |
| G4 — Địa danh cũ/mới | 45 | 30 | 15 |
| G5 — Near/bán kính | 35 | 23 | 12 |
| G6 — Trùng tên, mơ hồ | 20 | 13 | 7 |
| G7 — Không có kết quả phù hợp | 20 | 14 | 6 |
| **Tổng** | **300** | **200** | **100** |

### 5.3 Persona quota
Người dân 90 · BV/trường/cơ quan 70 · Du khách 50 · Giao hàng/cửa hàng 50 · Địa danh/lịch sử 40.

### 5.4 Nguồn (external evidence bắt buộc)
Cổng tỉnh/cơ quan NN · web y tế/giáo dục · web doanh nghiệp/chuỗi · NSO (xác minh địa danh) · cổng du lịch · câu hỏi người kiểm thử. **Cấm:** lấy tên POI từ atlas/OSM PBF/search_queries.json; "đổ tên gazetteer thành câu hỏi". Phủ 34 tỉnh, ≥5 câu/tỉnh; ưu tiên địa bàn khó.

### 5.5 G5 — evidence tọa độ (chuẩn implement)
Bằng chứng địa chỉ chữ KHÔNG đủ chứng minh tọa độ. Ba nguồn được phép (ưu tiên giảm dần): (1) cổng địa lý chính thức công bố tọa độ WGS84 + ngày kiểm tra; (2) GPS người kiểm thử đo tại chỗ (ghi phương pháp/ngày/sai số); (3) geocoder độc lập có điều khoản cho phép lưu tọa độ (ghi đối chiếu + độ tin cậy). **Cấm lấy tọa độ từ Atlas/OSM.** Format `near`: `{lat, lon, radius_km, crs:"EPSG:4326", coordinate_source, evidence_id, accuracy_m}`. Bán kính đủ lớn so với sai số; POI sát ngưỡng có sai số chồng lấn → xác minh thêm, không ép grade. Phân bố đô thị/nông thôn/miền núi/ven biển.

## 6. qrels & runs (TREC-style)

- `queries_holdout.jsonl` + `qrels_holdout.jsonl` trong `private_holdout/` — **tách quyền truy cập THẬT** (không chỉ ngoài git): agent tối ưu KHÔNG được đọc queries/qrels holdout trước nghiệm thu.
- qrels: `(query_id, place_id, grade 0–3)`; **pooled judgments** = gộp top-10 từ CẢ legacy LẪN admin adapter trước khi chấm + candidate relevant từ nguồn độc lập cho câu nhiều đáp án; `judgment_completeness` ghi lại; Recall@10 chỉ công bố đầy đủ khi relevant set kiểm kê đủ.
- Coverage Gap Rate = số positive query không có POI phù hợp trong Atlas / **280** — audit TOÀN DB; CẤM suy từ "top-10 rỗng".
- LLM-assisted nhưng human-grounded; disagreement → `judge_disagreements.csv`.

### 6.1 Rubric 0–3 (chuẩn implement — materialize vào `rubric_v1.md`)
Xét ràng buộc bắt buộc trước (tên thực thể, loại POI, địa bàn, mốc thời gian, tọa độ/bán kính, trạng thái hoạt động). **3 = exact_relevant** (đúng thực thể / đáp ứng mọi ràng buộc + đủ ý định) · **2 = relevant** (đủ ràng buộc bắt buộc, kém lý tưởng ở yếu tố ưu tiên) · **1 = partially_relevant** (vi phạm ≥1 ràng buộc bắt buộc hoặc chưa chứng minh đủ phù hợp) · **0 = irrelevant**. **grade ≥ 2 = relevant.** Special rules: G1 chỉ cùng thực thể mới ≥2 · G3 sai tỉnh/phường bắt buộc tối đa 1 · G4 nối sai quan hệ hành chính tối đa 1 · G5 ngoài bán kính tối đa 1, khoảng cách tính từ WGS84 gốc (không dùng distance_km đã làm tròn) · G6 resolver trả ambiguous khi đúng bản chất mơ hồ = hành vi ĐÚNG · G7 chỉ zero results mới PASS · **thiếu evidence → `unjudged`, KHÔNG ép về 0**.

### 6.2 primary_outcome (đúng 1 giá trị)
`hit` · `retrieval_failure` · `coverage_gap` · `correct_no_match` · `false_positive` · `unjudged` · `invalid_query` · `run_error`.
Thứ tự kiểm tra: validate topic/evidence → run error → coverage audit → qrels/top-10 → outcome (không nhầm retrieval_failure thành coverage_gap).

### 6.3 error_labels (nhiều nhãn, có thể rỗng) — 6 nhóm, 41 nhãn
Ground truth (evidence_missing, evidence_stale, label_conflict, insufficient_judgments, ambiguous_intent) · Data (poi_missing_in_db, gazetteer_gap, historical_alias_missing, incorrect_coordinates, incorrect_category_data, stale_poi, duplicate_entity) · Admin (admin_unresolved, admin_ambiguous, admin_wrong, historical_mapping_wrong, wrong_province, wrong_ward) · Text/ranking (normalization_failure, typo_handling_failure, alias_match_failure, category_match_failure, wrong_entity, rank_below_10, candidate_generation_failure, relevance_ranking_failure, false_positive_no_match) · Spatial (outside_radius, incorrect_distance, incorrect_distance_order, radius_filter_unsupported, spatial_evidence_uncertain) · Runtime (api_error, sqlite_error, query_timeout, non_deterministic_results, latency_budget_exceeded).
`severity`: info|minor|major|critical — critical = khẳng định chắc chắn sai địa danh/vị trí có bằng chứng rõ, hoặc dẫn người dùng tới cơ sở không phù hợp. Nhãn suy được tự động (rank_below_10, query_timeout…) scorer tự gán; nhãn cần phán đoán (wrong_entity, historical_mapping_wrong, evidence_stale…) KHÔNG được scorer tự sáng tác.

### 6.4 Công thức scorer
`Hit@10(q) = 1[∃d ∈ Top10(q): grade(q,d) ≥ 2]` · `MRR@10 = (1/|Q|) Σ 1/rank_q` (không relevant trong top-10 → rr=0) · `DCG@10 = Σ_{i=1..10} (2^grade_i − 1)/log2(i+1)` · `nDCG@10 = DCG/IDCG`; **qrels chưa đủ → báo `judgment_incomplete`, KHÔNG âm thầm coi unjudged = grade 0**.

## 7. Runner `tools/benchmark_e3a.py` (mới — giữ nguyên `search_benchmark.py`)

Modes: `validate` · `run` (snapshot working-copy read-only) · `judge-import` · `score` · `compare` · `report`.
Tree (chốt):
```
contracts/E3A_CONTRACT.md
benchmarks/e3a/
  README.md  rubric_v1.md  schema_v1.json
  queries_dev.jsonl  qrels_dev.jsonl  evidence_manifest.jsonl  freeze_manifest.json
  reports/  regression_150.json  baseline_summary.json|.md
            baseline_per_query.csv  retrieval_runs.jsonl
            error_analysis.csv  judge_disagreements.csv
private_holdout/          # ignored + access restricted
  queries_holdout.jsonl  qrels_holdout.jsonl  evidence_holdout.jsonl  holdout_manifest.json
tools/search_benchmark.py  tools/benchmark_e3a.py
```
`reports/` **trong git** (bằng chứng release); chỉ loại file lớn/hạn chế quyền/kết quả không công bố.

**`freeze_manifest.json` — CHỈ input/config/hashes** (sửa theo review): benchmark_version, rubric_version, ngày · git_sha, atlas_db_sha256, gazetteer_db_sha256, SQLite version · query-set hash + qrels hash · counts theo group/split/province. **Metrics (Hit@10/MRR/nDCG/lỗi) nằm trong `baseline_summary.json`** — không trộn vào manifest.

## 8. Metrics & ngưỡng GO-LIVE (blind holdout, qrels khóa trước)

e2e Hit@10 ≥ 80% · Retrieval Hit@10 ≥ 90% · MRR@10 ≥ 0,75 · nDCG@10 ≥ 0,80 · No-match ≥ 90% (holdout: **6 negative** — ≥90% thực tế = 6/6; báo cáo độ bất định thống kê, không khẳng định 6 câu chứng minh 90% toàn người dùng) · lỗi địa danh NGHIÊM TRỌNG = 0 · p95 ≤ 200 ms · regression 150 không giảm. Coverage Gap Rate: công bố thật + phân tích tỉnh/loại + ưu tiên bù dữ liệu — không ngưỡng cứng.

## 9. CI gate

Validators + 10–20 synthetic fixtures + scorer unit tests + metrics-from-fixtures. KHÔNG cần 300 câu/dữ liệu NSO trong GitHub Actions.

## 10. Governance & re-freeze rules (sửa theo review)

- Holdout: quyền truy cập tách thật; agent tối ưu chỉ thấy 200 dev.
- Human (Sếp) signoff GT cuối; LLM không vượt evidence/người.
- **Re-freeze phân biệt:** (a) Atlas DB / Gazetteer / qrels / query set đổi → **corpus/benchmark version mới + baseline lại**; (b) code ranking/adapter đổi → **run version mới trên corpus đã freeze** (đây là so sánh trước/sau hợp lệ); (c) rubric/scorer semantics đổi → version mới + **rescore retrieval runs đã lưu**.
- Không phải mọi lần sửa code search đều re-freeze query set.

## 11. Thứ tự thực thi (GPT chốt lần cuối)

1. **V1-03 nhỏ** — sync metadata 3 TP (level=province giữ nguyên; unit_type + ngày hiệu lực có nguồn; test API).
2. **Snapshot + preliminary freeze** — online backup Atlas + Gazetteer; pin dependency địa lý/provenance/hash; frozen master không ghi.
3. **Regression 150 trên bản copy** — kiểm ID cũ còn tồn tại; lưu `regression_150.json`.
4. **Finalize freeze + contract v1.0** — ký freeze_manifest; khóa schema/rubric/quota; nếu input đổi → quay lại bước 2.
5. **OpenCode/Cline** — deterministic validator + metrics + taxonomy + adapters + report; unit tests cho phân biệt no-match/coverage gap.
6. **Devin** — công cụ hỗ trợ evidence + qrels + coverage audit (gold entity ↔ Atlas pooled); không tự chốt GT khi thiếu bằng chứng.
7. **Hermes** — thu thập 300 câu theo batch 30, human signoff, split 200/100 + quyền holdout, baseline 2 adapter.
8. **Final verify** — schema + scoring tests + lint + CI + local 300-query run + report + blind holdout theo quy trình độc lập.
