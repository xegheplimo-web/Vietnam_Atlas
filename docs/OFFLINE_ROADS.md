# Dữ liệu đường và bản đồ offline đúng nghĩa

## Hai thứ khác nhau

1. **Dữ liệu đường sá OSM thô**: các điểm, way và relation trong `vietnam-latest.osm.pbf`. Có thể trích xuất `highway=*`, `name=*`, `surface=*`, `maxspeed=*`, v.v. Tệp nguồn có tất cả những gì Geofabrik xuất ra tại thời điểm tải, không phải đảm bảo mọi tuyến đường thực tế.
2. **Tile để hiển thị bản đồ**: một dịch vụ render từ dữ liệu OSM (vector/raster). Tệp PBF **không thể** mở thành lớp nền bản đồ Leaflet trực tiếp. Để offline ở mọi mức zoom, phải import và render/vector-tile.

## Phương án đề xuất cho dự án lớn

- Lưu bản `PBF` gốc có provenance, ngày tải và checksum.
- Chạy ETL OSM (osm2pgsql, imposm hoặc công cụ tương đương) ra PostGIS/mbtiles theo mục tiêu.
- Dựng tile server riêng hoặc dùng bộ dữ liệu vector tile được cấp phép (Geofabrik có bản experimental Shortbread; xác minh giấy phép, độ phủ và khả năng triển khai trước khi sử dụng).
- Cấu hình frontend chọn tile URL của tile server nội bộ.
- Không trộn SQL của POI/hồ sơ pháp nhân với bảng tile/đường thô; dùng kho chuyên biệt và API adapter nếu tải quốc gia lớn.
- Cập nhật theo lịch có kiểm chứng delta thay vì tải lại và nạp toàn bộ không cần thiết.

## Cảnh báo dịch vụ OSM công cộng

OpenStreetMap Foundation **cấm bulk download/prefetch** tile từ `tile.openstreetmap.org` để làm bản đồ offline. Ứng dụng này chỉ tải tile bình thường khi người dùng xem bản đồ. Không dùng bot để quét tile theo bbox.

Nguồn: https://operations.osmfoundation.org/policies/tiles/

## Chưa bao gồm

- Không có tile server offline sẵn chạy trong Docker Compose của ZIP này.
- Không có layer ranh giới 34 tỉnh theo polygon chính thức.
- Không có bộ định tuyến offline hoặc giao thông realtime.
- Không chứa 314 MB+ PBF vì dữ liệu thay đổi mỗi ngày và phải tải riêng.
