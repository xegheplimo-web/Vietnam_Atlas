# Kiến trúc và dữ liệu bộ nhớ địa lý

## Khả năng thực tế

- Front-end: HTML, CSS, JavaScript, Leaflet; nền tile OSM / CARTO / OpenTopoMap (dịch vụ bên thứ ba; cần Internet).
- API: Python Standard Library `ThreadingHTTPServer`, cùng origin với front-end.
- Lưu trữ: SQLite WAL trên máy cục bộ / Docker volume; bảng `places`, `changes`, `memory_records`, `place_media`, optional SQLite FTS5.
- Kết nối: HTTP JSON, GeoJSON. Không yêu cầu tài khoản cloud hay API key để chạy localhost.
- Khả năng lớn hơn: sử dụng PostgreSQL/PostGIS qua schema **tham khảo** tại `docs/schema_postgis_optional.sql`, cần viết migration/API adapter phù hợp. Bản chạy thực tế hiện **dùng SQLite**, không tự chuyển sang PostgreSQL.

## Quy trình dữ liệu

Nguồn có quyền sử dụng → validate → chuẩn hóa tiếng Việt không dấu → xác định thực thể theo `(source,source_id)` → upsert SQLite → lưu lịch sử → tìm theo địa giới/viewport/keyword → xuất dữ liệu/backup.

**Chống trùng đang có:** duy nhất hóa `(source,source_id)` khi nguồn ID tồn tại. Chưa có fuzzy spatial entity resolution thông minh để gộp từ nhiều nguồn. Không được tự động gộp địa điểm theo tên tương tự vì có thể là hai chi nhánh khác nhau.

## Danh mục hành chính

`data/provinces_34.json` chứa 34 tên tỉnh/thành.
`data/province_mergers_2025.json` chứa bản ánh xạ 63 tỉnh/thành cũ sang 34 mới, cộng một vài alias lịch sử.

Bản đồ chưa có polygon địa giới mới cấp tỉnh/xã. **Không được** sử dụng tâm điểm điều hướng để suy ra địa giới, không lấy bbox làm thay thế chính xác cho polygon. Nếu nhập PBF OSM, cần nạp/verify ranh giới chuẩn và dùng spatial join để gán tỉnh mới từ tọa độ.

## Về dữ liệu đăng ký kinh doanh

- Một POI trên bản đồ **không đồng nghĩa** một pháp nhân đã đăng ký kinh doanh.
- Không suy luận số đăng ký kinh doanh, mã số thuế hay trạng thái đăng ký từ Google Maps/OSM.
- Tách `legal_entity` / `business_registry_record` / `point_of_interest` khi phát triển thành kho doanh nghiệp. Đối chiếu tên, MST, địa điểm, thời điểm hiệu lực.
- Ảnh, đánh giá, dữ liệu người dùng và dữ liệu Google Maps phải có căn cứ quyền sử dụng, retention và attribution. Chỉ nhúng metadata hợp lệ.

## Bảo mật

- Chạy localhost; port Docker chỉ map đến `127.0.0.1`.
- Origin check giảm nguy cơ một số cross-site writes, **không phải cơ chế xác thực đầy đủ**.
- Không có phân quyền người dùng và không có TLS. Nếu có nhu cầu truy cập từ xa, phải thiết kế thêm.
- Sao lưu SQLite dùng backup API (tránh chỉ copy file db khi WAL đang chạy).
- Nếu dự án existing `F:\enterprise-memory-platform` đã có PostGIS/pgvector, ưu tiên tận dụng nó sau khi kiểm tra, không dựng hai kho đối chọi.
