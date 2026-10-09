# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

Đây là kết quả của hai drill mới chạy từ đầu đến cuối bằng Ubuntu/WSL, bare
mode, localhost. Trường `iso` dùng UTC; outage drill 2 là 10:00:20 ngày
09/10/2026 theo giờ Việt Nam/Asia Bangkok (UTC+7).
Log cũ được giữ nguyên và nối thêm log mới; các số dòng dưới đây trỏ đến lần
chạy mới nhất. Các đường dẫn tính từ thư mục gốc repository.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T02:59:30` UTC | chaos kill | `chaos/chaos-events.jsonl:4` |
| Request fail đầu tiên | `+1.993s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:50` |
| Request thành công sau đó | không có trong cửa sổ 40s | không có dòng `ok:true` nào sau t_outage | `reports/measure-latest-drill-1.json` |
| RTO | `NO_RECOVERY`; 15 request lỗi | `tools/measure_rto.py` | `reports/measure-latest-drill-1.json` |

Baseline không chạy health checker/failover; hai cảnh báo thiếu sự kiện này
trong kết quả đo baseline phù hợp với thiết kế diễn tập.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | `+0.000s` | `action:kill` | `chaos/chaos-events.jsonl:6` |
| User thấy lỗi đầu tiên | `+0.359s` | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:184` |
| Health check phát hiện | `+14.841s` | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:5` |
| Runbook ghi thông báo | `+16.959s` | bước 2; dùng --auto trong drill | `reports/runbook-run.jsonl:9` |
| Snapshot restore xong | `+17.086s` | `step:2_restore_snapshot` | `reports/failover-events.jsonl:31` |
| Region phụ ready | `+23.246s` | `step:4_wait_ready` | `reports/failover-events.jsonl:33` |
| DNS cutover | `+23.253s` | `step:5_dns_cutover` | `reports/failover-events.jsonl:34` |
| **RTO đo được** | `+28.577s` ≈ `28.6s` | dòng `ok:true` đầu sau lỗi, served_by=b | `reports/drill-2-withdr.jsonl:198` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.6s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `4.0s` / 2 doc | 300s (5 phút) | PASS |

Drill 2 `valid=true`, `warnings=[]`; region bị cô lập a, region phục hồi b.
Kết quả máy đọc được: `reports/measure-latest-drill-2.json`.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor / detection thực đo | ngân sách `15.0s`, thực đo `14.841141s` | `interval_s × threshold` và timestamp trong `reports/health-events.jsonl:5` | Hạ interval hoặc threshold, đánh giá false alert |
| Xác nhận outage + verify target | `2.144130s` | t_verify − t_detect, `reports/failover-events.jsonl:30` và `reports/health-events.jsonl:5` | Giảm thời gian xác nhận và round-trip; vẫn giữ xác nhận khi vận hành |
| Snapshot restore | `0.101068s` | t_restore − t_verify, `reports/failover-events.jsonl:31` và `reports/failover-events.jsonl:30` | Tối ưu storage/copy; snapshot thường xuyên hơn chủ yếu giảm RPO |
| GPU pool warm-up + scale/cutover overhead | `6.166732s` | t_cutover − t_restore; `waited_s=6.15` tại `reports/failover-events.jsonl:33`, cutover tại `reports/failover-events.jsonl:34` | Giữ pool đã warm-up ở standby |
| DNS/LB TTL cache + nhịp request | `5.324293s` | t_recovered − t_cutover, `reports/drill-2-withdr.jsonl:198` và `reports/failover-events.jsonl:34` | Giảm TTL và tăng tần suất lấy mẫu khi đo |

Các khoảng nối tiếp ở bảng cộng thành `28.577365s` theo timestamp chưa làm
tròn, tức `28.6s` khi làm tròn một chữ số. Cấu hình `5.0s × 3 = 15.0s`
là ngân sách detection theo rubric.
Không cộng thêm 15s vào detection đã đo. Sai khác nhỏ và độ trễ phát hiện phụ
thuộc pha polling, timeout và lịch chạy của hệ điều hành.

RPO `4.0s / 2 doc` là khoảng dữ liệu primary có nhưng snapshot khôi phục
không có tại thời điểm restore; ingest vẫn chạy khi tiến trình serving A bị
SIGSTOP. Đây không phải chỉ số mất dữ liệu chốt đúng tại thời điểm outage.
`reports/failover-events.jsonl:31` còn ghi phiên bản embedding
`embed-model=vi-e5-base@v3` để truy vết tính tương thích của snapshot.

10 request trực tiếp B: error rate 0%, p95 44.8ms,
evidence `reports/runbook-run.jsonl:13`. Recovery qua edge được chứng minh
riêng bằng request thành công ở dòng 198 của log traffic.
