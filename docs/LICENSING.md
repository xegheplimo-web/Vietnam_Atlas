# Giấy phép, nguồn và ghi công

## Tài liệu và dữ liệu nền

- **OpenStreetMap**: © OpenStreetMap contributors, ODbL 1.0. https://www.openstreetmap.org/copyright
- **Geofabrik Vietnam extract**: https://download.geofabrik.de/asia/vietnam.html — bản OSM PBF lấy riêng theo thời điểm chạy, không đi kèm ZIP.
- **Leaflet**: BSD 2-Clause, thư viện tải từ CDN https://leafletjs.com/
- **CARTO** và **OpenTopoMap**: nền bản đồ tùy chọn; tuân thủ quy định bản đồ/tile và yêu cầu attribution tương ứng.
- **Nghị quyết 202/2025/QH15** về tỉnh/thành mới: https://baochinhphu.vn/nghi-quyet-cua-quoc-hoi-ve-sap-xep-don-vi-hanh-chinh-cap-tinh-102250612191145158.htm

## Google Maps

Dự án `gosom/google-maps-scraper` có giấy phép phần mềm MIT, nhưng giấy phép của scraper không cấp quyền sao chép/lưu/phân phối số lượng lớn nội dung Google Maps. Gói này không tự động scrape Google Maps hay đóng gói lại ảnh, review, listing Google Maps. Nếu sau này nối nguồn dữ liệu khác, xác minh điều khoản/giấy phép phù hợp trước khi thu thập/lưu.

## OpenStreetMap tile policy

Không dùng `tile.openstreetmap.org` để tải sẵn (bulk prefetch) cả nước hoặc lưu cho offline. Tile chỉ tải khi người dùng thực sự xem. Offline/server quy mô lớn cần self-host hoặc nhà cung cấp tile có giấy phép phù hợp.
https://operations.osmfoundation.org/policies/tiles/

## Tác phẩm ví dụ

Các điểm DEMO trong `data/sample_import.csv` và `data/sample_places_synthetic.geojson` đều được tạo để minh họa, **không phải dữ liệu thực**.

Mã nguồn mới của template này dành cho sử dụng và chỉnh sửa trong dự án của bạn; các thư viện, nội dung bản đồ và dữ liệu bên thứ ba tiếp tục chịu giấy phép riêng của chúng.
