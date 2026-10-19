# GPT review — 19/10/2026 (thread "Phân tích dự án tiếp theo")

- **Thread**: `chatgpt.com/c/6ad135bf-ab0c-83ec-91cd-ae8cb2d3033d`
- **Input gửi đi**: brief §A/§B ngày 19/10 (facts recon + 4 phương án) + audit `docs/audits/2026-10-19-project-audit.md` @ `84b4ece`
- **Verdict**: **GO triển khai · NO-GO phát hành v1** — chọn phương án **C+** (C + V1-05 Ops chạy song song)
- **Coding agent**: Devin CLI only (Hermes = Lead Orchestrator, Git integration, independent verification)

## 1. Quyết định đã chốt

| Hạng mục | Quyết định của kiến trúc sư |
|---|---|
| C1 — Đóng nợ | PASS. Sửa golden (đúng 1 total + provenance), `/api/stats`, bảo vệ `private_holdout/`; **giữ nguyên corpus master**; chưa cần khóa version phát hành |
| C2 — Harness E3a | PASS. Devin CLI dựng runner + schema + 37 error labels + fixtures; **phải chứng minh scorer bằng synthetic fixtures trước khi chấm dữ liệu thật**; Hermes nghiệm thu độc lập; không sửa ranking |
| C3 — Pilot 100 | PASS có điều kiện. **Chỉ lấy từ 200 dev queries**, tuyệt đối không dùng blind holdout; kết quả gọi là `pilot_diagnostic`, không phải `baseline_300` |
| C4 — Freeze 300 | PASS. Thu 200 câu còn lại → khóa 200 dev/100 blind → audit coverage → ký manifest **một lần** → baseline 2 adapter |
| C5 — Sửa | PASS có điều chỉnh. Baseline trước, sửa sau; **một vòng cải thiện** nếu ngân sách hạn chế; ưu tiên **precision & abstention**; blind holdout chạy **một lần** |
| V1-05 | Chạy **song song** từ đầu: backup→restore→smoke, Windows/Docker, migration rollback, docs, license, security local-only; tag/release sau khi mọi gate PASS |

## 2. Phân bổ pilot 100 câu (chốt)

| Nhóm | Pilot | Tổng cuối |
|---|---|---|
| G1 — Tên POI | 20 | 60 |
| G2 — Không dấu/lỗi gõ | 16 | 50 |
| G3 — Danh mục + địa danh | 23 | 70 |
| G4 — Hành chính cũ/mới | 15 | 45 |
| G5 — Near | 12 | 35 |
| G6 — Mơ hồ | 7 | 20 |
| G7 — Negative | 7 | 20 |
| **Tổng** | **100** | **300** |

## 3. Nguyên tắc sửa search (C5) — "Precision & Abstention v1"

| Ưu tiên | Sửa đổi | Điều kiện |
|---|---|---|
| P0 | Nhận biết token chỉ loại POI (quán ăn, bệnh viện, khách sạn…) | Token loại hình không được thoả mãn chỉ bằng địa chỉ |
| P0 | Dùng bằng chứng có dấu ở name/category để phân biệt phở/phố, quán/quận | Vẫn hỗ trợ câu không dấu |
| P0 | Quy tắc từ chối kết quả không đủ bằng chứng (abstention) | Đo trade-off trên development set |
| P1 | Giảm ảnh hưởng token địa chỉ khi ranking | Không làm mất truy vấn tên riêng hợp lệ |

- **Không** xóa `phường/quận/xã/phố` khỏi mọi query (có thể là thành phần địa danh thật).
- **Không** làm fuzzy edit-distance toàn cục và **không** làm polygon cấp phường trong slice này (tăng false positive, vượt ngân sách).
- Sau `baseline_300`, nếu precision đạt nhưng G2/G3 đa số là retrieval failure → Hermes được quyền chuyển vòng sửa sang recall, **giữ nguyên gate nghiệm thu**.

## 4. qrels & no-match

