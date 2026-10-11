# Coverage / sự thật về độ đầy đủ dữ liệu

Ngày biên soạn: 2026-10-11 (Asia/Bangkok).

## Nguồn mở xác minh

- URL chính thức: https://download.geofabrik.de/asia/vietnam.html
- `vietnam-latest.osm.pbf`: Geofabrik ghi dữ liệu OSM đến 2026-10-09T20:21:04Z, file khoảng 314 MB vào lần truy cập ngày 11/10/2026. Số liệu **sẽ thay đổi** khi cập nhật.
- Nguồn bản đồ: © OpenStreetMap contributors, ODbL 1.0, https://www.openstreetmap.org/copyright
- Mã nguồn mô hình quan hệ OSM: node, way, area relation theo `pyosmium`, https://docs.osmcode.org/pyosmium/latest/user_manual/03-Working-with-Geometries/

## Có sẵn ngay trong ZIP này

- Tên 34 tỉnh/thành và bảng ánh xạ 63 cũ → 34 mới *từ bundle Vietnam_Atlas cũ*, không có polygon pháp lý mới.
- 2 tệp dữ liệu **GIẢ LẬP** để kiểm thử, không phải listing thực tế.
- Script tải nguồn PBF thực, nhập PBF, đồng bộ Atlas, nhập doanh nghiệp CSV có nguồn hợp lệ.
- 0 hồ sơ doanh nghiệp thật và 0 hồ sơ POI thật mới được tải và đóng gói trực tiếp vào ZIP này.

## Không nên tuyên bố

- Không khẳng định tất cả doanh nghiệp đăng ký tại Việt Nam; phần lớn hồ sơ đăng ký kinh doanh không nằm trong OSM.
- Không khẳng định mọi địa điểm còn hoạt động; OSM có độ trễ và thiếu dữ liệu ở nhiều khu vực.
- Không có ảnh Google Maps, số đăng ký kinh doanh tra từ Maps hay Google review.
- Không có API Google trả phí hoặc quá trình scrape Google hàng loạt.
- Không có số lượng POI toàn quốc đã được tính thực tế trong môi trường tạo ZIP; số chính xác chỉ có sau khi nhập PBF và chạy `--stats` trên máy người dùng.
- Không đồng nhất tên tỉnh ở địa chỉ tự do với polygon hành chính đã thẩm định.

## Cách kiểm chứng sau khi chạy trên máy người dùng

1. Đọc `data/vietnam-latest.osm.pbf.provenance.json` để biết SHA-256, ngày tải và Last-Modified từ server.
2. Đọc số lượng `active`, `node`, `way`, `relation` từ `tools/vn_osm_dataset.py --stats`.
3. Chạy thử trên 1 vùng/1 tập mẫu, đối chiếu các POI với OSM và địa phương thực địa.
4. Đánh giá thiếu/trùng theo 34 tỉnh mới, loại hình và tỷ lệ trống tên/toạ độ. Không suy từ một mẫu rằng độ phủ toàn quốc là 100%.
5. Kiểm tra SQLite `PRAGMA integrity_check`, đếm unique ID, chạy lặp kiểm thử idempotency, kiểm tra snapshot từ chối nguồn lỗi.
