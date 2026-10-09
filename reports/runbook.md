# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

Chạy từ thư mục gốc repo trong Linux/WSL, stack bare đã được khởi động.
`netblock --mock` dùng SIGSTOP nên không chạy trực tiếp trên Python Windows.
Region B cần có snapshot sẵn trong replica storage trước sự cố. Không seed lại
hoặc chạy clean trên dữ liệu đang dùng. Mặc định phải xác nhận `y/N`; `--auto`
chỉ dùng cho drill/CI. Lệnh ở bước 2 tự làm các bước 3–7 đúng một lần; các lệnh
tiếp theo dùng để kiểm tra kết quả, không thực hiện lại restore hay cutover.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.alive=false` hoặc HTTP 503 3 lần liên tiếp, `reports/health-events.jsonl` có `UNHEALTHY` | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | Xác nhận y; dòng `thong_bao_incident` ghi vào `reports/runbook-run.jsonl`. IC tự mở kênh incident; script chỉ ghi tên kênh, không gửi thông báo | Incident Commander (IC) |
| 3 | Kiểm tra restore state | `curl -fsS http://127.0.0.1:8002/v1/state` | `count > 0`, `weights=true`; log `2_restore_snapshot` có RPO, docs_lost và embed_model_version | Data/Storage Engineer |
| 4 | Kiểm tra pool warm→full | `curl -fsS http://127.0.0.1:8002/readyz` | HTTP 200; state ở bước 3 có `pool_state=full`. Nếu timeout, automation dừng trước cutover | ML Platform Engineer |
| 5 | Kiểm tra DNS/LB cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | Active region b; log `5_dns_cutover` xuất hiện sau `4_wait_ready` thành công. Chờ TTL hết trước khi kết luận về traffic | Traffic / Network SRE |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -fsS http://127.0.0.1:8080/v1/infer; echo; done` | 10 request trả về `[b]`, không có HTTP error; xem p95 và error rate của 10 probe trực tiếp B ở `verify_golden_signals` trong log runbook | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `warnings=[]`, `rto_verdict=PASS` (RTO ≤ 300s); RPO/docs_lost đối chiếu với log restore | Incident Commander |

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
1. **Điều kiện kỹ thuật**:
   - Region A đã khôi phục hoàn toàn, endpoint `/readyz` phản hồi HTTP 200 liên tục trong ≥ 15 phút không flap.
   - Toàn bộ dữ liệu mới ingest tại Region B trong thời gian sự cố đã được đồng bộ 2 chiều (reverse sync) về Region A để tránh data loss.
   - Pool GPU của Region A đã warm-up xong ở trạng thái `full`.
2. **Quy tắc an toàn**: Tuyệt đối không auto-rollback nhằm tránh flapping vòng lặp giữa 2 region. Thực hiện chuyển traffic trong maintenance window hoặc giờ thấp tải.
3. **Thẩm quyền quyết định**: Incident Commander (IC) phối hợp cùng Lead AI Architect phê duyệt manual 1-click trigger.

Nếu B không ready hoặc golden signals có lỗi, giữ incident mở và báo IC;
không tự trả traffic về A khi A chưa đáp ứng các điều kiện trên. Reverse sync
chưa được triển khai trong lab, nên failback cần quy trình đồng bộ riêng đã
được kiểm chứng trước khi chạy. Không dùng snapshot cũ của A để ghi đè dữ liệu
mới của B. Mỗi restore/failback phải xác định dữ liệu đích và có bản bảo toàn.