- LLM pre-judge + human review **có chọn lọc** (không chấm tay 100% ~3.000 cặp):
  100% cho 300 query intent + gold entity · 100% candidate LLM gán grade 2–3 · 100% grade 1 sát ngưỡng hoặc bất đồng nguồn · 100% G7 negative · 100% kết luận coverage_gap · 100% ca critical/ambiguous/thiếu nguồn · **≥30% phân tầng** cho grade 0–1 còn lại (seed cố định, ghi danh sách mẫu).
- Thiếu bằng chứng → `unjudged`, **không** tự hạ thành grade 0 (làm sai nDCG/coverage).
- No-match: **ngưỡng hiệu chỉnh trên dev set** + tín hiệu deterministic (category đúng? tên chứa phần đặc trưng? địa danh xác định đáng tin? khớp từ name/category hay chỉ address/notes? mức phủ token? mâu thuẫn địa bàn/loại hình?) — **không** dùng một ngưỡng BM25 toàn cục; khóa cấu hình trước holdout.
- Công bố **hai chỉ số riêng**: tỷ lệ negative trả rỗng · tỷ lệ positive bị từ chối nhầm.

## 5. Ba quy tắc không được thay đổi

1. Không cập nhật `atlas.sqlite3`/`gazetteer.sqlite3` frozen master sau corpus lock (sửa code → run mới có SHA).
2. Không dùng kết quả blind holdout để chọn threshold/ranking/rubric; gate fail → chặn phát hành, cần benchmark version mới.
3. Không mở rộng v1 thành semantic search / crawler đa nguồn / bản đồ cấp phường hoàn chỉnh.

## 6. Điểm chỉnh vào audit (đã áp dụng)

| # | Ý kiến | Xử lý |
|---|---|---|
| 1 | Số liệu test lệch: 246 chạy, 1 fail, 1 skip ⇒ **244 pass** (không phải 245) | Đã sửa §1 của audit; xác nhận lại `Ran 246 · FAILED (failures=1, skipped=1)` |
| 2 | `provinces_with_places = 0` là bug thật; đổi sang đếm `province_code`, có test cho DB trộn NULL/empty và chưa backfill | Đưa vào C1 |
| 3 | Không diễn giải probe thành tỷ lệ chất lượng | Đã thêm ghi chú "cách đọc bằng chứng" đầu §6 |
| 4 | Nguyên nhân MRR −2,94 chưa cô lập (recency vs candidate pool) | Đã thêm ghi chú ablation ở P2-1; không chặn E3a |
| 5 | Mức sẵn sàng: Search RED · Freshness/Geography/Ops AMBER | Khớp §7 audit |

---

## 7. Nguyên văn phản hồi của kiến trúc sư

…
Xem thêm
Đã xử lý trong 3m 15s
ChatGPT đã nói:
Vietnam Atlas — Quyết định triển khai v1 sau audit 19/10/2026

Verdict: chọn phương án C, với một điều chỉnh: triển khai V1-05 Ops song song ngay từ đầu, không đợi đến cuối. Đây là lộ trình phù hợp nhất để hoàn thành v1 mà vẫn giữ được benchmark độc lập, không làm lại corpus và không mở rộng kiến trúc.

Tôi đã đọc audit ngày 19/10 và đối chiếu trực tiếp mã nguồn.

GitHub main = 84b4ece2efa8b143dbbf6af3606de5f00ec8d8ce.

CI và CodeQL trên chính commit này đều SUCCESS.

server/store.py xác nhận nguyên nhân sai /api/stats và những hạn chế của tìm kiếm FTS5 hiện tại.

E3a chưa có các artifact cần thiết để nghiệm thu chất lượng độc lập.

Trạng thái quyết định: GO triển khai, nhưng NO-GO phát hành v1 ở thời điểm hiện tại.

(a) Review C1–C5

Giai đoạn

	

Verdict

	

Điều chỉnh




C1 — Đóng nợ

	

PASS về kế hoạch

	

Sửa golden, stats, .gitignore; chưa cần khóa version phát hành




C2 — Harness

	

PASS

	

Phải chứng minh scorer đúng bằng synthetic fixtures trước khi chấm dữ liệu thật




C3 — Pilot 100

	

PASS có điều kiện

	

Chỉ lấy từ 200 development queries; tuyệt đối không sử dụng blind holdout




C4 — Hoàn thành 300

	

PASS

	

Khóa ground truth, qrels, corpus hash và ký manifest một lần




