# Vietnam Atlas 2026 — Bản đồ Việt Nam và bộ nhớ địa lý

[![CI](https://github.com/xegheplimo-web/Vietnam_Atlas/actions/workflows/ci.yml/badge.svg)](https://github.com/xegheplimo-web/Vietnam_Atlas/actions/workflows/ci.yml)
[![CodeQL](https://github.com/xegheplimo-web/Vietnam_Atlas/actions/workflows/codeql.yml/badge.svg)](https://github.com/xegheplimo-web/Vietnam_Atlas/actions/workflows/codeql.yml)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Bộ mã nguồn mở rộng tự chạy trên Windows hoặc Docker: **bản đồ đẹp + đường sá OpenStreetMap + địa điểm lưu bền vững trong SQLite + tìm kiếm tiếng Việt không dấu + API + dữ liệu 34 tỉnh/thành + công cụ tải dữ liệu OSM toàn quốc**.

> **Phạm vi thực tế:** đây là ứng dụng đầy đủ chức năng cơ bản và bộ pipeline dữ liệu đầu vào, không phải bản sao cơ sở dữ liệu toàn bộ mọi tuyến đường/cửa hàng ở Việt Nam. Các lớp đường hiển thị khi trực tuyến lấy từ OpenStreetMap. Để có bản dữ liệu đường gốc toàn quốc, chạy trình tải Geofabrik (~314 MB ở thời điểm kiểm tra 10/10/2026). Cần quá trình import/render riêng để dựng bản đồ offline toàn quốc. Không nhúng ảnh Google Maps hoặc dữ liệu hàng loạt chưa xác minh quyền vào ZIP.

## Giấy phép: công khai mã nguồn, không cấp quyền sử dụng thương mại

**Dự án Public nhưng là source-available, không phải open source theo định nghĩa OSI.**
Phần mềm thuộc quyền tác giả của dự án trong các bản phân phối theo LICENSE hiện
sử dụng **[PolyForm Noncommercial 1.0.0](LICENSE)**. Bạn có thể đọc, nghiên cứu,
chỉnh sửa và chia sẻ cho những mục đích phi thương mại được giấy phép cho phép.

**Không được mặc nhiên khai thác thương mại**: bán mã, kinh doanh dịch vụ dựa
trên mã, cung cấp SaaS/API thu phí, tích hợp vào sản phẩm thương mại, hoặc triển khai
nhằm phục vụ hoạt động kinh doanh có tính thương mại nếu không có quyền cấp phép riêng
bằng văn bản. Xem [giải thích và yêu cầu sử dụng thương mại](COMMERCIAL_USE.md).

**Quan trọng:** Phiên bản đã phát hành trước đây theo MIT vẫn có quyền tương ứng
với giấy phép MIT được cấp hợp lệ khi đó; chuyển giấy phép không thu hồi quyền hồi tố.
Dữ liệu OpenStreetMap (ODbL), Leaflet và tài sản bên thứ ba có giấy phép riêng,
không bị áp đặt lệnh cấm thương mại từ giấy phép mã nguồn dự án.
Xem [nguồn, ghi công và phạm vi giấy phép](docs/LICENSING.md).

## 1. Mở ngay trên Windows 11

1. Giải nén ZIP, ví dụ vào `F:\Vietnam_Atlas_Full_2026`.
2. Cài Python 3.10+ nếu chưa có.
3. Nhấp đúp **`START_WINDOWS.bat`**. Không cần cài gói Python cho chức năng cốt lõi.
4. Mở Chrome ở **http://localhost:8765**.
5. Nhấn `＋ Thêm điểm` rồi bấm vào bản đồ để lưu địa điểm.

**Không dùng** `py -m http.server ... --directory web`: chế độ đó chỉ phục vụ file tĩnh và không có bộ nhớ SQLite/API.

### Hoặc Docker Desktop

Nhấp đúp `START_DOCKER_WINDOWS.bat`, hoặc:

```powershell
docker compose up --build -d
```

Sau đó mở http://localhost:8765. Docker mặc định chỉ mở cổng trên máy cục bộ `127.0.0.1`. Dữ liệu SQLite được giữ trong Docker volume `atlas_storage` và **khác dữ liệu của bản chạy Python Windows**; không chạy cùng lúc hai bản trên cùng cổng.

## 2. Chức năng chạy được

| Hạng mục | Trạng thái | Ghi chú |
|---|---|---|
| Bản đồ toàn Việt Nam, đường phố | Hoạt động qua Internet | Tiles OSM, có nền sáng, địa hình |
| Bộ nhớ địa điểm | Hoạt động | SQLite ở máy chủ; khác localStorage |
| Bộ nhớ thông tin chi tiết | Hoạt động | Sự kiện, nội dung, nguồn, độ tin cậy, thời điểm |
| Ảnh/tài liệu theo quyền | Hoạt động | Lưu liên kết HTTPS, nguồn và căn cứ quyền; không sao chép ảnh |
| Thêm / sửa / xóa / mở hồ sơ | Hoạt động | Mọi lần thay đổi lưu nhật ký |
| Tìm kiếm không dấu | Hoạt động | SQLite FTS5 khi khả dụng; dự phòng LIKE |
| Tìm theo vùng bản đồ / tỉnh | Hoạt động | Viewport bbox; giới hạn 500 điểm/lần trên UI |
| Xuất GeoJSON / CSV | Hoạt động | Mỗi yêu cầu tối đa 20.000 điểm |
| Nhập GeoJSON | Hoạt động | 10.000 đối tượng / lần và <=16 MiB; chỉ Point |
| Nhập CSV khối lượng lớn | CLI | Có script import theo lô và nguồn ID |
| Lịch sử thay đổi | Hoạt động | Nhật ký create / update / delete |
| Sao lưu SQLite | CLI | Sao lưu online an toàn và integrity_check |
| Danh mục 34 tỉnh/thành | Có | Tên; chưa gồm polygon địa giới pháp lý |
| Bảng chuyển 63 tỉnh cũ → 34 mới | Có | Kèm script dry-run, backup và thực thi |
| Tải PBF OSM Việt Nam | Có script | Tải riêng, cần Internet |
| Nhập tên POI nút OSM từ PBF | CLI tùy chọn | Cần `osmium`, không bao gồm POI dạng way/relation |
| Trích xuất đường OSM vùng chọn | CLI tùy chọn | Cần `osmium`, xuất GeoJSON xem trước |
| Nạp overlay đường GeoJSON lên bản đồ | Hoạt động | Giới hạn 25 MB / 20.000 features |
| Offline tile server toàn quốc | Chưa có | Cần dựng renderer/vector tile server riêng |
| Dữ liệu kinh doanh/ảnh Google Maps hàng loạt | Không nhúng | Cần chứng minh quyền sử dụng dữ liệu |
| Ranh giới mới cấp xã/phường chính xác | Chưa có | Phải nạp mã và polygon từ nguồn có thẩm quyền |

## 3. Tải dữ liệu đường sá và đối tượng Việt Nam

Tại thư mục giải nén:

```powershell
py tools/download_vietnam_osm.py
```

Tệp được tải thành `data/vietnam-latest.osm.pbf`, chứa các đối tượng OSM của Việt Nam: đường, nút, quan hệ... theo phạm vi của Geofabrik tại thời điểm tải. Tải PBF từ **Geofabrik**, không download hàng loạt tile của OpenStreetMap.

Muốn nhập địa điểm OSM kiểu nút có tên vào SQLite (bước tùy chọn):

```powershell
py -m pip install osmium
py tools/import_osm_poi.py data/vietnam-latest.osm.pbf --limit 5000
```

Bỏ `--limit 5000` để nạp tất cả POI *node* được bộ lọc hỗ trợ. Tên tỉnh của các điểm OSM **không tự suy ra từ tên tỉnh cũ**; cần chính xác polygon của 34 tỉnh mới và phép spatial join. Dữ liệu có thể lớn, nên backup trước và đo tốc độ trên một mẫu nhỏ.

Muốn trích xuất đường ở một vùng **làm overlay xem trước**:

```powershell
py tools/extract_roads.py data/vietnam-latest.osm.pbf --bbox 105.7,20.9,106.1,21.15 --limit 10000 --out data/roads-preview.geojson
```

Sau đó trên web chọn **Nạp lớp đường GeoJSON** và chọn file vừa tạo. Các tuyến đường do nền OSM thể hiện sẵn; overlay cho phép kiểm tra trực quan những đường được trích xuất. Trích xuất là theo vùng bbox ước lượng, không cắt hình học tại biên.

**Lưu ý:** OSM là dữ liệu bản đồ cộng đồng, không cam kết 100% mọi đường trên thực địa, đặc biệt đường mới hoặc đường ngõ chưa được đóng góp. Bản đồ giao diện yêu cầu Internet. Offline thật cần bộ render tile riêng từ PBF; xem `docs/OFFLINE_ROADS.md`.

## 4. Nhập CSV và chuyển tỉnh cũ → mới

Nhập dữ liệu từ CSV đã được cấp phép, xem `data/sample_import.csv`:

```powershell
py tools/import_csv.py data/sample_import.csv
```

Mẫu CSV có các cột `name,lat,lon` bắt buộc; các trường khác tùy chọn: `category,address,province,phone,website,notes,source,source_id,status`.

Kiểm tra tỉnh cũ (chỉ xem, không thay đổi):

```powershell
py tools/migrate_old_provinces.py
```

Sau khi kiểm tra và đồng ý, chạy:

```powershell
py tools/migrate_old_provinces.py --apply
```

Công cụ **tự sao lưu SQLite** trước khi thay đổi `province`, giữ `address` gốc, ghi lịch sử từng hồ sơ. Tên mới phản ánh đơn vị cấp tỉnh sau Nghị quyết 202/2025/QH15, vận hành từ 01/07/2025. Không tự gán địa giới theo tọa độ vì chưa có polygon chính thức.

## 5. Sao lưu dữ liệu

```powershell
py tools/backup_database.py
```

Bản chạy Python: cơ sở dữ liệu chính `data/atlas.sqlite3`, bản sao lưu `backups/atlas-...sqlite3`.

Bản Docker: database nằm trong volume `atlas_storage`. Dùng `docker compose exec`/`docker cp` hoặc backup theo chính sách volume. **Không xóa Docker volume nếu còn dữ liệu quan trọng.**

## 6. API mở sẵn

API cùng origin với giao diện. Một số endpoint:

- `GET /api/health`
- `GET /api/stats`
- `GET /api/provinces`
- `GET /api/places?search=ha%20noi&province=H%C3%A0%20N%E1%BB%99i&bbox=105,20,106,22&limit=100&offset=0`
- `GET /api/places/{id}`
- `GET /api/places/{id}/history`
- `POST /api/places` — JSON object
- `PUT /api/places/{id}` — JSON object, đầy đủ các trường của bản ghi
- `DELETE /api/places/{id}`
- `POST /api/import` — GeoJSON FeatureCollection
- `GET /api/export.geojson?limit=20000`
- `GET /api/export.csv`

Xem `docs/API.md`.

## 7. Giới hạn bảo mật và tích hợp

- Mặc định chạy **localhost**. Không mở trực tiếp API ra Internet. Nếu cần đa người dùng, phải thêm HTTPS, tài khoản/quyền truy cập, bảo vệ ghi dữ liệu, giới hạn tốc độ, monitoring và backup.
- `ATLAS_API_TOKEN` là tùy chọn cho môi trường API headless; nếu bật, giao diện mặc định chưa có luồng đăng nhập Bearer token, do đó việc ghi từ UI sẽ bị từ chối. Dùng reverse proxy xác thực trước khi cho người dùng từ xa truy cập.
- Đây là ứng dụng độc lập, **chưa cài hoặc chỉnh sửa** dự án `F:\enterprise-memory-platform`. Nếu cần tích hợp vào hệ thống đang có, kiểm tra API và schema hiện hữu trước khi chuyển dữ liệu, tránh tạo một kho trùng.
- Không lưu khóa API, dữ liệu cá nhân nhạy cảm, hoặc ảnh/địa điểm không có quyền. Không coi danh bạ địa điểm là cơ sở dữ liệu pháp lý doanh nghiệp.
- Tài nguyên OSM phải được ghi công và tuân thủ ODbL. Đọc `docs/LICENSING.md` và `docs/ARCHITECTURE.md`.

## 8. Tự kiểm thử

```powershell
py -m unittest discover -s tests -v
```

Test kiểm tra REST API thực qua HTTP, SQLite lưu dữ liệu và lịch sử, GeoJSON, lọc bbox, tìm kiếm không dấu, CSV, kiểm soát Origin, validation và danh mục tỉnh.

### CI tự động (GitHub Actions)

Mỗi push / pull request chạy tự động qua `.github/workflows/ci.yml`:

- **Test**: 8 integration tests trên ma trận Python 3.10–3.14 (Ubuntu) và Windows (3.12); báo cáo coverage trên Ubuntu/Python 3.12.
- **Lint & static checks**: `ruff check` + `ruff format --check`, cú pháp Python (`compileall`), JavaScript (`node --check`), hợp lệ dữ liệu JSON.
- **Docker**: build image và smoke test endpoint `/api/health` trong container.

Quét bảo mật bằng CodeQL định kỳ (`.github/workflows/codeql.yml`). Dependabot tự đề xuất cập nhật GitHub Actions và base image Docker hằng tuần.

## Nguồn tham khảo

- Geofabrik VN: https://download.geofabrik.de/asia/vietnam.html
- OSM copyright: https://www.openstreetmap.org/copyright
- OSM tile policy: https://operations.osmfoundation.org/policies/tiles/
- Nghị quyết 202/2025/QH15: https://baochinhphu.vn/nghi-quyet-cua-quoc-hoi-ve-sap-xep-don-vi-hanh-chinh-cap-tinh-102250612191145158.htm
