"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK step {n}: {name}", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    ans = input(f"{msg} [y/N]: ").strip().lower()
    return ans in ["y", "yes"]


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook: xac_nhan_outage -> thong_bao_incident -> scale_gpu_pool -> verify_state_replica -> dns_cutover -> verify_golden_signals -> post_incident."""
    if primary not in URL or target not in URL or primary == target:
        return {"ok": False, "error": "invalid_regions"}
    run_start = time.time()

    # 1 xac_nhan_outage
    # Cho health check phat hien UNHEALTHY neu dang co drill dien ra
    health_log = pathlib.Path("reports/health-events.jsonl")
    detected = False
    start_wait = time.time()
    while time.time() - start_wait < 25.0:
        if health_log.exists():
            lines = [l for l in health_log.read_text().splitlines() if l.strip()]
            for line in lines:
                try:
                    ev = json.loads(line)
                    if (ev.get("event") == "state_change" and
                            ev.get("region") == primary and
                            ev.get("to") == "UNHEALTHY" and
                            ev.get("ts", 0) >= run_start - 30.0):
                        detected = True
                        break
                except Exception:
                    pass
        if detected:
            break
        try:
            r = httpx.get(f"{URL[primary]}/readyz", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)

    primary_ok = False
    try:
        r = httpx.get(f"{URL[primary]}/readyz", timeout=1.5)
        primary_ok = (r.status_code == 200)
    except Exception:
        pass

    target_ok = False
    try:
        r = httpx.get(f"{URL[target]}/healthz", timeout=1.5)
        target_ok = (r.status_code == 200)
    except Exception:
        pass

    step(1, "xac_nhan_outage", primary=primary, target=target,
         health_alert_detected=detected, primary_ready=primary_ok, target_alive=target_ok)
    if primary_ok:
        return {"ok": False, "error": "primary_still_ready"}
    if not target_ok:
        return {"ok": False, "error": "target_unreachable"}

    # 2 thong_bao_incident
    if not confirm(auto, f"Xac nhan failover tu {primary} sang {target}?"):
        print("Failover bi huy bo boi nguoi van hanh.")
        return {"ok": False, "aborted": True}

    t_outage = None
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_path.exists():
        kills = [json.loads(l) for l in chaos_path.read_text().splitlines() if l.strip()]
        kills = [k for k in kills if k.get("action") == "kill"]
        if kills:
            t_outage = kills[-1].get("ts")

    now = time.time()
    notif_delay = round(now - t_outage, 2) if t_outage else None
    step(2, "thong_bao_incident", t_outage=t_outage,
         notification_delay_s=notif_delay,
         channel="#dr-incidents")

    # 3 scale_gpu_pool
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=fo_res.get("ok"), waited_s=fo_res.get("waited_s"))
    if not fo_res.get("ok"):
        return {"ok": False, "error": "failover_failed", "detail": fo_res}

    # 4 verify_state_replica
    st = fo_res.get("state", {})
    count = st.get("count")
    weights = st.get("weights")
    rpo_seconds = fo_res.get("rpo_seconds")
    docs_lost = fo_res.get("docs_lost")
    step(4, "verify_state_replica", count=count, weights=weights,
         rpo_seconds=rpo_seconds, docs_lost=docs_lost)

    # 5 dns_cutover
    step(5, "dns_cutover", active_region=target, ok=fo_res.get("ok"))

    # 6 verify_golden_signals (10 requests vao region target)
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t0 = time.time()
            try:
                r = client.get(f"{URL[target]}/v1/infer", params={"q": f"probe-{i}"})
                lat = (time.time() - t0) * 1000.0
                latencies.append(lat)
                if r.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            time.sleep(0.05)

    latencies.sort()
    p95_ms = round(latencies[int(0.95 * len(latencies))], 1) if latencies else None
    err_rate = round(errors / 10.0, 2)
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=p95_ms, error_rate=err_rate)

    # 7 post_incident
    elapsed = round(time.time() - run_start, 2)
    measure_cmd = "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed, measure_cmd=measure_cmd)

    return {
        "ok": errors == 0,
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed,
        "rpo_seconds": rpo_seconds,
        "docs_lost": docs_lost,
        "p95_latency_ms": p95_ms,
        "error_rate": err_rate,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
