# Audit kỹ thuật Vietnam Atlas — 19/10/2026

- **Repo**: `F:\Vietnam_Atlas_Full_2026` @ `7b574d8` (main = origin/main, working tree clean)
- **Corpus**: khóa 17/10/2026 — snapshot atlas SHA-256 `14293e39…203fe` (98.938 POI), gazetteer SHA-256 `ef7f8fef…635996`
- **Phạm vi**: đánh giá mức sẵn sàng v1 theo `contracts/E3A_CONTRACT.md` và 4 trụ nghiệm thu trong `_prompts/V1-release-tracker.md`
- **Phương pháp**: đọc mã nguồn → chạy gate thật → probe hành vi tìm kiếm trên corpus khóa → đối chiếu record khóa. Mọi số liệu có lệnh tái lập ở §12.

---

## 0. Tóm tắt điều hành

1. **Nền tảng vững**: stdlib-only, ít phụ thuộc, dữ liệu có provenance, corpus lock đứng vững (đã xác minh lại hôm nay: nội dung 6/6 bảng trùng khớp snapshot).
2. **Chất lượng tìm kiếm đang được đo bằng một dụng cụ đã bão hòa**: bộ 150 câu đạt Hit@10 99,26% và tái lập chính xác, nhưng nó không còn khả năng phân biệt chất lượng.
3. **Bốn lỗi hệ thống đã xác minh bằng probe độc lập** mà bộ 150 không phát hiện: token hành chính gây nhiễu, không có chịu lỗi gõ, geo scope chỉ cấp tỉnh, không có ngưỡng "không có kết quả".
4. **Hạ tầng nghiệm thu v1 (V1-02) chưa tồn tại**: không có `tools/benchmark_e3a.py`, `benchmarks/e3a/`, `private_holdout/`.
5. **Đề xuất**: dựng harness trước → pilot 100 câu để kiểm định dụng cụ đo → hoàn tất 300 → freeze → baseline → sửa theo gap. **Không sửa ranking trước baseline.**

---

## 1. Trạng thái xác minh (19/10/2026)

| Hạng mục | Kết quả | Bằng chứng |
|---|---|---|
| Git | sạch, `main` = `origin/main` @ `7b574d8` | `git status`, `git log origin/main -1` |
| CI (`CI`) | SUCCESS @ `7b574d8` | `gh run list --workflow CI` |
| CodeQL | SUCCESS (19/10 03:33Z) | `gh run list` |
| PR mở | 0 | `gh pr list` |
| Test local (246) | **245 pass · 1 fail · 1 skip** | `uv run --with xlrd --with openpyxl python -m unittest discover -s tests` |
| Ruff lint + format | PASS (42 files formatted) | `ruff check .`, `ruff format --check .` |
| Runtime smoke trên bản sao | PASS — `/api/health`, `/api/stats`, `/api/places?search=…`, `/api/admin/resolve`, UI 200 | §12.3 |
| Corpus lock (nội dung) | PASS — rowhash 6/6 bảng identical live ↔ snapshot | §12.2 |
| Regression 150 trên snapshot | tái lập đúng: Hit@10 **0,9926** · MRR **0,8841** · Recall@10 **0,8555** · no-match FP 0/10 · p95 **39,5 ms** | §12.4 |
| V1-02 artifacts | **ABSENT** | `git ls-files | grep e3a` → rỗng |
| V1-05 drill evidence | **ABSENT** | không có report backup→restore→smoke |
| Tag / release | không có | `git tag` rỗng |

**Ghi chú quan trọng về test đỏ:** `test_golden_top5_unchanged` fail với `query 'phở' total 11894 != 11895`. CI xanh vì test DB-dependent bị `skipUnless` khi checkout không có DB — nghĩa là **chỉ chạy local mới thấy**. Nguyên nhân đã xác định: fix token 1 dòng `way/1331423706` trước khi khóa corpus (ghi trong C3 của lock record) làm tổng giảm đúng 1, fixture chưa được re-baseline.

Test skip là `TestRealImport` (cần `data/gazetteer/raw/` + `xlrd`/`openpyxl`; đã xác minh pass khi có đủ dev deps — 246 test chạy, chỉ còn 1 fail golden).

---

## 2. Kiến trúc as-built

```
server/    app.py 509 · store.py 751 · geo.py 341 · live.py 110        = 1.711 dòng
tools/     25 CLI                                                      = 6.271 dòng
tests/     8 module                                                    = 4.395 dòng
web/       app.js 439 · index.html · style.css
```

