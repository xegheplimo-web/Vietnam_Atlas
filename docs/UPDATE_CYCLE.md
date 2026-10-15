# Chu kỳ cập nhật dữ liệu OSM

Tài liệu mô tả chu kỳ cập nhật dữ liệu OpenStreetMap vào Vietnam Atlas (Round E — Freshness v2): quy trình chạy, cách đọc báo cáo, quyết định ghi dữ liệu, rollback, và lịch chạy tự động.

## 1. Nguyên tắc an toàn

- Mặc định mọi lần chạy đều là **preview (dry-run)**: không ghi gì vào `data/atlas.sqlite3`.
- Dữ liệu chỉ được ghi khi người vận hành chạy có chủ đích với `-Apply` (tùy chọn thêm `-Update-existing`).
- `-Apply` **bắt buộc** `-ApprovedReport <path report.json>` — apply phải ràng buộc vào một báo cáo preview đã được duyệt; `tools/verify_approval.py` đối chiếu SHA-256 của PBF hiện tại và snapshot staging full mới nhất với đúng những gì report đã ghi, lệch là từ chối ghi.
- Báo cáo `reports/update-<stamp>.md` là tài liệu cần đọc **trước** khi quyết định ghi.
- Mỗi lần chạy đều chụp đầu ra thô vào `reports/_raw/<YYYYMMDDTHHMMSSZ>/` (`import.json`, `stats.txt`, `verify.txt`, `preview.json`, và `apply.json` + `postapply.json` khi chạy `-Apply`); console vẫn hiển thị qua `Tee-Object`.

## 2. Các bước tuần tự (RUN_UPDATE_WINDOWS.ps1)

| # | Bước | Công cụ | Artifact |
|---|------|---------|----------|
| 1 | Download | `tools/fetch_vn_geofabrik.py --out data/vietnam-latest.osm.pbf` | `data/vietnam-latest.osm.pbf`, `data/vietnam-latest.osm.pbf.provenance.json` |
| 2 | Import staging | `tools/vn_osm_dataset.py data/vietnam-latest.osm.pbf --db data/osm_vietnam.sqlite3 --mark-missing` (chu kỳ full) | `data/osm_vietnam.sqlite3`, `reports/_raw/<stamp>/import.json` |
| 3 | Verify | `tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --require-full --min-active 50000` (chu kỳ full) | `reports/_raw/<stamp>/verify.txt` |
| 4 | Preview | `tools/sync_osm_into_atlas.py --osm-db data/osm_vietnam.sqlite3 --atlas-db data/atlas.sqlite3` (dry-run, in đúng 1 JSON object) | `reports/_raw/<stamp>/preview.json` (`apply.json` khi `-Apply`) |
| 5 | Binding | `tools/verify_approval.py --report <approved report.json> --pbf data/vietnam-latest.osm.pbf --staging-db data/osm_vietnam.sqlite3` (chỉ khi `-Apply`) | chỉ chặn/cho qua |
| 6 | Post-apply | chạy lại sync dry-run sau apply (chỉ khi `-Apply`) | `reports/_raw/<stamp>/postapply.json` |
| 7 | Report | `tools/update_report.py` | `reports/update-<stamp>.json`, `reports/update-<stamp>.md` |

Ghi chú:

- Fetch là **etag-aware**: nguồn không đổi thì bỏ qua tải lại; `-ForceDownload` ép tải lại.
- `-SkipDownload` bỏ qua bước 1 theo kiểu **fail-closed**: thiếu PBF hoặc thiếu sidecar provenance → `throw` ngay (phải chạy không `-SkipDownload` để tải mới trước); khi đủ cả hai thì SHA-256 của PBF cục bộ phải khớp sidecar, lệch là dừng ngay (tránh báo cáo gắn nhầm metadata nguồn cũ).
- `-Limit N` chạy pilot trên N POI đầu: in cảnh báo `TEST MODE (limit)` (snapshot không được chứng nhận full-scope), verify giữ `--min-active 1` và **không** `--require-full`, import **không** kèm `--mark-missing` (importer cũng cấm cặp này), và sync tắt đánh giá retirement.
- Chu kỳ full (`-Limit 0`): import thêm `--mark-missing` để đánh dấu POI biến mất khỏi nguồn thành `is_active=0` (không bao giờ xóa; importer tự chặn khi >10% active biến mất), verify chạy `--require-full --min-active $VerifyMinActive` (tham số `-VerifyMinActive`, mặc định **50000**).
- `-OnlyDownload` chỉ tải PBF rồi thoát.
- `-Apply` chạy preview → kiểm tra `-ApprovedReport` → ghi Atlas → chạy lại dry-run lấy bằng chứng idempotency (`postapply.json`); kết quả ghi được chụp riêng ở `apply.json`, còn `preview.json` giữ nguyên bản dry-run của lần chạy đó. `-Apply -Update-existing` cập nhật thêm các dòng OSM chưa verified.

## 3. Đọc báo cáo

File `reports/update-<stamp>.md` (tiếng Việt) có các mục:

