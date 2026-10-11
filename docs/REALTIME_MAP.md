# Vietnam Atlas — Live map, near-real-time imagery, and low-latency delivery

Cập nhật 2026-10-11. Đánh giá trên repo `main` với Leaflet 1.9.4, stdlib HTTP, SQLite WAL, tối đa 500 marker/viewport, lớp satellite opt-in được merge qua PR #2.

## Khái niệm phải phân biệt

1. **Bản đồ phản hồi thời gian thực**: thao tác kéo/zoom/đổi lớp diễn ra ngay trong trình duyệt; tile nền tải từ CDN. Ảnh nền đó không nhất thiết mới chụp.
2. **Địa điểm cập nhật tức thời**: thêm/sửa/xóa POI hoặc cập nhật từ cơ sở dữ liệu, trình duyệt khác thấy khi nhận sự kiện live.
3. **Ảnh vệ tinh mới chụp gần thời gian thực**: phụ thuộc vệ tinh có đi qua vùng cần xem, thời tiết, quyền truy cập, thời gian xử lý/phát hành. Không có nguồn toàn quốc miễn phí đảm bảo nhìn rõ mái nhà 24/7 và cập nhật liên tục.

NASA Worldview/GIBS có nhiều cảnh quan sát có thể xuất hiện trong vài giờ, nhưng độ phân giải (chẳng hạn khoảng 250m đối với một số lớp MODIS/VIIRS) không thể nhìn rõ nhà. Mặt khác lớp satellite phục vụ rõ nhà của MapTiler không phải luồng trực tiếp từ vệ tinh. Nếu cần cả ảnh rõ nhà lẫn độ mới cao, cần hợp đồng nhà cung cấp có quyền redisplay, SLA, vùng phủ và giới hạn trễ; không bảo đảm luôn đủ dữ liệu.

Tham khảo:
- https://nasa-gibs.github.io/gibs-api-docs/access-basics/
- https://github.com/nasa-gibs/worldview
- https://docs.planet.com/data/imagery/skysat/
- https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events

## Chức năng đã triển khai trong nhánh live SSE

- `GET /api/live`: luồng SSE read-only; trường `id` là **revision** tăng dần từ bảng `changes.seq`, payload chỉ có revision (không bao gồm địa điểm/ảnh riêng tư).
- Stream gửi `ready` khi kết nối, `changed` sau commit, heartbeat mỗi 15 giây và tự reconnect sau tối đa 120 giây. EventSource dùng `Last-Event-ID` cho lần kết nối lại; đồng bộ luôn qua REST. Nếu EventSource bị đóng hẳn (ví dụ `503` khi đủ 32 stream), client tự mở kết nối mới sau 5 giây thay vì im lặng.
- `notify()` sau transaction tạo/sửa/xóa hoặc nhập GeoJSON hoàn tất. Không phát trước khi commit.
- Thay đổi từ lệnh CLI / tiến trình thứ hai trên cùng SQLite phát hiện qua kiểm tra revision mỗi 1 giây.
- Giới hạn 32 stream đồng thời **mỗi tiến trình**; nếu quá hạn `503`. Đây là thiết kế nhỏ cho local/nhóm test, không phải gateway nhiều ngàn người.
- Frontend chỉ `refresh()` của viewport + `refreshStats()`, bỏ request cũ bằng AbortController, gộp các sự kiện gần nhau (120 ms), giữ marker có ID trùng và chỉ thay đổi toạ độ/popup khi cần, gộp sự kiện di chuyển bản đồ (90 ms).
- EventSource không dùng GPU, không cần WebSocket framework, Redis hoặc Celery.

## Luồng hoạt động

```text
User A POST/PUT/DELETE (API)
  → SQLite WAL commit
  → LiveChanges.notify()
  → SSE "changed", revision=N
  → Browsers B/C (EventSource)
  → Debounce 120ms → GET /api/places?bbox=... + /api/stats
  → Diff current markers (id, lat/lon, popup)
  → Repaint only affected objects
```

### Khi không có SSE

Các REST endpoint vẫn hoạt động. Trình duyệt cũ không hỗ trợ EventSource chuyển sang polling 6 giây; với browsers có EventSource nhưng mất mạng, EventSource tự reconnect. Reverse proxy phải tắt response buffering với event-stream, HTTP keepalive hợp lý, hỗ trợ stream vô thời hạn hoặc reconnect sạch.