- **Runtime**: Python stdlib thuần (`http.server.ThreadingHTTPServer`), SQLite **WAL**, FTS5, không dependency runtime.
- **UI**: Leaflet + `web/app.js` (439 dòng): tìm kiếm, filter category, "gần tôi" (`near`), bbox theo viewport, CRUD điểm, export GeoJSON/CSV, panel chi tiết.
- **API**: `/api/places` (search, province, category, near, bbox, admin=1), `/api/admin/resolve`, `/api/provinces`, `/api/stats`, `/api/health`, `/api/live`, `/api/export.geojson|.csv`, `/api/places/<id>`, POST/PUT/DELETE place + memories/media.
- **Pipeline dữ liệu**: `fetch_vn_geofabrik` → `vn_osm_dataset` (PBF→staging) → `verify_osm_snapshot` → `sync_osm_into_atlas` (insert-only, bảo vệ verified/user) → `change_manifest` (fingerprint) → `verify_approval` → `apply` → `update_report` → rollback.
- **Địa danh**: `import_gazetteer` (NSO, 7 nguồn) → `admin_unit` 14.174 đơn vị (34 tỉnh hiện hành + 66 tỉnh lịch sử + 696 huyện lịch sử + 3.321 phường hiện hành + 10.057 phường lịch sử) + `admin_alias` + `admin_relation` + `admin_fts`; resolver hỗ trợ chuỗi lịch sử (`as_of`, `chain_current`).
- **CI**: matrix Python 3.10–3.14 trên Ubuntu + 3.12 Windows, coverage 86%, ruff lint + format, `compileall`, `node --check`, validate JSON, Docker build + smoke `/api/health`, CodeQL.

**Đánh giá**: kiến trúc đúng hướng cho một sản phẩm offline-first, tự lưu trữ, phi thương mại. Không có dấu hiệu over-engineering. Chi phí vận hành gần bằng 0.

---

## 3. Đường tìm kiếm — trọng tâm nghiệm thu

Chuỗi xử lý hiện tại (`server/store.py`):

1. `normalize()` — lowercase, `đ→d`, bỏ dấu NFD (mất thanh điệu).
2. Alias map (8 seed: `bv`, `ks`, `dh`, `tp`, `tx`, `tt`, `dv`, `ubnd`) + phrase alias tỉnh cũ→mới từ `province_mergers_2025.json`.
3. FTS5 `MATCH` = **AND của các prefix token** (`"quan"* AND "an"* …`), có OR mở rộng cho alias.
4. **Geo scope cấp tỉnh** (nếu query chứa tên tỉnh): UNION 2 nhánh — pool text đầy đủ (BM25 toàn token) ∪ pool trong polygon tỉnh (token còn lại).
5. Xếp hạng: `name_norm == query` → `grp tier` → accent-faithful → `bm25` → `updated_at DESC` → `id`.

Không có: phân loại ý định (loại POI vs tên riêng vs địa danh), trọng số trường (name ≫ address), popularity/quality signal, chịu lỗi gõ, ngưỡng relevance, khử trùng lặp.

**Hệ quả đo được**: engine rất mạnh với truy vấn "tên riêng chính xác hoặc gần đúng" và "category + tên tỉnh", nhưng yếu với ngôn ngữ tự nhiên có hư từ/đơn vị hành chính — đúng nhóm G2/G3/G7 của bộ 300.

---

## 4. Dữ liệu

| Chỉ số | Giá trị |
|---|---|
| POI | 98.938 |
| Có `province_code` | 97.212 (98,3%) |
| Có `address` | 36.798 (37,2%) — 62.140 dòng trống địa chỉ |
| Toạ độ null | 0 |
| Polygon tỉnh | 34 (GSO) |
| Gazetteer | 14.174 đơn vị · 3.321 phường hiện hành |
| Lịch sử thay đổi | 99.147 bản ghi |
| Phân bố category | amenity 57.348 · shop 20.840 · tourism 11.193 · office 5.293 · leisure 3.871 |

- Địa chỉ được compose từ `addr:*` (số nhà + đường → phường → tỉnh), đã chuẩn hoá ở V1-03.
- **`province` (text) rỗng 100%**; chỉ `province_code` được backfill → mọi metric/filter dựa trên cột `province` đều sai (xem P7).
- Tiền tố "Thành phố/Tỉnh" trong address được strip **case-sensitive**, nên 1.239 dòng còn token `thanh pho` (OSM ghi lowercase `thanh pho hue`).

