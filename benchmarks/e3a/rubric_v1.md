# E3a Rubric v1 — grading & error taxonomy (frozen)

Nguồn chân lý: `contracts/E3A_CONTRACT.md` §6. File này materialize rubric để judge
(người, có LLM hỗ trợ) chấm nhất quán. Scorer (`tools/benchmark_e3a.py`) chỉ tự gán
các nhãn **suy được tự động** (§3.2); mọi nhãn cần phán đoán phải do judge ghi.

## 1. Thang điểm 0–3

Xét **ràng buộc bắt buộc trước**: tên thực thể · loại POI · địa bàn · mốc thời gian ·
tọa độ/bán kính · trạng thái hoạt động. Sau đó mới xét mức độ phù hợp.

| Grade | Tên | Nghĩa | Ví dụ (query → POI chấm) |
|---|---|---|---|
| 3 | exact_relevant | Đúng thực thể / đáp ứng mọi ràng buộc bắt buộc + đủ ý định | `Bệnh viện Chợ Rẫy` → đúng bản ghi BV Chợ Rẫy; `phở gần chợ Bến Thành` → quán phở trong bán kính |
| 2 | relevant | Đủ ràng buộc bắt buộc, kém lý tưởng ở yếu tố ưu tiên (xa hơn, tên hơi lệch nhưng đúng thực thể/đúng loại đúng địa bàn) | `bệnh viện đa khoa hải phòng` → BV đa khoa khác trong HP; G5: POI đúng loại, trong bán kính nhưng không phải cái gần nhất |
| 1 | partially_relevant | Vi phạm ≥1 ràng buộc bắt buộc **hoặc** chưa chứng minh đủ phù hợp | G3 đúng loại sai phường bắt buộc; POI cùng tên khác hệ thống; G5 trong bán kính ± sai số chồng lấn chưa xác minh |
| 0 | irrelevant | Không liên quan | `phở hà nội` → cửa hàng quần áo; POI đã đóng cửa khi query yêu cầu đang mở |

**grade ≥ 2 = relevant** (dùng cho Hit@10, MRR, Recall, IDCG).

### Special rules theo nhóm

- **G1** (tên POI cụ thể): chỉ **cùng thực thể** mới ≥ 2. Trùng tên khác nơi = ≤1.
- **G3** (loại POI + địa danh): sai tỉnh/phường **bắt buộc** → tối đa 1.
- **G4** (địa danh cũ/mới): nối sai quan hệ hành chính (vd gán POI tỉnh cũ sang tỉnh mới không kế thừa) → tối đa 1.
- **G5** (near/bán kính): POI ngoài `radius_km` (tính từ WGS84 gốc, không dùng `distance_km` đã làm tròn) → tối đa 1. POI sát ngưỡng trong vùng chồng lấn sai số (`accuracy_m`) → xác minh thêm, **không ép grade**.
- **G6** (trùng tên/mơ hồ): resolver trả `ambiguous` khi đúng bản chất mơ hồ = hành vi ĐÚNG, không trừ điểm.
- **G7** (không có kết quả phù hợp): chỉ **zero results** mới PASS (`correct_no_match`); ≥1 kết quả = `false_positive`.
- **Thiếu evidence → `unjudged`, KHÔNG ép về 0** (contract §6.1). Scorer không tự hạ.

## 2. primary_outcome (đúng 1 giá trị/query/adapter)

Thứ tự kiểm tra (contract §6.2): validate topic/evidence → run error → coverage audit
→ qrels/top-10 → outcome. Không nhầm `retrieval_failure` thành `coverage_gap`.

| outcome | Điều kiện |
|---|---|
| `invalid_query` | Topic vi phạm schema (thiếu id/group/query, G5 thiếu `near` hợp lệ) |
| `run_error` | Timeout 2 s, HTTP error, exception — gồm cả timeout duy nhất 1 lần |
| `coverage_gap` | Query positive, audit TOÀN DB chứng minh thiếu POI (`coverage_audit=missing`, verified) |
| `unjudged` | Còn thiếu judgment để kết luận (top-10 có item chưa chấm, audit chưa xong) |
| `hit` | ≥1 item top-10 có grade ≥ 2 (verified) |
| `retrieval_failure` | Đã chấm đủ, không item top-10 nào ≥ 2 |
| `correct_no_match` | G7 trả 0 kết quả |
| `false_positive` | G7 trả ≥1 kết quả |