C5 — Baseline và tối ưu

	

PASS có điều chỉnh

	

Chạy baseline trước sửa; giới hạn một đợt cải tiến nếu ngân sách hạn chế; Ops song song

Điều kiện quan trọng nhất: 100 câu pilot không được trở thành cơ sở để điều chỉnh theo kết quả blind holdout. Pilot chỉ dùng để hiệu chuẩn công cụ đánh giá, chuẩn hóa rubric và tìm lỗi hệ thống trên tập development.

(b) Trả lời bốn câu hỏi kỹ thuật
1. Có chấp nhận pilot 100 câu trước khi đủ 300?

Có. Đây là phương án tôi khuyến nghị.

Dùng 100 câu thuộc 200 development queries, không lấy bất kỳ câu nào từ 100 blind holdout.

Phân bổ pilot:

Nhóm

	

Pilot

	

Tổng cuối




G1 — Tên POI

	

20

	

60




G2 — Không dấu/lỗi gõ

	

16

	

50




G3 — Danh mục + địa danh

	

23

	

70




G4 — Hành chính cũ/mới

	

15

	

45




G5 — Near

	

12

	

35




G6 — Mơ hồ

	

7

	

20




G7 — Negative

	

7

	

20




Tổng

	

100

	

300

Quy trình:

Hoàn thiện runner và kiểm tra scoring bằng synthetic fixtures.

Thu thập 100 câu thật từ nguồn độc lập, duyệt bằng chứng.

Chạy cả legacy và admin=1 trên corpus khóa.

Kiểm tra rubric, missing judgments, phân loại lỗi và độ ổn định của runner.

Nếu cần sửa semantics của scorer/rubric, phải version và tính lại toàn bộ kết quả pilot.

Thu thập 200 câu còn lại; khóa 300 queries và qrels, rồi tạo baseline chính thức.

Kết quả pilot được gọi là pilot_diagnostic, không phải baseline_300.

Cho phép dùng 100 câu đó để kiểm định dụng cụ, nhưng chúng là development data. Chỉ 100 holdout cuối cùng mới có vai trò nghiệm thu độc lập sau tối ưu.

2. Chỉ đủ một vòng sửa: ưu tiên precision hay recall?

Chốt precision trước, nhưng sửa theo ý định truy vấn, không xóa token hành chính hàng loạt.

Lý do từ code hiện tại:

quán và quận có thể thành cùng chuỗi quan sau chuẩn hóa.

phở và phố có thể thành pho.

Các trường name, category, address, province, notes được ghép vào search_text; FTS không phân biệt token xuất phát từ tên POI hay địa chỉ.

Candidate được lấy bằng AND-prefix, tạo ra match giả trước khi ranking.

Tôi ưu tiên một slice cải thiện có tên Precision & Abstention v1, gồm:

Ưu tiên

	

Sửa đổi

	

Điều kiện




P0

	

Nhận biết từ chỉ loại POI như quán ăn, bệnh viện, khách sạn

	

Không để token loại hình được thỏa mãn chỉ bằng địa chỉ




P0

	

Dùng bằng chứng có dấu ở tên/category để phân biệt các trường hợp phở/phố, quán/quận

	

Vẫn hỗ trợ câu hỏi không dấu




P0

	

Quy tắc từ chối kết quả không đủ bằng chứng

	

Đo trade-off trên development set




P1

	

Giảm ảnh hưởng của token địa chỉ khi ranking

	

Không làm mất truy vấn tên riêng hợp lệ

Không xóa phường, quận, xã, phố khỏi tất cả queries. Những từ này có thể là thành phần thiết yếu của địa danh hoặc tên thực thể.

Chưa làm fuzzy edit-distance toàn cục và chưa làm polygon phường trong slice này. Hai việc đó có phạm vi tác động rộng, dễ làm tăng false positives và vượt ngân sách.

Tuy nhiên, đây là thứ tự ưu tiên mặc định. Sau baseline_300, nếu precision đã đạt nhưng G2/G3 có retrieval failure chiếm phần lớn, Hermes có quyền chuyển một vòng sửa sang recall, miễn giữ nguyên gate nghiệm thu.

