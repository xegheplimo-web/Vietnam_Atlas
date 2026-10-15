# Chu kỳ cập nhật dữ liệu OSM

Tài liệu mô tả chu kỳ cập nhật dữ liệu OpenStreetMap vào Vietnam Atlas (Round D — Data Freshness & Quality Engine v1): quy trình chạy, cách đọc báo cáo, quyết định ghi dữ liệu, rollback, và lịch chạy tự động.

## 1. Nguyên tắc an toàn

- Mặc định mọi lần chạy đều là **preview (dry-run)**: không ghi gì vào `data/atlas.sqlite3`.
- Dữ liệu chỉ được ghi khi người vận hành chạy có chủ đích với `-Apply` (tùy chọn thêm `-Update-existing`).
- Báo cáo `reports/update-<stamp>.md` là tài liệu cần đọc **trước** khi quyết định ghi.
- Mỗi lần chạy đều chụp đầu ra thô vào `reports/_raw/<YYYYMMDDTHHMMSSZ>/` (`stats.txt`, `verify.txt`, `preview.json`, và `apply.json` khi chạy `-Apply`); console vẫn hiển thị qua `Tee-Object`.

## 2. Các bước tuần tự (RUN_UPDATE_WINDOWS.ps1)

| # | Bước | Công cụ | Artifact |
|---|------|---------|----------|
| 1 | Download | `tools/fetch_vn_geofabrik.py --out data/vietnam-latest.osm.pbf` | `data/vietnam-latest.osm.pbf`, `data/vietnam-latest.osm.pbf.provenance.json` |
| 2 | Import staging | `tools/vn_osm_dataset.py data/vietnam-latest.osm.pbf --db data/osm_vietnam.sqlite3` | `data/osm_vietnam.sqlite3` |
| 3 | Verify | `tools/verify_osm_snapshot.py --db data/osm_vietnam.sqlite3 --min-active 1` | `reports/_raw/<stamp>/verify.txt` |
| 4 | Preview | `tools/sync_osm_into_atlas.py --osm-db data/osm_vietnam.sqlite3 --atlas-db data/atlas.sqlite3` (dry-run, in đúng 1 JSON object) | `reports/_raw/<stamp>/preview.json` (`apply.json` khi `-Apply`) |
| 5 | Report | `tools/update_report.py` | `reports/update-<stamp>.json`, `reports/update-<stamp>.md` |

Ghi chú:

- Fetch là **etag-aware**: nguồn không đổi thì bỏ qua tải lại; `-ForceDownload` ép tải lại.
- `-SkipDownload` bỏ qua bước 1 với một kiểm tra an toàn: SHA-256 của PBF cục bộ phải khớp sidecar provenance, lệch là dừng ngay (tránh báo cáo gắn nhầm metadata nguồn cũ).
- `-Limit N` chạy pilot trên N POI đầu (vô hiệu hóa đánh giá retirement).
- `-OnlyDownload` chỉ tải PBF rồi thoát.
- `-Apply` chạy preview rồi ghi Atlas; kết quả ghi được chụp riêng ở `apply.json`, còn `preview.json` giữ nguyên bản dry-run đã được duyệt (không bị ghi đè). `-Apply -Update-existing` cập nhật thêm các dòng OSM chưa verified.
- Không chạy kèm `-Limit` khi muốn đánh giá retirement (`would_retire`) trong tương lai: retirement chỉ có ý nghĩa trên snapshot đầy đủ.

## 3. Đọc báo cáo

File `reports/update-<stamp>.md` (tiếng Việt) có các mục:

- **Nguồn OSM**: `last_modified`, 12 ký tự đầu của `sha256`, kích thước — đối chiếu với `data/vietnam-latest.osm.pbf.provenance.json`.
- **Kết quả so sánh với Atlas**: mới (`would_insert`), cập nhật (`would_update`), ngừng xuất hiện (`would_retire`), được bảo vệ (`protected`), đã có (`existing`) — tên key giữ nguyên của `tools/sync_osm_into_atlas.py`.
- **Staging/verify**: đường dẫn file thô `stats.txt`, `verify.txt`.
- **Kết luận**:
  - `CÓ thay đổi — xem xét trước khi chạy -Apply` khi có ít nhất một thay đổi.
  - `Không thay đổi — chu kỳ lặp idempotent` khi cả ba đếm đều bằng 0.

File `reports/update-<stamp>.json` là bản máy (cùng các key đếm, thêm `verdict`, `provenance`, `artifacts`) để script khác đọc tiếp. Sinh report cũng có thể chạy tay:

```powershell
py -3 tools/update_report.py --preview reports/_raw/<stamp>/preview.json --provenance data/vietnam-latest.osm.pbf.provenance.json --staging-stats reports/_raw/<stamp>/stats.txt --verify reports/_raw/<stamp>/verify.txt --out-dir reports --stamp <stamp>
```

## 4. Quyết định ghi dữ liệu

1. Đọc `reports/update-<stamp>.md`; xem xét các dòng mới / cập nhật / ngừng xuất hiện.
2. Chạy `RUN_UPDATE_WINDOWS.ps1 -Apply` (thêm `-Update-existing` nếu muốn cập nhật cả dòng OSM đã có nhưng chưa verified).
3. Trước khi ghi, `tools/sync_osm_into_atlas.py` tự tạo **backup online** `backups/atlas-pre-osm-sync-<YYYYMMDDTHHMMSSZ>.sqlite3` bằng SQLite online backup và chạy `PRAGMA integrity_check` ngay sau khi tạo.
4. Lỗi ở bước sinh báo cáo làm script `throw` — kể cả khi apply đã hoàn tất (đánh dấu rõ chu kỳ đã ghi dữ liệu nhưng thiếu báo cáo).

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
- **Lưu ý retirement**: chu kỳ hiện chạy import **không** kèm `--mark-missing`, nên staging không đánh dấu được địa điểm biến mất khỏi OSM → `would_retire` thực tế luôn 0. Bật `--mark-missing` (chạy full, không `-Limit`) là hạng mục tiếp theo của roadmap khi muốn theo dõi các địa điểm ngừng xuất hiện.

## 7. Idempotency

- Chạy lại chu kỳ trên **cùng một snapshot** (fetch bỏ qua vì etag không đổi): preview cho 0 thay đổi → kết luận `Không thay đổi — chu kỳ lặp idempotent`.
- `-Apply` khi không có diff: sync ghi 0 dòng (`mode=APPLY_NO_CHANGES`) và **không** tạo thêm backup trùng.

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
- `reports/_raw/<stamp>/` — đầu ra thô (`stats.txt`, `verify.txt`, `preview.json`) của từng lần chạy.
- `reports/update-<stamp>.{json,md}` — báo cáo của từng lần chạy.