## 3. error_labels — 6 nhóm, 37 nhãn (đếm 5+7+6+9+5+5)

`severity`: `info` · `minor` · `major` · `critical`.
**critical** = khẳng định chắc chắn sai địa danh/vị trí có bằng chứng rõ, hoặc dẫn
người dùng tới cơ sở không phù hợp (vd chỉ đường tới bệnh viện sai tỉnh).

### 3.1 Nhãn judge gán (cần phán đoán — scorer KHÔNG tự sáng tác)

| Nhóm | Nhãn | Khi nào | Ví dụ |
|---|---|---|---|
| ground_truth | `evidence_missing` | Query/qrel không có nguồn chứng minh | Tên POI chỉ biết qua Atlas |
| ground_truth | `evidence_stale` | Nguồn cũ, thực thể đổi tên/đóng cửa/dời | Web 2019 giới thiệu chi nhánh đã đóng |
| ground_truth | `label_conflict` | Hai nguồn/judge mâu thuẫn nhau | Cổng tỉnh ghi "đã sáp nhập", web BV vẫn hoạt động |
| ground_truth | `insufficient_judgments` | Pool chưa đủ để kết luận recall | Top-10 còn item chưa chấm |
| ground_truth | `ambiguous_intent` | Câu hỏi thật sự mơ hồ (G6 đúng bản chất) | `chợ đầu mối` không kèm tỉnh |
| data | `poi_missing_in_db` | Audit chứng minh POI phù hợp không có trong Atlas | Trạm y tế xã X có trên cổng tỉnh, DB không có |
| data | `gazetteer_gap` | Đơn vị hành chính thiếu/sai trong gazetteer | Phường mới sáp nhập chưa nạp |
| data | `historical_alias_missing` | Thiếu alias tên cũ→mới | "Bến xe Miền Đông cũ" không map |
| data | `incorrect_coordinates` | Tọa độ POI sai có bằng chứng | Marker lệch 3 km so GPS đo tại chỗ |
| data | `incorrect_category_data` | Category OSM sai/không đủ | Bệnh viện ghi `amenity=clinic` nhưng là BV đa khoa |
| data | `stale_poi` | POI đã đóng/đổi nhưng DB còn `unverified/verified` | Siêu thị đã phá dỡ |
| data | `duplicate_entity` | Hai bản ghi cùng thực thể | 2 row "Chợ Bến Thành" |
| admin | `admin_unresolved` | Resolver không resolve được toponym có thật | `phường Phú Thọ Hòa` không ra |
| admin | `admin_ambiguous` | Resolver trả ambiguous khi thực tế đủ thông tin | `quận 1` rõ nghĩa vẫn ambiguous |
| admin | `admin_wrong` | Resolve sang đơn vị sai | `thị trấn Chợ Mới` → xã Chợ Mới khác tỉnh |
| admin | `historical_mapping_wrong` | Chuỗi cũ→mới gán sai successor | Bình Dương cũ → sai tỉnh kế thừa |
| admin | `wrong_province` | Kết quả/lọc sang tỉnh khác | Query `phở nam định` trả POI Ninh Bình |
| admin | `wrong_ward` | Sai phường/xã bắt buộc | `phường 3` → `phường 4` |
| text_ranking | `normalization_failure` | Sai do khử dấu/NFC/NFD | `đà nẵng` không ra `Đà Nẵng` |
| text_ranking | `typo_handling_failure` | Lỗi gõ trong phạm vi chính tả hợp lý không cứu được | `khoaa` (5 ký tự, DL≤1) vẫn miss |
| text_ranking | `alias_match_failure` | Viết tắt phổ biến không expand | `bv chọ rẫy` không ra Chợ Rẫy |
| text_ranking | `category_match_failure` | Token loại POI không khớp category | `trạm xăng` vs `fuel` |
| text_ranking | `wrong_entity` | Trả thực thể khác cùng tên | `Khách sạn Sông Hồng` ở HN thay vì HP |
| text_ranking | `candidate_generation_failure` | FTS/LIKE không sinh ra ứng viên đúng | Token dài quá 8 bị cắt mất match |
| text_ranking | `relevance_ranking_failure` | Ứng viên đúng có nhưng xếp sai thứ tự | Exact name xếp dưới kết quả loãng |
| text_ranking | `false_positive_no_match` | G7 trả kết quả | Query nghĩa vô nghĩa vẫn ra POI |
| spatial | `incorrect_distance` | `distance_km` báo sai so haversine WGS84 | Lệch > 0.2 km so tính lại |
| spatial | `incorrect_distance_order` | Thứ tự kết quả không theo khoảng cách (mode distance-first) | POI 5 km xếp trên POI 1 km |
| spatial | `radius_filter_unsupported` | Engine không hỗ trợ lọc bán kính (ghi nhận, không phải lỗi engine) | API không có tham số radius |
| spatial | `spatial_evidence_uncertain` | Evidence tọa độ thiếu tin cậy/độ chính xác | accuracy_m lớn hơn radius_km |
| runtime | `non_deterministic_results` | 5 repeat trả top-10 khác nhau | Repeat 3 đảo thứ tự |

