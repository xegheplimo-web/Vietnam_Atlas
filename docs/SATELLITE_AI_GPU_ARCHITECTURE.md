# Vietnam Atlas — Satellite Imagery + AI/GPU Architecture

Ngày thiết kế: 2026-10-11. Trạng thái: **satellite viewer = implemented on this branch**, **AI/GPU pipeline = architecture only, NOT deployed/trained**.

## 0. Decision

Mục tiêu có 2 sản phẩm độc lập:
1. **Bản đồ vệ tinh để xem rõ nhà/cửa hàng**: hiển thị ảnh raster độ phân giải cao từ nhà cung cấp được cấp phép, có gói truy cập miễn phí phi thương mại theo hạn mức. Không cần GPU.
2. **Phân tích ảnh bằng AI/GPU**: nhập cảnh ảnh mà dự án có quyền sao chép/xử lý, chuẩn hoá dữ liệu địa lý, suy luận công trình/thay đổi và lưu kết quả có nguồn gốc kiểm chứng. Cần imagery chất lượng cao gốc; không suy ra mái nhà thật từ Sentinel-2 10 m.

Không dùng ảnh nền Google/Esri/MapTiler làm dữ liệu huấn luyện, bulk download, server-side tile cache hoặc offline nếu giấy phép tương ứng không cho phép. Free-to-view ≠ open data ≠ redistribution rights.

## 1. Repo recon (2026-10-11)

- Browser: HTML/CSS/JS + Leaflet 1.9.4, OSM/CARTO/OpenTopoMap baselayers.
- HTTP API: Python stdlib, cùng origin, không có OAuth/RBAC.
- DB: SQLite WAL (places, changes, memory_records, place_media), tìm kiếm bbox/FTS5.
- Chưa có imagery catalog, raster tiles server, background job queue, 3D hoặc công cụ GPU.
- Giấy phép repository: PolyForm Noncommercial; nguồn ảnh có giấy phép độc lập.
- Ưu tiên **không thay Leaflet/SQLite** chỉ để thêm vệ tinh.

## 2. Viewer satellite implemented

Lớp nút "Vệ tinh" nằm cạnh 3 lớp nền hiện tại; mặc định disabled nếu chưa cấu hình.

### MapTiler Cloud (sử dụng thử nghiệm/phi thương mại theo hạn mức)

Windows PowerShell, tại thư mục repo:

```powershell
$env:ATLAS_MAPTILER_KEY = "YOUR_ORIGIN_RESTRICTED_BROWSER_KEY"
py -m server.app
```

Mở `http://localhost:8765`, chọn "Vệ tinh". `GET /api/map-config` thông báo cấu hình cho trình duyệt. Key được **công khai trong network request của browser**, không phải secret backend; cấu hình Allowed HTTP Origin tại MapTiler. Khi triển khai Internet cần bổ sung origin tên miền production. Không commit key vào Git.

Docker: thiết lập biến trên host trước khi `docker compose up --build -d`, hoặc tạo `.env` (đã gitignored):

```dotenv
ATLAS_MAPTILER_KEY=YOUR_ORIGIN_RESTRICTED_BROWSER_KEY
```

### XYZ self-hosted hoặc ảnh đã mua quyền

```dotenv
ATLAS_SATELLITE_TILES_URL=https://your-licensed-tile-host/tiles/{z}/{x}/{y}.png
ATLAS_SATELLITE_ATTRIBUTION=Imagery copyright holder, year, terms
```

- Ưu tiên cấu hình custom nếu có cả custom và MapTiler.
- Chỉ HTTPS hoặc HTTP trên localhost, đủ biến `{z}/{x}/{y}` và phải có attribution.
- Custom endpoint phải là **XYZ Web Mercator 256 px**; MapTiler dùng 512 px cùng zoom offset theo tài liệu Leaflet.
- Custom URL được chuyển về browser; **không có backend tile proxy** và **không auto-download**.
- Browser truy vấn tile từ nguồn cung cấp, chịu terms/quota/caching của nguồn. Không thể hứa nguồn dữ liệu phủ đủ Việt Nam hoặc cập nhật theo ngày.
- Nếu cấu hình bị thiếu/hỏng, các lớp OSM, sáng, địa hình và toàn bộ CRUD vẫn hoạt động.
- Attribution MapTiler dạng chữ + logo bắt buộc trên bản đồ khi dùng bản Free.

Tài liệu dịch vụ: https://docs.maptiler.com/cloud/api/tiles/ ; https://docs.maptiler.com/leaflet/examples/raster-tiles-in-leaflet-js/ ; https://www.maptiler.com/terms/cloud/ .

## 3. Target AI + GPU data flow (not yet implemented)

```text
LEGAL SOURCE REGISTRY
  ├─ Live basemap provider: only display, never feed to AI/cache unless rights allow
  └─ Licensed open/owned scenes: STAC, GeoTIFF/COG, drone orthomosaic
       ↓  ingest + integrity + provenance + license + acquisition date
INGEST / CATALOG WORKER (CPU/IO)
       ↓  cloud / nodata / CRS / GSD / bounds checks
RASTER PROCESSING (CPU/GPU optional)
       ↓  COG, orthorectification where metadata supports, overviews, ROI tiles
OPTIONAL GPU INFERENCE (off by default)
       ├─ segmentation/building outline: on sufficiently fine original imagery
       ├─ change detection: co-registered scenes, resolution/date matched
       └─ enhancement/super-resolution: visualization ONLY, NOT ground truth
       ↓  geometries + per-feature confidence + model info + review status
HUMAN REVIEW / QA
       ↓
VECTOR FEATURES / CHANGE PROPOSALS (GeoJSON)
       ↓  verified import path + provenance + dedup check
EXISTING VIETNAM ATLAS PLACES SQLITE + HISTORY
       ↓
LEAFLET OVERLAYS + ORIGINAL RASTER XYZ/COG TILE SERVER
```

