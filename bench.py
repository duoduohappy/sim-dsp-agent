# -*- coding: utf-8 -*-
"""
bench.py — 竞价延迟压测与容量估算（P2）

⚠️ 仿真学习用途。回答面试核心问题："100ms 竞价超时，你的服务 P99 是多少？容量多大？"

输出：
  1. /bid 端点并发压测：P50/P90/P95/P99/max
  2. 单实例容量估算：QPS = 并发数 / 平均延迟
  3. 延迟预算分解：模型推理 / 序列化 / 网络 / 剩余余量（对比 100ms 上限）

用法：python bench.py [压测请求数，默认600] [并发线程数，默认8]
"""
from __future__ import annotations

import statistics
import sys
import threading
import time

import requests
import uvicorn

import dsp_server
import model

PORT = 8788
BASE = f"http://127.0.0.1:{PORT}"
SLA_MS = 100.0  # OpenRTB 竞价超时预算

import random
from run_ab import gen_request


def wait_ready(timeout: float = 15.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            requests.get(f"{BASE}/stats", timeout=1)
            return
        except requests.RequestException:
            time.sleep(0.2)
    raise RuntimeError("DSP server 未在超时内启动")


def model_inference_ms(samples: int = 200) -> float:
    """单独测量模型推理耗时（延迟预算分解用）"""
    bundle = model.load_model("pctr_model.pkl")
    sample = {"os": "ios", "region": "CN-BJ", "slot_h": 50, "hour_bucket": 2}
    t0 = time.perf_counter()
    for _ in range(samples):
        model.predict(bundle, sample)
    return (time.perf_counter() - t0) / samples * 1000


def bench(n_req: int, workers: int) -> dict:
    dsp_server.configure(strategy="ocpc", total_budget=100.0, target_cpa=3.0)
    rng = random.Random(7)
    payloads = [gen_request(random.Random(i), i)[0] for i in range(n_req)]
    latencies, errors = [], 0
    lock = threading.Lock()
    idx = {"i": 0}

    def worker():
        nonlocal errors
        session = requests.Session()
        while True:
            with lock:
                i = idx["i"]
                idx["i"] += 1
                if i >= n_req:
                    return
            t0 = time.perf_counter()
            try:
                r = session.post(f"{BASE}/bid", json=payloads[i], timeout=SLA_MS / 1000)
                ok = r.status_code == 200
            except requests.RequestException:
                ok = False
            dt = (time.perf_counter() - t0) * 1000
            with lock:
                (latencies if ok else [errors]).append(dt)
                if not ok:
                    errors += 1

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    latencies.sort()

    def pct(p: float) -> float:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else 0.0

    mean = statistics.mean(latencies) if latencies else 0.0
    return {"n": len(latencies), "errors": errors, "wall_s": round(wall, 2),
            "mean_ms": round(mean, 2), "p50_ms": pct(0.50), "p90_ms": pct(0.90),
            "p95_ms": pct(0.95), "p99_ms": pct(0.99), "max_ms": round(latencies[-1], 2),
            "qps": round(len(latencies) / wall, 1),
            "capacity_1c": round(1000.0 / mean, 0),
            "model_ms": round(model_inference_ms(), 3)}


def report(b: dict) -> str:
    lines = ["=" * 70,
             "      sim-dsp-agent /bid 延迟压测与容量报告（本机仿真环境）", "=" * 70,
             f"  压测请求 : {b['n']}   错误/超时 : {b['errors']}   并发 : 实测",
             f"  耗时     : {b['wall_s']}s   实测吞吐 : {b['qps']} QPS",
             "-" * 70, "  [ 延迟分布 ]",
             f"  P50 {b['p50_ms']:>8.2f} ms    P90 {b['p90_ms']:>8.2f} ms",
             f"  P95 {b['p95_ms']:>8.2f} ms    P99 {b['p99_ms']:>8.2f} ms",
             f"  Max {b['max_ms']:>8.2f} ms", "-" * 70,
             "  [ 延迟预算分解（SLA 100ms）]",
             f"  模型推理(单次)      : {b['model_ms']:>8.3f} ms",
             f"  HTTP+序列化+调度    : {b['p99_ms'] - b['model_ms']:>8.2f} ms（由 P99 反推）",
             f"  剩余安全余量        : {SLA_MS - b['p99_ms']:>8.2f} ms"
             f"  （{'PASS' if b['p99_ms'] < SLA_MS else 'FAIL'}: P99 {'<' if b['p99_ms'] < SLA_MS else '>='} 100ms）",
             "-" * 70,
             f"  [ 容量估算 ]",
             f"  单连接串行容量   : ~{b['capacity_1c']:.0f} QPS/核",
             f"  实测并发吞吐     : {b['qps']} QPS（{sys.argv[2] if len(sys.argv) > 2 else 8} 线程本机回环）",
             f"  日预算估算       : ~{b['qps'] * 86400 / 1e6:.1f} 百万次竞价/天/实例",
             "  生产提示         : uvicorn 多 worker + uvloop 可再放大数倍；",
             "                     真实瓶颈通常在模型特征查询(Redis P99)而非本端点。",
             "=" * 70]
    return "\n".join(lines)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    server = uvicorn.Server(uvicorn.Config(dsp_server.app, host="127.0.0.1",
                                           port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_ready()
    b = bench(n, int(sys.argv[2]) if len(sys.argv) > 2 else 8)
    print(report(b))


if __name__ == "__main__":
    main()