3. LLM pre-judge + human review 30% hay human 100%?

Chấp nhận LLM pre-judge + human review có chọn lọc. Không yêu cầu chấm thủ công 100% khoảng 3.000 cặp.

Nhưng không chỉ kiểm tra ngẫu nhiên 30% trên toàn bộ tập. Tôi chốt rubric review như sau:

Đối tượng

	

Human review




300 query intents và bằng chứng gold entity

	

100%




Candidate được LLM đề xuất grade 2–3

	

100%




Candidate grade 1 sát ngưỡng 2 hoặc bất đồng giữa các nguồn

	

100%




Toàn bộ G7 negative queries

	

100%




Mọi kết luận coverage_gap

	

100%




Candidate grade 0–1 còn lại

	

≥30%, lấy mẫu phân tầng




Ca critical, ambiguous hoặc thiếu nguồn

	

100%, không tự PASS

Dùng seed cố định và ghi danh sách sample để việc kiểm tra có thể tái lập.

Nếu phát hiện nhiều false-negative trong mẫu grade 0–1, mở rộng kiểm duyệt ở nhóm lỗi đó thay vì giữ cứng 30%.

Các cặp chưa đủ chứng cứ phải mang unjudged. Không được cho phép một mô hình tự biến nhãn chưa chắc chắn thành grade 0, vì điều đó trực tiếp làm sai nDCG và coverage.

Với một operator, đây là cách cân bằng hợp lý giữa công sức và độ tin cậy.

4. No-match: threshold tham số hay quy tắc cứng?

Chọn ngưỡng có thể hiệu chỉnh trên development set, kết hợp một số ràng buộc cứng về ngữ nghĩa.

Không sử dụng quy tắc tổng quát:

Còn dưới hai token đặc trưng khớp thì trả về 0.

Quy tắc ấy có thể làm hỏng các truy vấn hợp lệ một từ như phở, ATM, WinMart hoặc tên riêng ngắn.

Tôi đề nghị dùng các tín hiệu deterministic:

Category có đúng không?

Tên thực thể có chứa phần đặc trưng bắt buộc không?

Địa danh bắt buộc có được xác định đáng tin cậy không?

Phần khớp đến từ name/category hay chỉ từ address/notes?

Mức độ khớp có dấu/không dấu và mức bao phủ các token quan trọng.

Có bằng chứng mâu thuẫn về địa bàn hoặc loại hình không?

Đánh giá các ngưỡng ứng viên trên development queries; chọn một cấu hình cố định, ghi vào adapter config và khóa trước khi chạy holdout.

Không dùng BM25 thô như một threshold toàn cục duy nhất. Điểm BM25 có thể thay đổi theo câu hỏi, độ dài văn bản và nhánh SQL, nên không bảo đảm một mức điểm cố định tương ứng với cùng độ liên quan.

No-match phải công bố hai chỉ số riêng: tỷ lệ truy vấn negative trả về rỗng theo contract, và tỷ lệ positive bị từ chối nhầm. Không được nâng No-match Accuracy bằng cách chặn luôn các truy vấn hợp lệ.

(c) Những điểm trong §A cần chỉnh
1. Test count đang lệch số học

Bạn ghi:

246 tests → 245 pass, 1 fail, 1 skip

Tổng các trạng thái này là 247. Nếu runner báo Ran 246 tests với 1 fail và 1 skip thì số pass thực tế là 244.

Đây chỉ là sai số trình bày, nhưng nên sửa trong audit để giữ kỷ luật evidence.

2. provinces_with_places = 0 là bug thật

Đã xác nhận tại server/store.py:

Python
Chạy
province_count = con.execute(
    "SELECT COUNT(DISTINCT province) FROM places WHERE province != ''"
).fetchone()[0]

Theo audit, dữ liệu thực có province_code cho 97.212 POI, còn province text rỗng.

Cần đổi sang đếm distinct province_code hợp lệ, có regression tests cho DB trộn NULL/empty và trường hợp chưa backfill. Không cần ghi thêm province text vào 98.938 dòng chỉ để sửa một metric.

3. Không nên diễn giải các probe thành tỷ lệ chất lượng

