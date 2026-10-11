# Đề xuất tính năng hoàn chỉnh — Vietnam_Atlas 2026

> Tổng hợp: thảo luận với GPT (conversation "Ảnh vệ tinh Việt Nam") + kiểm chứng chéo trực tiếp với code repo.
> Ngày: 2026-10-11 · Trạng thái repo: `main` @ `45b7e34`, PR #1–#5 đã merge, 26/26 tests.

## 1. Hiện trạng (đã xác minh thực tế)

| Hạng mục | Trạng thái |
|---|---|
| Bản đồ + UI (Leaflet 1.9.4) | ✅ Live — 3 lớp nền + lớp vệ tinh opt-in |
| Lớp vệ tinh MapTiler | ✅ E2E verified với key thật (zoom 6/17/20 → provider 5/16/19, tile 512px, 0 lỗi; PR #5 sửa endpoint 404) |
| Đồng bộ POI realtime (SSE) | ✅ PR #4 — revision-only, 32 stream/proc, client refresh viewport |
| Pipeline dữ liệu OSM | ✅ PR #3 — fetch (provenance SHA-256) → staging → sync an toàn → verify → export ODbL |
| Dữ liệu POI | ⚠️ Pilot 5.000 POI (ODbL); **toàn quốc chưa nạp full** |
| Server | Python stdlib + SQLite WAL + FTS5 (`remove_diacritics 2`) |
| Tìm kiếm | ⚠️ FTS5 prefix-token, **sort `updated_at DESC`** — chưa BM25/ranking |
| Polygon hành chính 34 tỉnh | ❌ Chưa có (sync ghi `province=""`) |
| Auth / kiểm duyệt / HTTPS | ❌ Chưa có |
| CI/CD | ✅ CI matrix 6 job + Docker + CodeQL + SonarCloud xanh |

## 2. Đề xuất 3 giai đoạn

### Giai đoạn A — Làm ngay (không cần tài khoản, AI online, hạ tầng phân tán)

| # | Tính năng | Hướng kỹ thuật |
|---|---|---|
| A1 | Nạp POI toàn quốc | Full PBF → staging → QA → backup → sync; đối chiếu số lượng + chống trùng trước/sau |
| A2 | Tìm kiếm tiếng Việt thông minh | Giữ FTS5 + `search_text` không dấu; thêm BM25, ưu tiên tên khớp chính xác, alias, địa danh cũ/mới |
| A3 | Tìm kiếm toàn quốc + "gần tôi" | Tách search toàn quốc khỏi lọc viewport; định vị trình duyệt opt-in |
| A4 | Bộ lọc địa điểm | Taxonomy OSM: ăn uống, y tế, giáo dục, dịch vụ, bán lẻ + trạng thái |
| A5 | Hồ sơ địa điểm hữu ích | Tên, địa chỉ, tọa độ, ĐT, website, nguồn, ngày cập nhật, mức xác minh |
| A6 | Chỉ đường + chia sẻ | Link tọa độ, copy địa chỉ, mở app bản đồ ngoài để dẫn đường |
| A7 | Tối ưu viewport | `EXPLAIN QUERY PLAN`, benchmark bbox/FTS5, kiểm marker trên mobile |
| A8 | Chất lượng dữ liệu hiển thị | Thống kê POI theo tỉnh/loại, bản ghi thiếu tên, trùng ID, tuổi dữ liệu |
| A9 | Kiểm thử hồi quy thật | E2E Chrome/Android, offline/reconnect SSE, key lỗi, zoom, đa tab |

### Giai đoạn B — Bắt buộc trước Public Beta

| # | Tính năng | Hướng kỹ thuật |
|---|---|---|
| B1 | Polygon 34 tỉnh/thành | GeoJSON giản lược; nguồn có ngày hiệu lực + quyền sử dụng (NQ 202/2025/QH15) |
| B2 | Địa danh hành chính cũ/mới | Bảng mã hành chính, alias, quan hệ lịch sử; không tự suy tỉnh từ tâm |
| B3 | Đóng góp địa điểm | Change proposal riêng — **không sửa trực tiếp bản ghi verified** |
| B4 | Kiểm duyệt + chống spam | Hàng đợi duyệt, diff, vai trò reviewer/admin, nhật ký, chống trùng, revision |
| B5 | Đăng nhập + phân quyền | Session cookie an toàn, CSRF, quyền đọc/đề xuất/duyệt/quản trị |
| B6 | PWA + mobile | Web manifest + service worker nhẹ; responsive |
| B7 | HTTPS + chống lạm dụng | Reverse proxy HTTP/2 (giải luôn giới hạn ~6 kết nối HTTP/1.x/tab), rate limit, CSP |
| B8 | Cập nhật OSM tự động | Snapshot → incremental `.osc.gz` diff → staging → QA → atomic publish + rollback |
| B9 | API đọc công khai | Versioning, cursor pagination, bbox, quota, docs, ghi công ODbL |
| B10 | Giám sát + phục hồi | Health/ready, latency histogram, lỗi SSE, backup + test restore |
| B11 | Khả năng tiếp cận | Bàn phím, screen reader, tương phản, vùng chạm, trạng thái loading/error |

### Giai đoạn C — Sau Public Beta (theo nhu cầu/số liệu)

| # | Tính năng | Hướng kỹ thuật |
|---|---|---|
| C1 | Clustering server-side | Grid clusters theo zoom (rẻ hơn MVT trước) |
| C2 | Spatial index quy mô lớn | Đánh giá SQLite R*Tree; chỉ PostGIS khi benchmark chứng minh |
| C3 | Vector tiles MVT | Tile riêng + cache CDN; giữ Leaflet + plugin |
| C4 | AI dedupe địa điểm | Rule + spatial + text similarity → hàng đợi duyệt, không tự gộp |
| C5 | Kho doanh nghiệp | Pháp nhân riêng (MST), nguồn được cấp quyền, liên kết nhiều địa điểm |
| C6 | Semantic search | Chỉ khi FTS5/alias không đạt; embedding batch + rerank |
| C7 | AI+GPU nhận diện ảnh | RTX 3090 worker riêng, ảnh được cấp phép; segmentation, change detection |
| C8 | Lịch sử biến động | Versioned snapshots (mở/đóng/di chuyển) |
| C9 | Offline theo vùng | Tự sinh tile/GeoJSON từ dữ liệu có quyền — **không bulk-download tile OSMF** |
| C10 | Trợ lý hỏi đáp địa điểm | LLM + truy vấn dữ liệu, trả lời kèm nguồn, không bịa |

## 3. Chốt kỹ thuật (đã thống nhất + kiểm chứng với code)

- **Tìm kiếm — phương án C tăng dần:** giữ `search_text` không dấu + `remove_diacritics=2` (đã có sẵn — verified `store.py`); thêm bảng alias/tên cũ có nguồn; thêm BM25 vào ranking (thay `updated_at DESC` — verified dòng 275); prefix index & custom tokenizer **chưa làm** — chỉ khi benchmark chứng minh cần.
- **Ranking 2 tầng:** FTS5 candidate retrieval → rerank nhẹ (khớp tên chính xác → alias → BM25 → danh mục → khoảng cách → chất lượng dữ liệu). FTS hiện 1 cột ghép — boost tên bằng Python/SQL trước, tách cột khi cần.
- **Giữ nguyên lõi:** Python stdlib, SQLite WAL/FTS5, Leaflet, SSE, Docker 1 service. **Không**: custom tokenizer, GPU, PostGIS, Elasticsearch, microservices ở vòng này.
- **Không ưu tiên lúc này:** 3D hàng loạt, street-view tự tạo, AI trong từng truy vấn, thay Leaflet, tự xây routing.

## 4. Thứ tự thực thi + tiêu chí dừng

**Bước 0:** Khóa bộ 30–40 truy vấn benchmark trên pilot 5.000 POI (chưa đổi thuật toán)
**Bước 1:** Import full PBF vào staging (không `--limit`)
**Bước 2:** Data Quality Gate
**Bước 3:** Sync an toàn (dry-run → đối chiếu → backup → apply → verify)
**Bước 4:** Mở rộng 150 truy vấn chuẩn có nhãn (40 tên chính xác · 30 có/không dấu · 25 alias · 25 danh mục+địa danh · 20 hành chính cũ/mới · 10 no-match)
**Bước 5:** Tối ưu FTS5/BM25/alias → regression

| Hạng mục | Tiêu chí PASS |
|---|---|
| Full OSM | SHA-256 khớp provenance; snapshot full thật; không lỗi; báo cáo node/way/relation |
| Data Quality | `integrity_check=ok`; 0 trùng `(osm_type,osm_id)`; 0 tọa độ sai; giữ provenance; không ghi đè user/verified |
| Sync | Dry-run/apply đối soát; backup restore thử; idempotent |
| Search | Hit@10 ≥95% (tên), ≥95% (không dấu), ≥90% (alias); MRR@10 ≥0.85; p95 <200ms |
| Regression | Unit + browser + data snapshot đều đạt |

## 5. ⚠️ 2 điểm importer cần xử lý trước full import (verified trong code)

> **Cập nhật 2026-10-11 (vòng A): cả 2 điểm đã xử lý trong toolkit.**
> 1. ✅ `verify_osm_snapshot.py` thêm `--require-full` + `full_snapshots` + `latest_snapshot` (chỉ pass khi có snapshot full-scope thật). Full import đã chạy: snapshot `pbf:full:retire=0` — verify pass với `--min-active 90000 --require-full`.
> 2. ✅ `sync_osm_into_atlas.py` thêm `--coverage-report` (type mix, top categories, lưới 1°). Province vẫn **không** suy từ tâm điểm — giữ đúng nguyên tắc.

1. `verify_osm_snapshot.py` — `--min-active` default = **1** (dòng 51): đủ smoke test, **không đủ chứng nhận full import**. Cần: chạy full mode, đối chiếu số bản ghi theo loại + số bị bỏ qua với PBF nguồn.
2. `sync_osm_into_atlas.py` — ghi `province=""` cho bản ghi OSM mới (dòng 90): không thể đếm độ phủ 34 tỉnh. Trước khi có polygon tin cậy: báo cáo theo lưới tọa độ/địa danh nguồn (nhãn chưa xác minh). **Không** bật `--mark-missing` từ pilot 5.000.

## 6. Workstream giao việc

| WS | Nội dung | Phụ thuộc |
|---|---|---|
| A — OSM Full | Import full, manifest, số liệu thực, staging QA | Có PBF đã xác minh ✅ |
| B — Data Quality | Nguồn, trùng, dữ liệu thiếu, sync/restore, đối chiếu snapshot | A xong staging |
| C — Search Quality | Benchmark ban đầu → baseline full → BM25/alias → benchmark lại | Song song A; tối ưu sau B |

**Điều kiện dừng vòng này:** 1 snapshot POI toàn quốc nhập + kiểm chứng an toàn; 150 truy vấn gắn nhãn; search đạt ngưỡng; báo cáo độ phủ thực tế (có gì / thiếu gì).

---

*Nguồn: thảo luận 2 vòng với GPT (2026-10-11) + xác minh chéo code (store.py, verify_osm_snapshot.py, sync_osm_into_atlas.py — 4/4 claim khớp). Các ngưỡng là release target, chưa phải số liệu repo đã đạt.*
