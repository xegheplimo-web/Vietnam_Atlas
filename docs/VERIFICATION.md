# Biên bản kiểm chứng

- API/SQLite chạy thực tế trong test qua local HTTP và database tạm.
- Test CRUD, lưu nhật ký, FTS không dấu, lọc bbox, lỗi tọa độ, nhập GeoJSON, CSV, Origin restriction.
- Kiểm tra cú pháp JavaScript bằng `node --check` nếu Node.js có trong môi trường build.
- Kiểm tra cú pháp tất cả file Python bằng `compileall`.
- Kiểm tra JSON danh mục 34 tỉnh và ánh xạ 63 → 34 (không trùng).
- Kiểm tra file ZIP sau tạo bằng zipfile integrity test.

**Chưa kiểm chứng end-to-end:** render trình duyệt trên máy Windows của người dùng; tải 314MB PBF thực tế; import PBF với pyosmium; xây tile server offline; tích hợp dự án `F:\enterprise-memory-platform`; Docker image build (đòi Docker Engine). Đây là các hạng mục tùy môi trường, phải chạy thử trên máy đích trước khi production.