- **Nguồn OSM**: `last_modified`, 12 ký tự đầu của `sha256`, kích thước — đối chiếu với `data/vietnam-latest.osm.pbf.provenance.json`.
- **Kết quả so sánh với Atlas**: mới (`would_insert`), cập nhật (`would_update`), ngừng xuất hiện (`would_retire`), được bảo vệ (`protected`), đã có (`existing`) — tên key giữ nguyên của `tools/sync_osm_into_atlas.py`.
- **Staging/verify**: đường dẫn file thô `stats.txt`, `verify.txt`.
- **Snapshot & ràng buộc phê duyệt**: `stage` (`preview`/`apply` theo `mode` của sync), SHA-256 sidecar, file nguồn + kích thước, `source_date`, snapshot staging mới nhất (`id`, `mode`, `file_sha256`, `imported_at`) và số snapshot full-scope. Chỉ report `stage=preview` mới được dùng làm `-ApprovedReport`.
- **Chất lượng dữ liệu**: `invalid_coordinates` (từ `verify.txt`), số dòng active ngoài bbox Việt Nam (`outside_region`, kèm mẫu ≤10 dòng), `skip_reasons` từ `import.json` (khi importer mới có), `position_jumps` từ sync (khi lane khác merge).
- **Ứng viên retire (staging-only)**: dòng staging `is_active=0` còn dòng Atlas `source='OpenStreetMap'` — tổng đang theo dõi, mới trong lần chạy, `ready_for_review`, `cancelled_reappeared`, `protected_missing` (Atlas không còn `unverified` → không retire), mẫu ≤15 dòng.
- **POI tái xuất hiện (would_restore)**: `would_restore`, `restore_blocked_manual`, `changed_in_source` từ kết quả sync (đọc kiểu optional — vắng trong JSON cũ vẫn không lỗi).
- **Kiểm tra sau apply**: `would_insert`/`would_update` còn lại của dry-run chạy lại sau apply; `ok` khi cả hai bằng 0, ngược lại in cảnh báo.
- **Kết luận**:
  - `CÓ thay đổi — xem xét trước khi chạy -Apply` khi có ít nhất một thay đổi.
  - `Không thay đổi — chu kỳ lặp idempotent` khi cả ba đếm đều bằng 0.

File `reports/update-<stamp>.json` là bản máy (cùng các key đếm, thêm `verdict`, `provenance`, `snapshot`, `staging`, `stage`, `changes`, `quality`, `retire_candidates`, `post_apply`, `artifacts`) để script khác đọc tiếp — và là file truyền cho `-ApprovedReport`. Sinh report cũng có thể chạy tay:

```powershell
py -3 tools/update_report.py --preview reports/_raw/<stamp>/preview.json --provenance data/vietnam-latest.osm.pbf.provenance.json --staging-stats reports/_raw/<stamp>/stats.txt --verify reports/_raw/<stamp>/verify.txt --import-json reports/_raw/<stamp>/import.json --out-dir reports --stamp <stamp>
```

## 4. Quyết định ghi dữ liệu

1. Chạy chu kỳ preview (`RUN_UPDATE_WINDOWS.ps1` không `-Apply`), đọc `reports/update-<stamp>.md`; xem xét các dòng mới / cập nhật / ngừng xuất hiện / ứng viên retire.
2. Duyệt xong, chạy `RUN_UPDATE_WINDOWS.ps1 -Apply -ApprovedReport reports/update-<stamp>.json` (thêm `-Update-existing` nếu muốn cập nhật cả dòng OSM đã có nhưng chưa verified). Thiếu `-ApprovedReport` → script `throw` ngay.
3. Trước khi ghi, `tools/verify_approval.py` chạy trong pipeline kiểm tra report đã duyệt còn khớp thực tế: `stage=="preview"`, `snapshot.sha256` trùng SHA-256 PBF hiện tại, và `staging.latest_snapshot.{id,file_sha256}` trùng snapshot **full** mới nhất trong staging. Lệch bất kỳ điều kiện nào → exit 2 → script `throw`, không ghi. PBF hay staging thay đổi sau khi duyệt = report cũ → phải chạy preview lại và duyệt report mới.
4. Khi ghi, `tools/sync_osm_into_atlas.py` tự tạo **backup online** `backups/atlas-pre-osm-sync-<YYYYMMDDTHHMMSSZ>.sqlite3` bằng SQLite online backup và chạy `PRAGMA integrity_check` ngay sau khi tạo.
5. Lỗi ở bước sinh báo cáo làm script `throw` — kể cả khi apply đã hoàn tất (đánh dấu rõ chu kỳ đã ghi dữ liệu nhưng thiếu báo cáo).

## 5. Rollback

1. Dừng server đang chạy trên `data/atlas.sqlite3`.
2. Chọn backup mới nhất `backups/atlas-pre-osm-sync-*.sqlite3` (đã kiểm tra `integrity_check` lúc tạo).
3. Di chuyển `data/atlas.sqlite3` hiện tại sang tên tạm (không xóa ngay), copy backup đè lên `data/atlas.sqlite3`.
4. Chạy `PRAGMA integrity_check` trên bản vừa restore; phải ra `ok` mới khởi động server.