Các trường hợp bệnh viện bạch maai, quán ăn hoàn kiếm, nhà hàng lỗ đen là bằng chứng tái lập được của lỗi cụ thể.

Nhưng 2/10 negative probes trả sai không đủ để kết luận No-match Accuracy của hệ thống là 80%. Tương tự, hai ví dụ typo không chứng minh phần lớn G2 sẽ thất bại.

Chỉ số thật phải lấy từ benchmark có phân bổ và ground truth đã khóa.

4. Nguyên nhân MRR giảm chưa được cô lập hoàn toàn

Việc updated_at DESC tham gia ranking đã được code xác nhận. Tuy nhiên, mức giảm MRR từ 0,914 xuống 0,8841 có thể do cả thay đổi candidate pool lẫn recency.

Cần thử ablation trên bản copy nếu muốn quy trách nhiệm chính xác. Đây không phải yêu cầu chặn E3a.

5. Mức sẵn sàng v1 cần cập nhật

Dựa trên audit, đánh giá chính xác là:

Trụ

	

Trạng thái




Search Quality

	

RED — chưa có independent benchmark




Data Freshness

	

AMBER — pipeline đã có, thiếu recovery drill




Geography

	

AMBER — dữ liệu hành chính có, chưa chứng minh hết truy vấn thực tế




Operations

	

AMBER — CI xanh nhưng local test đỏ, chưa có release drill

Không nên gọi 246 tests OK hoặc 4 trụ PASS trước khi sửa golden và hoàn tất đánh giá.

(d) Thứ tự cuối cùng để hoàn thành v1

Tôi không đề xuất một kiến trúc D mới. Chọn C+, tức phương án C với V1-05 thực hiện song song và một vòng cải thiện search được chọn theo bằng chứng.

C1 — Clean gates

Sửa đúng một total trong golden fixture kèm provenance, sửa /api/stats, bảo vệ private_holdout/, giữ corpus master không đổi. Tests local + CI phải xanh.

C2 — E3a Scorer

Devin CLI triển khai runner, schema, 37 error labels, fixtures và báo cáo. Hermes nghiệm thu độc lập, không sửa search ranking.

C3 — Pilot 100 development queries

Chấm đủ qrels và kiểm tra rubric. Sửa công cụ chấm nếu cần; không sửa ranking dựa trên vài probe chưa định lượng.

C4 — 300-query freeze

Thu thập 200 câu còn lại, khóa 200 dev/100 blind, audit coverage, ký manifest một lần rồi chạy baseline hai adapter.

C5 — Một vòng cải thiện search có kiểm soát

Chọn precision/abstention trước nếu baseline xác nhận. Đo before/after trên cùng frozen corpus. Chạy blind holdout một lần cho nghiệm thu.

V1-05 — Vận hành song song

Backup→restore→smoke, Windows/Docker, migration rollback, tài liệu, license, security local-only. Đóng gói, cập nhật BUILD_MANIFEST, tag/release sau khi tất cả gate bắt buộc PASS.

Ba quy tắc không được thay đổi

Một: Không cập nhật atlas.sqlite3 hoặc gazetteer.sqlite3 frozen master sau corpus lock. Có thể sửa code và tạo run mới với SHA rõ ràng mà không cần làm lại database.

Hai: Không dùng kết quả blind holdout để lựa chọn threshold, ranking hoặc sửa lại rubric. Nếu release gate thất bại, phát hành phải bị chặn và cần quy trình benchmark version mới trước khi thử nghiệm nghiệm thu tiếp.

Ba: Không thay đổi v1 thành dự án tìm kiếm semantic, crawler đa nguồn hay bản đồ cấp phường hoàn chỉnh. Các khả năng đó để sau release nếu dữ liệu benchmark chứng minh nhu cầu.

Chốt cho Hermes: triển khai C1 ngay, tiếp đến C2 và pilot 100; bắt đầu Ops drill song song khi scorer đang được xây dựng. Giữ Devin CLI là coding agent duy nhất, Hermes làm Lead Orchestrator, Git integration và independent verification. Mục tiêu là kết thúc bằng một release v1 có số đo đáng tin, chứ không phải một bộ test đẹp nhưng bỏ sót lỗi tìm kiếm thực tế.