---

## 5. Vòng đời cập nhật dữ liệu

Đã có: preview mặc định (dry-run) → change-manifest với fingerprint (chống đổi tập thay đổi sau khi duyệt) → operator approval → apply trong transaction → report JSON/MD → rollback; chạy hằng tuần qua Task Scheduler; idempotent; verified/user records được bảo vệ.

Thiếu để đóng trụ Data Freshness: **bằng chứng drill** backup → restore → smoke trên bản sao (V1-05), và **F1-mini lifecycle ledger** (V1-06, không chặn v1).

---

## 6. Phát hiện (đã xác minh)

Mức: **P0** = chặn nghiệm thu v1 · **P1** = ảnh hưởng trực tiếp chất lượng đo · **P2** = nợ kỹ thuật.

### P0-1. Dụng cụ đo v1 chưa tồn tại
Không có `tools/benchmark_e3a.py`, `benchmarks/e3a/`, `private_holdout/`, `rubric_v1.md`, `schema_v1.json`, qrels. Trụ Search Quality **không thể** nghiệm thu.
Ngoài ra `private_holdout/` **chưa có trong `.gitignore`** → rủi ro commit lộ holdout ngay khi bắt đầu tạo file.

### P0-2. Golden fixture lệch 1 dòng so với corpus khóa
`tests/fixtures/search_golden.json` kỳ vọng `phở` = 11.895; corpus khóa trả 11.894. Nguyên nhân: fix token dòng `way/1331423706` (C3) — hành vi đúng, fixture chưa re-baseline. Test local đỏ, CI không thấy.

### P1-1. Bộ 150 câu đã bão hòa
Hit@10 99,26%, MRR 0,8841, no-match FP 0/10 — tái lập chính xác hôm nay. Nhưng 136 câu có nhãn sinh từ chính DB; 4 câu coverage-gap được báo riêng; query được soạn theo dữ liệu. **Số liệu cao không còn chứng minh được chất lượng** và không phát hiện được các lỗi dưới đây (đã thử: 4 nhóm lỗi đều không xuất hiện trong bộ 150).

### P1-2. Token hành chính/đường phố gây nhiễu precision
Index chứa: `phuong` 9.581 dòng · `quan` 7.557 · `xa` 4.157 · `thanh pho` 1.239 · `tinh` 1.794.
Vì query là AND của prefix, các token hư từ này khớp hàng nghìn POI không liên quan:

| Query | Tổng | Top-3 thực tế |
|---|---|---|
| `quán phớ hà nội` | **911** | WinMart+ ×3 (khớp `pho`∈"Phố", `quan`∈"Quận") |
| `quán ăn hoàn kiếm` | 6 | "Toà án nhân dân quận Hoàn Kiếm" (khớp `quan`∈"quận", `an`∈"nhân") |
| `quán ăn phường Hoàn Kiếm` | **0** | — (AND đòi đủ 5 token trong cùng một POI) |
| `quận 1` | 2.163 | "Quán 114", "Quan 123", "1995 Quan" |

Đây là lỗi **precision + false positive**, đúng nhóm `wrong_entity` / `false_positive_no_match` (severity `major`/`critical`) trong rubric.

### P1-3. Không có chịu lỗi gõ (edit-distance)
| Query | Kết quả |
|---|---|
| `bệnh viện bạch maai` | **0** |
| `đại học bách khoaa` | **0** |
| `bệnh viện bạch mai` (không dấu) | 2 ✔ |
| `khách sạn mường than` (thiếu ký tự cuối) | 16 ✔ (nhờ prefix) |

Prefix-match cứu được thiếu ký tự cuối, nhưng **không cứu được lỗi giữa từ**. Nhóm G2 (50 câu, 17 blind) sẽ mất phần lớn recall nếu giữ nguyên.

### P1-4. Geo scope chỉ cấp tỉnh
| Query | Kết quả | Nguyên nhân |
|---|---|---|
| `cà phê buôn ma thuột` | **0** | "Buôn Ma Thuột" không phải đơn vị cấp tỉnh → không có polygon/không resolve được |
| `phở việt trì` | miss (đã ghi trong benchmarks/README) | "Việt Trì" là phường thuộc Phú Thọ |
| `quán ăn phường Hoàn Kiếm` | **0** | cấp phường không tham gia scope |