## 6. Được bảo vệ

- `protected`: dòng đã có trong Atlas mà sync không sửa/xóa. Ý nghĩa tùy chế độ chạy:
  - **Mặc định (không `-Update-existing`)**: mọi dòng đã có đều bị tính `protected` (kể cả OSM `unverified`) vì tính năng cập nhật đang tắt.
  - **Khi `-Update-existing`**: chỉ dòng `verified`/do người dùng tạo được bảo vệ; dòng `unverified` được phép cập nhật (và đếm vào `would_update`).
- `would_retire` chỉ áp dụng cho dòng OSM `unverified` tương ứng đã ngừng xuất hiện trong snapshot staging (import với `--mark-missing` đánh bộ inactive, không bao giờ xóa).
- **Retirement tại E1**: chu kỳ full giờ chạy import kèm `--mark-missing`, nên staging đánh dấu được POI biến mất khỏi OSM. Ở slice E1, retire/restore vẫn là **candidates-only**: report chỉ ghi nhận ứng viên retire (xem mục 3) và đếm `would_retire`/`would_restore`/`restore_blocked_manual`; quyết định ghi lifecycle thật (cờ `--apply-lifecycle` trong `tools/sync_osm_into_atlas.py`) do lane khác phát triển.
- **Ứng viên retire theo snapshot**: `tools/update_report.py` theo dõi từng `sid` inactive qua các lần chạy trong state file `reports/_state/retire_candidates.json` — `sid` mới vào `candidate`, lên `ready_for_review` khi vắng mặt trong ≥2 snapshot full khác nhau và ≥7 ngày; `sid` tái xuất hiện (staging active trở lại) → `cancelled_reappeared`. State giữ toàn bộ candidate/ready và tối đa 1000 cancelled gần nhất.
- Dòng staging inactive mà Atlas tương ứng không còn `unverified` (verified/user) bị loại khỏi ứng viên và đếm vào `protected_missing`.

## 7. Idempotency

- Chạy lại chu kỳ trên **cùng một snapshot** (fetch bỏ qua vì etag không đổi): preview cho 0 thay đổi → kết luận `Không thay đổi — chu kỳ lặp idempotent`.
- `-Apply` khi không có diff: sync ghi 0 dòng (`mode=APPLY_NO_CHANGES`) và **không** tạo thêm backup trùng.
- Sau mỗi `-Apply` thành công, pipeline chạy lại sync dry-run một lần nữa và chụp vào `reports/_raw/<stamp>/postapply.json`: `would_insert>0` hoặc `would_update>0` nghĩa là còn thay đổi ghi được → `Write-Warning` trên console và mục **Kiểm tra sau apply** của báo cáo in cảnh báo (`would_retire`/`would_restore` là report-only ở E1, không tính vào cảnh báo).

## 8. Lịch chạy preview hàng tuần (Windows Task Scheduler)

`tools/register_update_task.ps1` (mặc định `Action = Query`):

```powershell
# Đăng ký: chạy mỗi Chủ nhật 21:30
powershell -NoProfile -ExecutionPolicy Bypass -File tools/register_update_task.ps1 -Action Register -TaskName VietnamAtlas-Update-Preview -Time 21:30 -Day SUN

# Truy vấn trạng thái
powershell -NoProfile -ExecutionPolicy Bypass -File tools/register_update_task.ps1 -Action Query -TaskName VietnamAtlas-Update-Preview

# Hủy đăng ký
powershell -NoProfile -ExecutionPolicy Bypass -File tools/register_update_task.ps1 -Action Unregister -TaskName VietnamAtlas-Update-Preview
```

**Quan trọng:** task chỉ chạy **PREVIEW** (không `-Apply`) — không có gì tự động xuất bản dữ liệu mà không có người đọc báo cáo.

## 9. Đường dẫn artifacts

- `data/vietnam-latest.osm.pbf` + `data/vietnam-latest.osm.pbf.provenance.json` — snapshot OSM và provenance.
- `data/osm_vietnam.sqlite3` — staging database (sync mở ở chế độ read-only).
- `data/atlas.sqlite3` — Atlas thật (chỉ ghi qua `-Apply`).
- `backups/atlas-pre-osm-sync-*.sqlite3` — backup trước mỗi lần ghi.
- `reports/_raw/<stamp>/` — đầu ra thô (`import.json`, `stats.txt`, `verify.txt`, `preview.json`; thêm `apply.json`, `postapply.json` khi `-Apply`) của từng lần chạy.
- `reports/update-<stamp>.{json,md}` — báo cáo của từng lần chạy; file `.json` của một preview đã duyệt chính là tham số `-ApprovedReport` cho lần `-Apply`.
- `reports/_state/retire_candidates.json` — state file theo dõi ứng viên retire qua các lần chạy (ghi atomic, tự phục hồi khi file hỏng).