### Separate worker deployment

- `web + server` vẫn chạy CPU và không import PyTorch, GDAL hoặc nvidia libs mặc định.
- Một **GPU worker OPTIONAL** trong môi trường conda/venv hoặc Docker GPU dùng NVIDIA RTX 3090 24GB, CUDA-compatible torch version, GPU health checks. Không phát tán GPU workload sang request thread của web server.
- Queue ban đầu: durable SQLite-backed jobs/process worker hoặc file-manifest queue có lock; **chỉ chọn 1 loại sau benchmark**, không triển khai cả Redis/Celery/Kafka ngay. API HTTP localhost + auth cho jobs; idempotency theo `source_id + source_version + pipeline_version`.
- CPU pipeline: GDAL/rasterio, rio-cogeo/COG, STAC catalog; sinh tiles bằng rio-tiler/TiTiler hoặc pre-generated MBTiles/XYZ tuỳ ROI, không ép đổi backend chính.
- GPU pipeline tùy bài toán: PyTorch + segmentation model có license hợp lệ, Torch inference, ONNX Runtime nếu benchmark nhanh hơn; Real-ESRGAN là **optional preview enhancement**.
- Tránh để model tự viết vào bảng `places`. AI chỉ sinh `proposals`, người dùng xác minh trước. Không suy luận mã số doanh nghiệp từ mái nhà.

## 4. Required scene metadata (contract; design proposal)

```json
{
  "scene_id": "provider:dataset:id:version",
  "provider": "licensed_source",
  "asset_uri": "local-or-permitted-COG-uri",
  "checksum_sha256": "...",
  "acquired_at": "2026-01-01T00:00:00Z",
  "ingested_at": "2026-10-11T00:00:00Z",
  "license_id": "exact-license-and-version",
  "rights": {
    "display": true,
    "store": true,
    "derivatives": true,
    "redistribute": false,
    "train": false
  },
  "source_gsd_m": 0.3,
  "crs": "EPSG:32648",
  "bounds_wgs84": [105.0, 20.0, 105.1, 20.1],
  "cloud_fraction": 0.02,
  "processing_version": "raster-pipeline-v1"
}
```

Rights defaults **DENY** if missing. Raster derived assets must inherit source license and provenance, expiry/retention if relevant. Persist original GSD and acquisition date alongside output; never label AI enlarged pixels as better optical GSD.

## 5. Inference contract, quality & safety

Output proposed change/feature must contain:
- `scene_id`, `model_id`, `model_version`, `processing_version`, geometry CRS, source GSD and capture time.
- Class/confidence and review state: `unverified`, `verified`, `rejected`.
- Optional `enhanced=true` with a prominent **AI-generated visualization** label; never infer legal boundaries or business identity from enhanced appearance.
- Geometry precision benchmark against manually labeled **Vietnam-specific** scenes.
- Reject imagery with cloud/occlusion too high; verify orthorectification, no-data, date alignment, and model domain shift (urban/rural/season).
- Change detection needs at least 2 rights-cleared scenes; avoid false positives caused by season, shadow, roof color, viewing angle.
- For building footprints, aim at roughly sub-meter original source imagery. Sentinel-2 at 10 m cannot reliably resolve individual houses; GPU upscaling cannot reconstruct missing observations.
- Disallow rendering "verified" AI output before reviewer acceptance.

## 6. Recommended implementation stages

| Stage | Deliverable | GPU | Current state |
|---|---|---|---|
| A | Opt-in licensed satellite XYZ base layer + attribution + validation | No | Implemented in this branch |
| B | Quality test on 3 Vietnam neighborhoods + MapTiler quota/legal validation | No | Manual verification pending |
| C | Licensed source registry / STAC / COG ROI ingest + rights gate | No | Architecture |
| D | Local raster serving / tiled overlay on Leaflet for right-cleared imagery | No | Architecture |
| E | GPU worker + building/roof inference + review queue | Yes | Architecture |
| F | Aligned time-series change detection + metrics, release gates | Optional | Architecture |

## 7. Acceptance gates

A: When key absent, satellite control disabled and no tiles are requested. When valid key exists, switch preserves marker/viewport; attribution/logo shown. Invalid key/429/403 gives recoverable warning; no uncontrolled retry. Docker and Windows tested.
B: Compare at least 3 urban/rural sites in Vietnam for actual building visibility and provider recency/license. Do not claim 2026 captures without date metadata.
C: End-to-end audit metadata/checksums/rights; deny import on unknown license; no Google/Esri/MapTiler tile harvesting.
D: Raster renderer processes a bounded licensed test ROI; no global bulk prefetch; benchmark p95 map latency, cache hit ratio, storage.
E/F: Hand-labelled evaluation across different regions and resolutions; report precision/recall/IoU, confusion and false-positive rate. Human review required, fallback to CPU/no-AI; define VRAM cap and worker restart behavior.

## 8. Architecture constraints

- Project software license (PolyForm Noncommercial) **does not** expand rights in satellite imagery.
- All external data sources have their own provenance and usage limits.
- Current server has neither account-based auth nor safe remote-job endpoint. Do **not** expose inference endpoints publicly until TLS, auth, queue quotas, validation, rate limit and audit log are implemented.
- Ignore 3D/photogrammetry unless product requirements justify it. RTX 3090 capability makes GPU feasible but is not itself a reason to add dependencies.
- Build on the existing place-store; avoid a second POI store and avoid unverified merging.
