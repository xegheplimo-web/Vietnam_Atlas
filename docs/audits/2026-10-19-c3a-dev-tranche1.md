# C3a — Dev tranche 1: 40 câu soạn từ nguồn ngoài (19/10/2026)

## 1. Vì sao phải soạn lại

Bộ 100 câu `benchmarks/e3a/pilot/` lấy tên POI **trực tiếp từ atlas/OSM** ⇒ vi phạm
contract §5.4 và dùng toạ độ OSM cho G5 ⇒ vi phạm §5.5. Nó bị hạ cấp thành
**smoke set** (không công bố số). Dev/holdout chính thức bắt buộc soạn từ **nguồn ngoài**.

Tranche 1 = 40 câu, 4 người soạn theo địa bàn, mỗi câu 1 nguồn ngoài kiểm chứng được:

| Địa bàn | Số câu | Nhóm |
|---|---|---|
| Hà Nội | 10 | G1 3 · G2 3 · G3 2 · G4 2 |
| TP.HCM | 10 | G1 3 · G2 3 · G3 2 · G4 2 |
| Đà Nẵng/Huế/Hải Phòng/Cần Thơ | 10 | (G1 2 · G2 2 · G3 4 · G4 1 · G7 1) |
| Toàn quốc (chuỗi/trùng tên/no-match) | 10 | G1 2 · G2 2 · G3 2 · G6 3 · G7 1 |

Tổng: **G1 10 · G2 10 · G3 10 · G4 5 · G6 3 · G7 2**.

## 2. Kiểm nguồn (máy kiểm, không tin lời người soạn)

`tools/dev_ingest.py` mở **từng** URL, đối chiếu snippet phải là chuỗi con nguyên văn:

| Chỉ số | Trước khi siết | Sau khi siết |
|---|---|---|
| URL mở được | 39/40 (1 lỗi TLS `gialai.gov.vn`) | **40/40** |
| Snippet khớp nguyên văn | 22/40 | **40/40** |
| — tự trích lại từ trang (lệch nhẹ, similarity ≥ 0.75) | — | 4 |
| — xác minh bằng trình duyệt thật (trang render JS) | — | 7 |

Các lỗi thật đã phát hiện ở bước này (người soạn làm sai, máy bắt được):
- **HN-06**: tự đổi "phường Cầu Giấy" → "quận Cầu Giấy" trong snippet.
- **HN-07**: ghép câu từ nhiều dòng bảng, thêm ký tự không có trên trang.
- **M-05, N-06**: viết lại câu theo ý mình thay vì copy nguyên văn.
- **N-09, HCM-06, HCM-10, HN-04, N-01**: snippet đúng nhưng nằm trong trang render JS —
  phải xác minh bằng trình duyệt (đã ghi `verified_via: "browser"`).
- **N-02**: nguồn (vietnamtourism) chặn bot ⇒ thay bằng bài của Sở VH-TT-DL Lào Cai.

Sửa hạ tầng đo trong `tools/dev_ingest.py`: giải nén gzip/deflate, chuẩn hoá NFC
(trang dùng Unicode tổ hợp, snippet dùng dựng sẵn), NBSP/zero-width/dấu nháy,
trích lại snippet theo **ranh giới câu** (không cắt giữa từ).

## 3. Ánh xạ gold → place_id (verify, không dùng để chọn câu hỏi)

51 qrels nháp, `grade 3`, `judgment_status: pending` — **chờ Sếp duyệt**.

Phải làm rõ 2 nhóm:
- **Trùng thực thể, khác tên** (đã map, ghi `gold_aliases`):
  `Đại học Cần Thơ` ↔ `Đại học Cần Thơ (Khu 1)`;
  `Địa đạo Củ Chi` ↔ `Địa Đạo Bến Đình Củ Chi`;
  `Trường ĐH Bách khoa - ĐHQG TP.HCM` ↔ `Trường Đại học Bách khoa, Đại học Quốc gia TP.HCM`;
  `Bảo tàng Điêu khắc Chăm Đà Nẵng` ↔ `Bảo tàng Nghệ thuật Điêu khắc Chăm Đà Nẵng`;
  `Bệnh viện đa khoa tư nhân Hà Thành/Hồng Hà` ↔ `Bệnh viện Đa khoa Hà Thành/Hồng Hà`.
- **Chuỗi nhiều chi nhánh (G6)**: `gold_like` chọn tối đa 3 POI **khác tỉnh** —
  Long Châu (01/20/22), Vinmec (01/22/31), Phúc Long (01/24/31).

Mã tỉnh phải theo **địa giới 2025** (corpus dùng mã sau sáp nhập): N-02 Lào Cai `10→15`,
N-10 Hà Giang `2→08`.

## 4. Coverage gap thật — 4/40 câu (10%)

| Câu | Gold | Kết luận |
|---|---|---|
| HN-03 | Văn Miếu - Quốc Tử Giám | corpus **không có** di tích này (chỉ có tên phường/trường trùng) |
| M-05 | Sơn Trà Marina, Hải Vân Xanh Glamping | 2 quán trong bài của cổng du lịch Đà Nẵng, corpus không có |
| N-04 | Hồ Lắk | corpus không có hồ này (Đắk Lắk chỉ có vài POI) |
| N-06 | House Of Len Coffee, 1990 Cafe | 2 quán cà phê Buôn Ma Thuột, corpus không có |

Đây là **tín hiệu dữ liệu**, không phải lỗi soạn: Coverage Gap Rate tranche 1 = 10%
(GPT cho phép tới ~15% trước khi freeze). Văn Miếu thiếu là gap đáng chú ý nhất
(địa danh cấp quốc gia).

## 5. Lỗi hạ tầng phát hiện khi chạy 40 câu qua harness

Lần chạy đầu: **18/40 câu legacy-http timeout và 40/40 admin-http timeout** (cùng ngưỡng 2 s),
trong khi gọi tay server trên bản working copy chỉ mất **12–130 ms**.

Nguyên nhân: `spawn_server()` mở server với `stdout=subprocess.PIPE` **không ai đọc** →
buffer pipe đầy (~64KB log truy cập) → server block ở `write()` → mọi request sau đó timeout oan.
Đã sửa: ghi log server ra **file** `server.log` cạnh working copy, đóng file trong `stop_server()`,
lỗi khởi động đọc đuôi log (`_tail`). Bài học: harness tự chạy server thì phải drain/đổi hướng stdout.

## 6. Việc còn lại trước khi công nhận tranche 1

1. Sếp duyệt 40 câu + 51 qrels nháp (đổi `judgment_status` → `verified`, ghi `judged_by`).
2. Chạy lại harness sau fix → `run` + `score` thật (hiện số liệu HTTP vừa có mới hợp lệ).
3. Đo ngưỡng no-match trên tranche 1 (G7 = 2 câu là quá ít để hiệu chỉnh — cần thêm data ở tranche 2).
4. G5 (toạ độ) chưa soạn: §5.5 cấm toạ độ Atlas/OSM, cần chốt nguồn (cổng địa lý chính thức /
   GPS người kiểm thử / geocoder có giấy phép) trước khi soạn tranche sau.