### 3.2 Nhãn scorer TỰ gán (suy được từ run/qrels — deterministic)

| Nhãn | Điều kiện tự gán |
|---|---|
| `query_timeout` | 1 request của query vượt timeout 2 s |
| `api_error` | HTTP status ≠ 200 / lỗi transport |
| `sqlite_error` | Adapter in-process raise `sqlite3.Error` |
| `non_deterministic_results` | `ids_by_repeat` không đồng nhất giữa các repeat |
| `rank_below_10` | Qrels có grade ≥ 2 verified nhưng không xuất hiện top-10 |
| `false_positive_no_match` | G7 trả ≥ 1 kết quả |
| `outside_radius` | G5: ≥1 POI top-10 có tọa độ ngoài `radius_km + accuracy_m/1000` |
| `incorrect_distance` | G5: `distance_km` báo lệch haversine > 0.2 km |
| `latency_budget_exceeded` | Adapter p95 > 200 ms (đánh dấu query vượt ngân sách) |

Severity mặc định của nhãn tự gán: `query_timeout`/`api_error`/`sqlite_error` = major;
`non_deterministic_results` = major; `rank_below_10` = minor; `false_positive_no_match` = minor;
`outside_radius`/`incorrect_distance` = info (lỗi scoring do oracle xử lý);
`latency_budget_exceeded` = minor. Judge có thể nâng severity khi có bằng chứng.

## 4. Ví dụ chấm điểm tổng hợp

1. Query G4 `bệnh viện Bắc Giang cũ` → top-1 là BV thuộc Bắc Ninh mới (đúng kế thừa) = **3**;
   POI ghi tỉnh Bắc Giang đã hợp nhất nhưng chain gán sai successor = **1** + `historical_mapping_wrong`.
2. Query G5 `atm gần chợ Đà Lạt` (radius 1 km, accuracy 50 m): ATM cách 0.8 km = **2–3**;
   ATM cách 1.3 km = **≤1** + `outside_radius`; `distance_km` báo 0.4 km = `incorrect_distance`.
3. Query G7 `zzz quán bánh phở sài gòn 8888` → trả 3 POI = `false_positive` + `false_positive_no_match` (scorer tự gán).
4. Query G1 `Nhà thuốc ABC` → top-3 là "Nhà thuốc ABC" chi nhánh khác thành phố = tối đa **1** (không cùng thực thể).
5. Top-10 chứa 2 item chưa có judgment verified → outcome `unjudged` (không hạ 0), nhãn `insufficient_judgments` khi cần.

## 5. Công thức scorer (nhắc lại contract §6.4)

```
Hit@10(q)  = 1[∃d ∈ Top10(q): grade(q,d) ≥ 2]
MRR@10     = (1/|Q|) Σ 1/rank_q           # không có relevant trong top-10 → rr=0
DCG@10     = Σ_{i=1..10} (2^grade_i − 1) / log2(i+1)     # exponential gain
nDCG@10    = DCG / IDCG                    # IDCG từ qrels grade sắp giảm dần
Recall@10  = |Top10 ∩ Relevant| / |Relevant|             # Relevant = qrels grade ≥ 2 verified
```

- Mẫu số metric headline = **positive queries đã judge đủ** (G7 tách riêng).
- `no_match_false_positive` = tỉ lệ G7 trả ≥ 1 kết quả.
- `coverage_gap_rate` = positive queries audit `missing` / **280** (mọi positive, kể cả
  `unjudged`, nằm trong mẫu số). Audit chưa đủ → chỉ xuất `provisional_coverage_rate`
  (mẫu số = positive đã audit) — xem B3 trong `docs/audits/2026-10-19-gpt-review-2.md`.
- qrels chưa đủ → query báo `judgment_incomplete`, KHÔNG âm thầm coi unjudged = 0.
