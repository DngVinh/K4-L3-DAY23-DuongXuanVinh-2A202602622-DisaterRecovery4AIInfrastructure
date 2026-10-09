# Chạy Lab 23 trên Windows

Diễn tập chấm điểm dùng **Ubuntu/WSL + bare mode + --mock**. Toàn bộ traffic,
health checker, chaos và runbook phải chạy trong cùng Ubuntu, cùng thư mục.
Python Windows không có SIGSTOP/SIGCONT mà chaos netblock sử dụng. File PID
của WSL cũng không phải PID của Python Windows.

## 1. Mở đúng Ubuntu

Trong PowerShell:

```powershell
wsl -d Ubuntu
```

Trong terminal Ubuntu, chuyển đến thư mục lab đang dùng rồi làm các bước
trong [GUIDE.md](GUIDE.md). Với repo trên máy dùng cho bài nộp này:

```bash
cd "/mnt/c/Users/Duong Vinh/K4-L3-DAY23-DuongXuanVinh-2A202602622-DisaterRecovery4AIInfrastructure"
```

Để diễn tập thêm, tạo bản chạy mới trước khi seed/chạy traffic; giữ nguyên
bằng chứng đã nộp. `bash scripts/up_bare.sh` tạo các PID cần cho chaos.
Không khởi động stack nếu cổng 8001/8002/8080 đang thuộc dịch vụ khác.

## 2. Demo API bằng Python Windows

Phần này dành cho kiểm tra setup/inference trong thư mục chạy mới.
Không dùng cách này để chạy chaos netblock hoặc đo drill chấm điểm.
Cần Python và dependencies của requirements.txt được cài sẵn.

Trong PowerShell ở thư mục lab mới, kiểm tra state chưa tồn tại rồi seed:

```powershell
if ((Test-Path -LiteralPath state/region-a) -or (Test-Path -LiteralPath state/region-b)) {
    throw 'State đã tồn tại. Hãy dùng một thư mục chạy mới trước khi seed.'
}
python state/seed_vectors.py --region a --docs 200
python state/seed_vectors.py --region b --docs 0 --weights-mb 0
python -c "open('edge/active_region','x',encoding='utf-8').write('a')"
```

Chế độ `x` từ chối ghi đè nếu active_region đã tồn tại. UTF-8 của Python
không thêm BOM. Kiểm tra byte bằng lệnh chỉ đọc:

```powershell
python -c "from pathlib import Path; print(Path('edge/active_region').read_bytes())"
```

Kết quả mong đợi là `b'a'`. Trong Windows PowerShell 5.1, `-Encoding utf8`
thêm BOM; `-NoNewline` chỉ bỏ newline, không bỏ BOM.
[Nguồn Microsoft về encoding](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-5.1).

Mở ba terminal, mỗi terminal phải ở cùng thư mục lab. Đặt biến môi trường
cho từng region trước khi chạy uvicorn; nếu thiếu, ứng dụng dùng mặc định A.

Terminal 1 — Region A:

```powershell
$env:REGION = 'a'
$env:STATE_DIR = 'state/region-a'
$env:WARMUP_SECONDS = '6'
python -m uvicorn serving.app:app --host 127.0.0.1 --port 8001 --log-level warning
```

Terminal 2 — Region B:

```powershell
$env:REGION = 'b'
$env:STATE_DIR = 'state/region-b'
$env:WARMUP_SECONDS = '6'
python -m uvicorn serving.app:app --host 127.0.0.1 --port 8002 --log-level warning
```

Terminal 3 — Edge:

```powershell
$env:EDGE_TTL_SECONDS = '5'
python -m uvicorn edge.proxy:app --host 127.0.0.1 --port 8080 --log-level warning
```

Từ terminal khác, kiểm tra bằng curl.exe:

```powershell
curl.exe http://127.0.0.1:8001/healthz
curl.exe http://127.0.0.1:8002/healthz
curl.exe http://127.0.0.1:8002/v1/state
curl.exe http://127.0.0.1:8080/edge/state
curl.exe "http://127.0.0.1:8080/v1/infer?q=hoa%20don%20thang%207"
```

Checkpoint: A/B có đúng region tương ứng; B ban đầu count=0, weights=false;
edge chọn a và inference có edge_region=a, answer bắt đầu bằng [a].
Readiness còn bao gồm thời gian warm-up khi pool đổi trạng thái lúc chạy.

## 3. Các điểm cần hiểu đúng trong checklist

- `generateResolvConf` thuộc `[network]` của `/etc/wsl.conf`, không thuộc
  `.wslconfig`. Chỉ xử lý nếu có lỗi cấu hình thực tế; không cần thay đổi
  cấu hình máy khi Ubuntu đã hoạt động.
  [Nguồn Microsoft về cấu hình WSL](https://learn.microsoft.com/windows/wsl/wsl-config).
- Mỗi terminal phải ở cùng thư mục; không trộn PID, Python hoặc state giữa
  Windows và WSL. Không cần Start-Job cho quy trình này.
- `interval × threshold = 15s` là ngân sách detection danh nghĩa của lab.
  Detection thực đo phụ thuộc pha polling, timeout và lịch chạy; không phải
  luôn chính xác 15s hoặc một cận dưới toán học cố định.
- Các số 28.5s, 14.04s, 7 doc, 16/32 request trong hướng dẫn là ví dụ.
  Kết quả bài nộp này là RTO 28.6s, RPO 4s / 2 doc. Công cụ đo hiện tại
  xuất requests_failed; không có trường requests_total trong kết quả đo.
- Runbook mặc định hỏi y/N; --auto dành cho drill/CI. Một lỗi đơn lẻ không
  xác nhận outage; cần alert UNHEALTHY mới hợp lệ hoặc ba probe lỗi liên tiếp.
- Bằng chứng chính nằm tại các đường dẫn chuẩn trong [reports](reports),
  gồm timestamp thật và path:line. Postmortem phân tích standby, replication,
  warm-up và routing cache thay vì lấy việc chạy chaos làm nguyên nhân gốc.