Gazetteer **đã có** 3.321 phường hiện hành + alias + quan hệ lịch sử, nhưng **không có toạ độ/polygon** và search chưa dùng. Đây là khoảng trống dữ liệu, không chỉ code.

### P1-5. Không có ngưỡng "không có kết quả"
| Query rác | Kết quả |
|---|---|
| `bệnh viện vũ trụ hà nội` | **2** (Bệnh viện Mắt Việt Nhật, BV Sức khoẻ Tâm thần) |
| `nhà hàng lỗ đen` | **1** (Nhà Hàng Đèn Lồng — trùng dấu `đen`/`đèn`) |
| 8/10 probe còn lại | 0 ✔ |

Bộ 150 báo 0/10 false positive vì query no-match của nó được chọn "sạch" (`zxqv`, `mặt trăng`…). Ngưỡng GO-LIVE đòi No-match accuracy ≥ 90% trên 20 câu (6 blind) — hiện chưa có cơ chế nào để đạt một cách có kiểm soát.

### P2-1. Recency là tiebreaker xếp hạng
`ORDER BY … bm25, places.updated_at DESC`. Lần recompose 21.355 dòng làm MRR −2,94 điểm dù Hit@10 không đổi (ghi trong lock record C4). Recency là tín hiệu *vận hành*, không phải *liên quan* — nên thay bằng tín hiệu ngữ nghĩa (độ phủ token, độ dài tên, exact-prefix).

### P2-2. `/api/stats` trả sai `provinces_with_places`
Trả `0` vì đếm `DISTINCT province` trong khi cột này rỗng 100% (dữ liệu thật nằm ở `province_code` = 97.212). Metric hiển thị cho người dùng đang sai.

### P2-3. Đóng gói phát hành chưa sẵn sàng
`BUILD_MANIFEST.json` version `2.4-round-c-t2-2026-10-12` (lệch 5 commit + V1-03); không tag, không script đóng gói app, không release notes. `reports/` và `_prompts/` bị gitignore nên bằng chứng vận hành không nằm trong repo (trừ khi `git add -f`).

---

## 7. Mức sẵn sàng theo 4 trụ v1

| Trụ | Trạng thái | Thiếu gì |
|---|---|---|
| **Search Quality** | 🔴 RED | Harness + 300 câu + qrels + coverage audit; baseline 2 adapter; 4 lỗi P1-2…P1-5 chưa được định lượng |
| **Data Freshness** | 🟡 AMBER | Quy trình đủ; thiếu drill backup→restore→smoke (V1-05) và lifecycle ledger (V1-06) |
| **Geography** | 🟡 AMBER | 34 tỉnh + lịch sử + alias đủ; thiếu polygon/centroid phường (không bắt buộc cho v1) và search chưa dùng cấp phường |
| **Operations & Release** | 🟡 AMBER | CI/backup/docs/license tốt; thiếu release package, version, tag; public internet vẫn NO-GO |

---

## 8. Rủi ro chính

1. **Ảo giác chất lượng**: tiếp tục tối ưu trên bộ 150 sẽ tạo cảm giác "gần xong" trong khi bộ 300 độc lập sẽ trượt mạnh ở G2/G3/G7.
2. **Nhiễm baseline**: sửa ranking trước khi freeze sẽ làm mất khả năng quy trách nhiệm cải thiện (không còn so sánh trước/sau hợp lệ).
3. **Lộ holdout**: `private_holdout/` chưa được ignore.
4. **Trôi corpus**: mọi thay đổi dữ liệu sau freeze đều buộc re-freeze + re-baseline.
5. **Nợ nhỏ tích tụ**: golden lệch, stats sai, version lệch — rẻ nếu đóng ngay, đắt nếu để trước release.

---

## 9. Thứ tự thực thi đề xuất (chờ kiến trúc sư xác nhận)

| GĐ | Nội dung | Cổng ra |
|---|---|---|
| **0. Đóng nợ** (1–2 ngày) | Re-baseline golden + provenance · sửa `/api/stats` · thêm `private_holdout/` vào `.gitignore` · bump manifest/version | Test local 246/246 xanh · CI xanh |
| **1. Dụng cụ đo** | `tools/benchmark_e3a.py` (validate/run/judge-import/score/compare/report) + `rubric_v1.md` + `schema_v1.json` + fixtures tổng hợp + unit test scorer (no-match vs coverage-gap) | Scorer chạy đúng trên fixtures; không cần GT thật |
| **2. Pilot 100 câu** | 100 câu đầu trong quota 300, trải đều 7 nhóm, evidence ngoài Atlas | Đo được 4 lỗi P1-2…P1-5 bằng số; rubric không còn điểm mơ hồ |
| **3. Hoàn tất 300 + qrels** | 200 dev + 100 blind, pooled judgments 2 adapter, coverage audit toàn DB | `freeze_manifest.json` ký 1 lần |
| **4. Baseline → gap → sửa** | Baseline 2 adapter trên corpus khóa → gate → quyết định E3b/E3c → 2 vòng sửa có cổng | Hit@10/MRR/nDCG/no-match theo ngưỡng |
| **5. V1-05 + release** | Drill backup/restore, Docker/Win smoke, package, tag | 4 trụ PASS |

