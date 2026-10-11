# Giấy phép phần mềm Vietnam Atlas (phạm vi hiện hành)

- **Mã nguồn thuộc quyền cấp phép của xegheplimo-web trong bản phân phối hiện hành:** PolyForm Noncommercial License 1.0.0. Xem [LICENSE](../LICENSE) (toàn văn chính thức, không sửa đổi) và [NOTICE](../NOTICE).
- **Repository Public:** ai cũng xem được mã, nhưng việc kinh doanh, bán lại, sử dụng dưới dạng dịch vụ có mục đích thương mại hoặc tích hợp sản phẩm thương mại **không được cấp phép mặc định**. Xem [COMMERCIAL_USE.md](../COMMERCIAL_USE.md).
- **Quyền MIT đã cấp trước đây:** Các phiên bản cũ được phân phối hợp lệ theo MIT tiếp tục có quyền theo MIT; thay đổi giấy phép **không hồi tố**. Không tuyên bố độc quyền những thành phần đã được bên khác cấp quyền theo MIT.
- **Phần mềm và dữ liệu tách biệt:** PolyForm của dự án **không áp dụng** cho OSM/Geofabrik (ODbL), Leaflet (BSD), bản đồ nền, dữ liệu chính quyền hay dữ liệu do người khác sở hữu. Quyền thương mại đối với OSM vẫn tuân theo ODbL.
- **Lưu ý ngoại lệ:** PolyForm Noncommercial cho phép một số loại tổ chức phi thương mại sử dụng, kể cả khi có nguồn tài trợ. Bảng ví dụ trong COMMERCIAL_USE chỉ mang tính giải thích; nội dung LICENSE quyết định quyền và nghĩa vụ.
- **Mã phát triển tiếp:** Kiểm tra quyền tác giả, đóng góp và giấy phép của thành phần nhập từ dự án khác trước khi tái cấp phép. Không tự ý áp PolyForm lên những phần không có quyền sở hữu.

---

# Giấy phép, nguồn và ghi công

## Tài liệu và dữ liệu nền

- **OpenStreetMap**: © OpenStreetMap contributors, ODbL 1.0. https://www.openstreetmap.org/copyright
- **Geofabrik Vietnam extract**: https://download.geofabrik.de/asia/vietnam.html — bản OSM PBF lấy riêng theo thời điểm chạy, không đi kèm ZIP.
- **Leaflet**: BSD 2-Clause, thư viện tải từ CDN https://leafletjs.com/
- **CARTO** và **OpenTopoMap**: nền bản đồ tùy chọn; tuân thủ quy định bản đồ/tile và yêu cầu attribution tương ứng.
- **Nghị quyết 202/2025/QH15** về tỉnh/thành mới: https://baochinhphu.vn/nghi-quyet-cua-quoc-hoi-ve-sap-xep-don-vi-hanh-chinh-cap-tinh-102250612191145158.htm
- **Ranh giới hành chính 34 tỉnh** (`data/provinces/provinces_34.geojson`): hình học từ "Vietnam Administrative Units Reference Map" — sapnhap.bando.com.vn (NXB Tài nguyên, Môi trường và Bản đồ Việt Nam — Bộ NN&MT); tổng hợp qua repo `thanglequoc/vietnamese-provinces-database` (code + dataset compilation: MIT, © 2021 Thang Le Quoc). Dữ liệu bản đồ nhà nước công bố công khai nhưng **chưa có giấy phép mở riêng** — tham chiếu điều khoản nhà cung cấp trước khi dùng cho mục đích thương mại; giữ nguyên attribution khi tái phân phối (xem `data/provinces/PROVENANCE.json`). Tên/mã đơn vị hành chính theo GSO (Nghị quyết 388/NQ-UBTVQH16).

## Google Maps

Dự án `gosom/google-maps-scraper` có giấy phép phần mềm MIT, nhưng giấy phép của scraper không cấp quyền sao chép/lưu/phân phối số lượng lớn nội dung Google Maps. Gói này không tự động scrape Google Maps hay đóng gói lại ảnh, review, listing Google Maps. Nếu sau này nối nguồn dữ liệu khác, xác minh điều khoản/giấy phép phù hợp trước khi thu thập/lưu.

## OpenStreetMap tile policy

Không dùng `tile.openstreetmap.org` để tải sẵn (bulk prefetch) cả nước hoặc lưu cho offline. Tile chỉ tải khi người dùng thực sự xem. Offline/server quy mô lớn cần self-host hoặc nhà cung cấp tile có giấy phép phù hợp.
https://operations.osmfoundation.org/policies/tiles/

## Tác phẩm ví dụ

Các điểm DEMO trong `data/sample_import.csv` và `data/sample_places_synthetic.geojson` đều được tạo để minh họa, **không phải dữ liệu thực**.

Mã nguồn mới của template này dành cho sử dụng và chỉnh sửa trong dự án của bạn; các thư viện, nội dung bản đồ và dữ liệu bên thứ ba tiếp tục chịu giấy phép riêng của chúng.
