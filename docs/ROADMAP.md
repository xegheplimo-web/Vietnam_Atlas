# Lộ trình phát triển (định hướng)

Cập nhật: 2026-10-11. Trạng thái hiện tại: v2.0 full-starter, repo Public, giấy phép PolyForm Noncommercial 1.0.0.

## Nguyên tắc

- Ưu tiên hoàn thiện lõi bản đồ/dữ liệu trước khi thêm miền mới (3D, AI).
- Module nặng (GPU/ML) chỉ triển khai như dịch vụ tùy chọn tách rời; không đưa PyTorch/dependency 3D vào máy chủ bản đồ chính.
- Giữ tách bạch giấy phép: mã nguồn dự án PolyForm Noncommercial; dữ liệu và thư viện bên thứ ba theo giấy phép gốc (OSM ODbL, Leaflet BSD, ...).

## Quyết định đã chốt

**Không tích hợp Map-Anything-v1 (tái dựng 3D từ ảnh/video) ở giai đoạn hiện tại.** Đánh giá 2026-10-11: MapAnything giải bài toán hình học 3D (point cloud, depth map, surface normals) — không bổ sung đường sá, POI hay dữ liệu hành chính cho Vietnam Atlas; tích hợp ngay chỉ tăng kích thước và chi phí bảo trì. Giấy phép đã kiểm chứng: mã nguồn MapAnything Apache-2.0; checkpoint `facebook/map-anything-v1` trên Hugging Face CC-BY-NC-4.0 — hai giấy phép phải giữ tách biệt nếu dùng sau này.

## Thứ tự ưu tiên

1. Ổn định lõi hiện tại (server stdlib + SQLite WAL + Leaflet + CI đầy đủ) — đang chạy.
2. Hoàn thiện polygon hành chính 34 tỉnh/thành (và cấp xã nếu khả thi) từ nguồn chính thống; không suy diễn địa giới từ điểm điều hướng/bbox.
3. Nhập đầy đủ POI OSM (node/way/relation) + chống trùng theo `(source, source_id)`; giữ provenance.
4. Tối ưu tìm kiếm tiếng Việt không dấu, tile server/offline, quy trình cập nhật dữ liệu định kỳ.
5. (Mở rộng — chỉ khi có use case thật) Dịch vụ GPU tùy chọn dựng 3D từ ảnh/video của cửa hàng/phố/công trình.

## Điều kiện xem xét lại Map-Anything

- Có yêu cầu thực tế: người dùng tải ảnh/video → xem mô hình 3D.
- Benchmark trên RTX 3090 24GB (VRAM, tốc độ, chất lượng) đạt mức dùng được.
- Triển khai tách dịch vụ riêng hoặc module optional — không nhét vào server chính.

## Chưa kiểm chứng

- Chưa benchmark Map-Anything trên phần cứng thực tế (mới đánh giá tài liệu + giấy phép).
- Polygon hành chính 34 tỉnh: chưa chọn nguồn dữ liệu chính thống (cần khảo sát: ranh giới công bố chính thức, OSM relations, đối chiếu NQ 202/2025).
