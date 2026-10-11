# Data rights — bắt buộc tách khỏi giấy phép mã nguồn

Mã Vietnam_Atlas hiện công khai theo **PolyForm Noncommercial** cho mã thuộc quyền cấp phép của dự án.

**OSM:** © OpenStreetMap contributors — ODbL 1.0. Dữ liệu OSM được phép dùng cả cho mục đích thương mại theo ODbL. Chủ repo không có quyền áp hạn chế phi thương mại của PolyForm lên các bản ghi OSM gốc hoặc bản phái sinh bắt buộc chia sẻ theo ODbL. Khi công khai kho kết hợp, cần xác định rõ derivative vs collective database theo OSMF; việc gộp thuộc tính vào cùng bảng có thể làm phát sinh nghĩa vụ share-alike. Xem:
- https://www.openstreetmap.org/copyright
- https://osmfoundation.org/wiki/Licence_and_Legal_FAQ
- https://osmfoundation.org/wiki/Licence/Community_Guidelines/Collective_Database_Guideline_Guideline

**Doanh nghiệp:** nguồn đăng ký kinh doanh không tự động là public-domain/open-data. Chỉ nhập dữ liệu mà bên cung cấp cho phép lưu, sử dụng, và phân phối theo phạm vi dự định. Dùng `import_authorized_registry.py` có `--source-url` + `--rights-basis` để lưu provenance, nhưng **có metadata không thay thế việc thật sự có quyền**. Không cam kết tự đồng bộ cơ sở đăng ký pháp nhân quốc gia.

**Google:** không trích xuất hay tái phát hành dữ liệu Google Maps bằng scraper từ gói này, không lưu ảnh/review Google. Giấy phép MIT của phần mềm gosom scraper **không** cấp quyền sử dụng Google Maps Content.

**Dữ liệu người dùng:** giữ riêng, không tự xuất công khai trong nguồn dataset OSM; việc sửa bản ghi người dùng không được thực hiện bởi lần cập nhật OSM.

Nếu cung cấp API miễn phí cho cộng đồng từ dữ liệu OSM, hiển thị attribution và đưa dữ liệu/ phương thức tạo bản phái sinh theo điều khoản ODbL hiện hành. Tránh việc đồng bộ rồi áp một giấy phép PolyForm riêng lên toàn bộ dữ liệu xuất CSV/GeoJSON.