Nguyên tắc: **đo trước, sửa sau**; mọi fix chạy lại trên cùng corpus đã freeze để so sánh hợp lệ.

---

## 10. Việc không nên làm

- Không sửa ranking/adapters trước khi baseline được freeze (mất khả năng so sánh).
- Không nới ngưỡng hoặc đổi rubric để "đạt".
- Không lấy tên POI từ Atlas/OSM để sinh câu hỏi benchmark.
- Không thêm dependency runtime cho app chính.
- Không chạy server trên frozen master (G8) — luôn dùng bản sao.

---

## 11. Câu hỏi mở cho kiến trúc sư

1. Chấp nhận **pilot 100 câu trước** (trong quota 300) để kiểm định dụng cụ đo, hay bắt buộc đủ 300 mới được chạy baseline?
2. Nếu chỉ đủ ngân sách **một vòng sửa** trước blind holdout: ưu tiên precision (tách token hành chính + ngưỡng no-match) hay recall (chịu lỗi gõ + geo cấp phường)?
3. qrels: cho phép **LLM pre-judge + human spot-check ≥30% + human 100% cho grade ≥2**, hay human 100% toàn bộ ~3.000 cặp pooled?
4. No-match: dùng **ngưỡng tham số đo được** (trade-off precision/recall) hay quy tắc cứng (sau khi loại token hành chính, còn <2 token khớp → 0 kết quả)?

---

## 12. Phụ lục — lệnh tái lập

```bash
# 12.1 Git / CI
cd /f/Vietnam_Atlas_Full_2026
git status --short && git log --oneline -3 && gh run list --limit 3 --json workflowName,conclusion

# 12.2 Toàn vẹn corpus (rowhash từng bảng, so live ↔ snapshot)
python - <<'PY'
import sqlite3, hashlib
def rowhash(p):
    con = sqlite3.connect(p); con.row_factory = sqlite3.Row
    out = {}
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like '%_fts%' order by name")]
    for t in tables:
        h = hashlib.sha256()
        rows = con.execute(f"select * from {t} order by rowid").fetchall()
        for r in rows:
            h.update(repr(tuple(r)).encode())
        out[t] = (len(rows), h.hexdigest()[:16])
    con.close(); return out
live = rowhash('data/atlas.sqlite3')
snap = rowhash('backups/corpus-lock-20261017/atlas-20261017-054643-342609.sqlite3')
print({t: live[t] == snap[t] for t in live})
PY
# kết quả 19/10: changes/places/metadata/memory_records/place_media/search_aliases = SAME 6/6

# 12.3 Runtime smoke trên bản sao (KHÔNG dùng master — G8)
cp data/atlas.sqlite3 "$TMPDIR/atlas-smoke/atlas.sqlite3"
python -m server.app --db "<copy>" --host 127.0.0.1 --port 8799
curl -s http://127.0.0.1:8799/api/health
curl -s "http://127.0.0.1:8799/api/places?search=ph%E1%BB%9F%20h%C3%A0%20n%E1%BB%99i&limit=3"
curl -s "http://127.0.0.1:8799/api/admin/resolve?q=b%E1%BB%87nh%20vi%E1%BB%87n%20Tr%E1%BA%A3ng%20Bom"

# 12.4 Regression 150 trên snapshot khóa
python tools/search_benchmark.py --atlas-db "<snapshot copy>" --json-out regression150.json

# 12.5 Test + lint
uv run --no-project --with xlrd --with openpyxl python -m unittest discover -s tests
uv run --no-project --with ruff ruff check .
uv run --no-project --with ruff ruff format --check .
```

Các probe hành vi tìm kiếm ở §6 chạy trực tiếp qua `server.store.list_places(con, search=…)` trên `data/atlas.sqlite3` (đọc-only).
