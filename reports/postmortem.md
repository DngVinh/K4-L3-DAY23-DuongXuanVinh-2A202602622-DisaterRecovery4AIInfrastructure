# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

Các ISO time dưới đây là UTC theo log; cộng 7 giờ để đổi sang giờ Việt Nam.
Timeline và số liệu dùng lần diễn tập mới nhất từ đầu đến cuối bằng Ubuntu/WSL.
Log giữ lịch sử; các số dòng dưới đây đã tính phần log cũ được bảo toàn.

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T03:00:20.806 | outage bắt đầu (chaos kill netblock) | `chaos/chaos-events.jsonl:6` |
| 2026-10-09T03:00:21.164 | user đầu tiên bị ảnh hưởng (request fail tại +0.359s) | `reports/drill-2-withdr.jsonl:184` |
| 2026-10-09T03:00:35.647 | health check alert (UNHEALTHY tại +14.841s) | `reports/health-events.jsonl:5` |
| 2026-10-09T03:00:37.765 | runbook ghi thông báo tại +16.959s; drill dùng --auto | `reports/runbook-run.jsonl:9` |
| 2026-10-09T03:00:49.383 | resolved (request đầu tiên OK từ B tại +28.577s) | `reports/drill-2-withdr.jsonl:198` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.6s` · gap: `-271.4s` (đạt chỉ tiêu sớm hơn 271.4 giây)
- RPO mục tiêu lab: 300s · đo được: `4.0s` (`2` doc chưa có trong restore) · gap: `-296.0s`
- **Bước tốn nhiều giây nhất:** detection thực đo `14.841s`, chiếm ~51.9% RTO. Ngân sách cấu hình là `5s × 3 = 15s`; threshold 3 lần thất bại liên tiếp hạn chế false alert.

Restore riêng chỉ mất 0.101s; khoảng 2.144s giữa alert và verify target là
thời gian xác nhận/HTTP, không phải thời gian copy snapshot. Golden signals
có 10 request trực tiếp vào B, p95 44.8ms, error rate 0%
(`reports/runbook-run.jsonl:13`); recovery qua edge được chứng minh riêng
bằng `reports/drill-2-withdr.jsonl:198`. RPO được đo tại restore khi ingest vẫn
chạy, nên không diễn giải 2 doc thành lượng mất dữ liệu chốt tại outage.

## 3. Root cause (5 whys)

Nếu đây là outage thật, bước nào trong runbook của tôi sẽ có nguy cơ thất bại?
- **Why 1**: Region B không thể tức thì gánh tải khi Region A bị cô lập mạng (netblock).
  -> Do Region B chạy ở chế độ warm-standby (vector count=0, chưa có model weights trên đĩa).
- **Why 2**: Hệ thống bắt buộc phải chờ tải snapshot vector DB và weights từ replica storage.
  -> Do kiến trúc cross-region replication hoạt động bất đồng bộ theo chu kỳ 30s thay vì active-active synchronous replication.
- **Why 3**: Sau khi nạp weights, Region B vẫn chưa thể nhận traffic ngay mà phải đợi 6.15s.
  -> Do GPU pool cần thời gian khởi tạo context và warm-up weights vào VRAM (`WARMUP_SECONDS=6`) khi chuyển từ warm sang full.
- **Why 4**: Traffic người dùng vẫn mất thêm ~5s lỗi sau khi Region B đã ready.
  -> Do TTL cache của DNS proxy / Edge Router (`EDGE_TTL_SECONDS=5s`) vẫn trỏ về Region A cho đến khi hết hạn cache.
- **Why 5**: Runbook cần 15s mới bắt đầu quy trình khôi phục.
  -> Do thiết kế phát hiện outage có độ trễ sàn (`interval=5s × threshold=3 = 15s`) để phân biệt sự cố đứt gãy hoàn toàn với các dao động mạng tạm thời (chống flapping 2 chiều).

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Thử CDC/replication thường xuyên hơn, đo docs_lost và overhead | Data Platform Team | 2026-10-25 | Mục tiêu RPO < 2s; phải đo lại, chưa cam kết mức giảm |
| 2 | Duy trì GPU pre-warmed standby ở Region B (weights nạp sẵn trong VRAM) | ML Infra Team | 2026-11-01 | Giảm RTO 6.0s |
| 3 | Chuyển đổi DNS routing sang Anycast Global Load Balancer với dynamic health probing | Network SRE | 2026-11-15 | Giảm RTO ~4.0s (loại bỏ DNS TTL delay) |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Giá trị: `5.0s × 3 = 15.0s`.
   - Ngân sách cấu hình chiếm `15.0s / 28.6s ≈ 52.4%`; detection thực đo chiếm khoảng 51.9% tổng RTO.
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Ngân sách phát hiện danh nghĩa giảm từ 15s xuống 3s (12 giây). Đây là dự báo cấu hình, chưa phải kết quả đo; timeout và lịch polling có thể làm mức giảm thực tế khác đi.
   - Cái giá phải trả: Rủi ro flapping tăng vọt. Chỉ một đợt nghẽn mạng ngắn, GC pause hoặc packet drop trong 3 giây sẽ kích hoạt cảnh báo giả, dẫn đến tự động cutover nhầm sang region phụ. Việc này làm đứt gãy kết nối hàng loạt client đang phục vụ và tốn kém tài nguyên scale pool không cần thiết.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - `docs_lost` (2 documents tại restore trong drill này) đại diện cho các bản ghi mới trên Region A chưa có trong DB khôi phục ở B.
   - Nếu Region A mất vĩnh viễn và không có journal/nguồn ingest khác, 2 tài liệu này có thể không khôi phục được, làm thiếu thông tin cho RAG. Cần rà soát nguồn dữ liệu và ingest lại.

## 6. Baseline và các câu hỏi phản tư của GUIDE

- Trước containment, edge chỉ thấy lỗi/timeout từ A; chưa có thành phần tự
  phát hiện và thực hiện failover. B có count=0, weights=false, pool=warm;
  đổi routing sang B ngay sẽ trả region_not_ready.
- Giữ GPU standby đã warm-up có thể giảm phần chờ 6s mà không hạ threshold;
  đổi lại là chi phí compute/GPU thường trực.
- Health checker chạy tiến trình riêng và không import serving. Khi serving A
  bị SIGSTOP, checker vẫn hoạt động nếu host/OS giám sát còn sống.
- Bằng chứng cho cam kết RTO nằm trong raw traffic/chaos/health/failover logs
  và `reports/measure-latest-drill-2.json`, không dựa vào quan sát bằng mắt.
- Drill mô phỏng serving mất kết nối; chưa mô phỏng mất đĩa, GPU thật hay tải
  production. Sau cửa sổ đo đã SIGCONT A và dừng các tiến trình của lần chạy.