## Mục tiêu SLO để đo, KHÔNG phải số đo hiện tại

| Metric | Chỉ tiêu đề xuất |
|---|---|
| Interaction delay (pan/zoom) | Render gần 60 FPS trên desktop thông dụng |
| Satellite tile first visible | 1–2 giây nếu CDN/net tốt; đo p75 và p95 thực |
| API `/api/places` p95, viewport thông thường | dưới 200 ms sau khi chuẩn dữ liệu + index |
| Place commit-to-other-client render, 2 browsers local | dưới 500 ms p95 (đo network/paint) |
| Cross-process CLI update | 1–2 giây qua DB check |
| Client data consistency | no stale overwrite after zoom/search, reconnect recovers changes |
| 32 live clients | bounded, graceful 503 |

## Mở cho người dùng Internet

**Không public localhost server ngay**. Các bước bắt buộc:
1. Triển khai HTTPS reverse proxy/CDN cho web asset; tên miền ổn định, gzip/br, cache fingerprinted asset (JS/CSS), cache tile chỉ nếu được nhà cung cấp cho phép.
2. AuthN/AuthZ người sửa/nhập/xóa (session/token), public read tối thiểu và rate-limit cho API; moderation/anti-spam của cộng đồng, request limits.
3. SSE có reverse-proxy `proxy_buffering off`, `Cache-Control: no-cache`, idle timeout > heartbeat (15 s), per-user connection quotas.
4. Đo TTFB, LCP, p95 request, thời gian event-to-paint trên mạng di động thật; giới hạn concurrent clients/memory/FD.
5. Khi vượt single-process: dùng gateway SSE chuyên dụng và shared pub/sub (Redis Streams/NATS/PostgreSQL NOTIFY chỉ nếu đã có hạ tầng) để chia nhiều replicas; SQLite local không phải kho phân tán.
6. Dữ liệu POI hàng triệu bản ghi: tránh render mọi DOM marker, dùng viewport cluster/vector tile (MVT) + geospatial indexing và paging; không tăng giới hạn 500 bằng cách tải toàn bộ 34 tỉnh.
7. Ảnh vệ tinh mới: catalog STAC có `acquired_at`, `published_at`, GSD thật, cloud cover, license, độ trễ; nút "Cập nhật gần nhất" phải thể hiện thời điểm chụp thật từ metadata, không lấy `Date.now()` để giả định.

## Hạn chế của implementation hiện tại

- 32 SSE streams/process là giới hạn chủ ý và có thread/SQLite poll mỗi client/giây; không scale tới hàng ngàn clients.
- Trình duyệt giới hạn khoảng 6 kết nối HTTP/1.x mỗi origin: nhiều tab trong cùng một trình duyệt có thể khiến request REST xếp hàng sau các SSE đang mở (tối đa 120 giây). Giai đoạn public dùng HTTP/2 multiplexing qua TLS hoặc chia sẻ một kết nối live giữa các tab; hiện chấp nhận cho localhost/nhóm thử nghiệm.
- Events thông báo thay đổi **địa điểm**, không phát ảnh vệ tinh live. `/api/live` không chứa ảnh/capture timestamps.
- SSE không làm rõ ảnh: AI/GPU xử lý batch với ảnh được cấp phép, phát hành kết quả khi sẵn, không nằm trên critical path của map.
- `places`/SQLite chưa có phân quyền Internet; địa điểm người dùng thêm cần duyệt trước khi xuất bản nếu dùng công khai.
- UI vẫn giới hạn 500 bản ghi theo viewport; lượng POI lớn phải xử lý clustering/tile server/aggregation ở giai đoạn khác.

## Release gates

- Unit/integration tests xác minh SSE sau commit, không phát dữ liệu PII, event id tăng, reconnect/capacity, đường cũ vẫn hoạt động.
- Benchmark 1, 10 và 32 client trên Windows/Docker, lỗi 503 không làm API chính chậm.
- Trên mạng thật đo thời gian click → commit → browser B cập nhật; so sánh before/after về FPS, HTTP requests/zoom.
- CI Python matrix, Ruff, JS syntax, Docker, CodeQL đều xanh; browser E2E thực trên Windows còn phải xác minh trước triển khai public (gồm kịch bản ≥6 tab cùng trình duyệt để đo REST refresh không xếp hàng sau SSE trên HTTP/1.x).
