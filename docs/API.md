# REST API (same-origin)

### Điểm dữ liệu

JSON mẫu:

```json
{
  "name": "Cửa hàng mẫu",
  "category": "shop",
  "address": "Địa chỉ nguyên bản",
  "province": "Bắc Ninh",
  "phone": "",
  "website": "",
  "lat": 21.1,
  "lon": 106.2,
  "notes": "Ghi nguồn và lịch sử xác minh ở đây",
  "source": "manual",
  "source_id": "my-unique-id-1",
  "status": "unverified"
}
```

- `POST /api/places`: tạo. Nếu `(source,source_id)` đã tồn tại và `source_id` không rỗng thì cập nhật bản ghi tương ứng (idempotency theo nguồn).
- `PUT /api/places/{id}`: thay thế trường dữ liệu của đối tượng đó.
- `DELETE /api/places/{id}`: xóa khỏi bảng hiện hành, ghi nhật ký thay đổi giữ `prior_json`.
- `GET /api/places?search=...&province=...&bbox=west,south,east,north&limit=1..1000&offset=...`: tìm kiếm và phân trang. BBox có thể qua kinh tuyến 180. JSON trả `items,total,limit,offset`.
  - `category=amenity:cafe|shop:`: lọc theo 1–20 prefix OSM phân tách `|` (chữ thường, `a-z0-9_:`); sai định dạng trả 400.
  - `near=lat,lon`: mỗi item trả thêm `distance_km` (haversine, 1 chữ số thập phân). Không kèm `search` thì sắp xếp gần→xa; có `search` giữ thứ hạng relevance (BM25 + exact-name), `distance_km` chỉ là metadata. Tọa độ ngoài WGS84 trả 400.
  - `province=...`: lọc tỉnh tường minh — chấp nhận mã GSO (`79`), tên mới (`hồ chí minh`) hoặc tên cũ (`sài gòn`, `hà tây`); resolve qua `geo.resolve_province`. POI đã gán `province_code` khớp theo polygon; POI ngoài mọi polygon (`province_code IS NULL`) khớp theo nhãn `province` text cũ. Tên không resolve được coi như nhãn text.
  - Tên tỉnh cũ (trước sáp nhập 2025, vd `sài gòn`, `bình dương`, `hà tây`) trong `search` tự mở rộng OR sang tên mới theo `data/province_mergers_2025.json`.
  - **Geo-scope theo polygon (round C):** query chứa tên tỉnh — mới (`phở hồ chí minh`) hoặc cũ qua alias (`quán ăn bắc kạn` → Thái Nguyên; cụm con ≥2 token như `vũng tàu` ⊆ `bà rịa–vũng tàu` → Hồ Chí Minh; ưu tiên cụm dài nhất, 1 token đơn như `tàu`/`phú` không resolve) — giới hạn kết quả theo `(province_code = mã GSO OR khớp text tên tỉnh)`. Token tỉnh bị rút khỏi match token còn lại (`phở`, `quán ăn`…), vẫn chạy trên tập đã scope. POI trong polygon vắng mặt text tỉnh vẫn được tìm thấy (đa số snapshot thiếu address/province); POI ngoài polygon nhưng chứa text tỉnh vẫn lọt qua nhánh OR. Query chỉ gồm tên tỉnh giữ hành vi text cũ.
- `GET /api/places/{id}/history`: nhật ký tối đa 200 sự kiện gần nhất.
- `GET /api/places/{id}/memories`: danh sách sự kiện/thông tin đã ghi (tối đa 500).
- `POST /api/places/{id}/memories`: thêm bản ghi `{ "kind":"fact", "content":"...", "source":"user", "confidence":0.8, "observed_at":"2026-10-11" }` (append-only).
- `GET /api/places/{id}/media`: danh sách tham chiếu ảnh/tài liệu (tối đa 500).
- `POST /api/places/{id}/media`: thêm `{ "url":"https://...", "rights_basis":"own photo", "source":"user", "caption":"..." }`. Chỉ lưu liên kết và metadata, không tải ảnh.
- `GET /api/export.geojson?limit=20000`: snapshot đến 20.000 đối tượng; `total_matching` cho biết tổng trước cắt. Nhận cùng bộ lọc với `/api/places` (`search`, `province`, `bbox`, `category`, `near`); khi có `near`, properties mỗi feature kèm `distance_km`. Nếu nhiều dữ liệu, phân trang `/api/places` hoặc export trực tiếp từ database.
- `GET /api/export.csv`: xuất tối đa 20.000 đối tượng hiện hành và BOM UTF-8 cho Excel. Ô văn bản bắt đầu bằng `=`,`+`,`-`,`@`, tab, CR được chèn `'` ở đầu để chống formula injection khi mở bằng spreadsheet.
- `POST /api/import`: request body GeoJSON FeatureCollection (chỉ Point). Tối đa 10.000 features hoặc 16 MiB/call. Trả `created,updated,rejected,errors`. Features có `properties.id` hoặc `feature.id` sẽ được dùng như stable identity; nếu thiếu, dùng `(source,source_id)` khi có, nếu thiếu nữa sẽ sinh UUID mới, dễ trùng khi import lại.
- `GET /api/provinces`: tên 34 đơn vị cấp tỉnh, kèm `boundary_available` (true khi polygon ranh giới nạp được từ `data/provinces/provinces_34.geojson`, false khi file thiếu/hỏng).
- `GET /api/health`: kiểm tra database.
- `GET /api/live`: SSE stream read-only (\`text/event-stream\`) phát `ready` và `changed` với JSON `{\"revision\":<changes.seq>}`. Browser `EventSource` tự reconnect bằng `Last-Event-ID`; phát lại invalidation khi có commit. Chỉ báo cập nhật POI, không phải ảnh vệ tinh trực tiếp. Tối đa 32 luồng đồng thời trên một tiến trình. Xem [Realtime Map](REALTIME_MAP.md).
- `GET /api/stats`: số địa điểm, sự kiện lịch sử và tỉnh có dữ liệu.

### Test API trên Windows PowerShell

```powershell
$body = @{ name='Điểm thử'; lat=21.0285; lon=105.8542; province='Hà Nội'; source='manual'; source_id='demo-01' } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://localhost:8765/api/places' -Method POST -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod 'http://localhost:8765/api/stats'
```

### Giới hạn công nghệ

- SQLite FTS5 hỗ trợ tìm kiếm tiếng Việt không dấu bằng cột chuẩn hóa; từ tìm kiếm dùng prefix match. Khi FTS5 không khả dụng, dùng LIKE.
- Dữ liệu địa điểm có lưu `source`, `source_id`, nhưng chưa có bảng quyền media đa phiên bản. Xem schema PostGIS mẫu nếu cần kho lớn.
- Bộ nhớ chi tiết là các bản ghi thêm mới, không có luồng xóa/đính chính độc lập. Khi xóa địa điểm, memory/media gắn với địa điểm bị xóa theo quan hệ cascade; nhật ký chỉnh sửa địa điểm vẫn giữ.
- Không tích hợp user authentication/authorization hoàn chỉnh hoặc rate-limit; không public máy chủ như vậy.
