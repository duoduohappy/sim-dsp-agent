# -*- coding: utf-8 -*-
"""
run_ab.py — A/B 对比闭环（P0+P1 集成验收）

流程：
  1. 启动 FastAPI DSP 服务（本地 uvicorn）
  2. 同一随机流量（带早晚高峰 QPS 曲线、已知真实 pCTR）分别打两组策略：
     A组 fixed：固定 CPM 出价
     B组 ocpc：pCTR 模型 eCPM 出价（pCPC×pCTR×1000×pCVR加成）+ PID pacing
  3. 仿真二价竞价、胜出回传 /win、点击/转化回传 /clk /cvr
  4. 输出对比报表

用法：python run_ab.py [每组请求数，默认800]
"""
from __future__ import annotations

import math
import random
import sys
import threading
import time

import requests
import uvicorn

import dsp_server
import model

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
BUDGET = 0.6          # 每组仿真预算（元）
DURATION = 20.0       # 每组仿真时长（秒），QPS 曲线在此区间压缩一个昼夜周期
BASE_DELAY = 0.012    # 平均请求间隔（秒）

# 与 model._synth_dataset 一致的真实 CTR 潜变量（生成器已知答案，用于仿真点击/转化）
_OS_W = {"android": 0.0, "ios": 0.6, "windows": -0.5}
_RG_W = dict(zip(model.REGION_POOL, [0.5, 0.3, -0.2, 0.0, -0.4, 0.2]))
_SL_W = dict(zip(model.SLOT_H_POOL, [-0.3, 0.0, 0.4, 0.2]))
_HW = [0.4, -0.3, 0.0, 0.5]
_SLOTS = [(320, 50), (300, 90), (336, 250), (1080, 600)]


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def gen_request(rng: random.Random, i: int) -> tuple[dict, float]:
    """生成一条 OpenRTB 2.5 BidRequest + 该流量的真实 pCTR（仿真点击用）"""
    os_, rg = rng.choice(model.OS_POOL), rng.choice(model.REGION_POOL)
    w, h = rng.choice(_SLOTS)
    hb = i % 24 // 6
    pctr = sigmoid(-3.2 + _OS_W[os_] + _RG_W[rg] + _SL_W[h] + _HW[hb])
    req = {
        "id": f"req-{i:06d}",
        "imp": [{"id": "1", "banner": {"w": w, "h": h},
                 "bidfloor": round(rng.uniform(1.0, 8.0), 2), "bidfloorcur": "CNY"}],
        "device": {"os": os_, "geo": rg},
        "user": {"id": uuid_hex(rng)},
        "test": 1, "at": 2,
    }
    return req, pctr


def uuid_hex(rng: random.Random) -> str:
    return f"{rng.getrandbits(64):016x}"


def wait_ready(timeout: float = 15.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            requests.get(f"{BASE}/stats", timeout=1)
            return
        except requests.RequestException:
            time.sleep(0.2)
    raise RuntimeError("DSP server 未在超时内启动")


def run_arm(strategy: str, n: int, seed: int = 42) -> dict:
    """跑一组实验：fixed 或 ocpc"""
    dsp_server.configure(strategy=strategy, total_budget=BUDGET, target_cpa=3.0)
    rng = random.Random(seed)
    stats = {"req": 0, "bid": 0, "win": 0, "clk": 0, "cvr": 0, "timeout": 0, "no_bid": 0}
    t0 = time.time()

    for i in range(n):
        # QPS 曲线：昼夜正弦压缩到 DURATION 秒内（早晚高峰）
        frac = (time.time() - t0) / DURATION
        qps_factor = 1.0 + 0.6 * math.sin(2 * math.pi * frac)
        time.sleep(max(0.0, BASE_DELAY / qps_factor))

        if time.time() - t0 > DURATION + 5:
            break
        req, pctr_true = gen_request(rng, i)
        stats["req"] += 1
        try:
            r = requests.post(f"{BASE}/bid", json=req, timeout=0.1)  # 100ms 竞价超时
        except requests.Timeout:
            stats["timeout"] += 1
            continue
        if r.status_code != 200:
            continue
        resp = r.json()
        seat = resp.get("seatbid") or []
        if not seat:
            stats["no_bid"] += 1
            continue

        bid_cpm = seat[0]["bid"][0]["price"]
        floor = req["imp"][0]["bidfloor"]
        stats["bid"] += 1

        # 二价拍卖仿真：出价越高胜率越高，结算价 = 出价×0.85
        if rng.random() > min(0.95, 1 - floor / max(bid_cpm, floor) + 0.35):
            continue
        clearing = round(bid_cpm * 0.85, 4)
        stats["win"] += 1
        requests.get(f"{BASE}/win", params={"req_id": req["id"], "price": clearing}, timeout=0.5)

        if rng.random() < pctr_true:  # 已知真实 pCTR 驱动的点击仿真
            stats["clk"] += 1
            requests.get(f"{BASE}/clk", params={"req_id": req["id"]}, timeout=0.5)
            if rng.random() < 0.15:   # 点击后转化
                stats["cvr"] += 1
                requests.get(f"{BASE}/cvr", params={"req_id": req["id"]}, timeout=0.5)

    time.sleep(0.3)
    srv = requests.get(f"{BASE}/stats", timeout=2).json()
    srv.update({k: stats[k] for k in ("req", "timeout")})
    return srv


def report(results: list[tuple[str, dict]]) -> str:
    lines = ["=" * 76, "        sim-dsp-agent A/B 对比报表（fixed vs ocpc，纯仿真数据）", "=" * 76,
             f"{'指标':<16}{'A: fixed':>16}{'B: ocpc':>16}", "-" * 76]
    keys = [("竞价请求", "req"), ("出价数", "n_bids"), ("竞拍超时", "timeout"),
            ("胜出/曝光", "n_wins"), ("点击", "n_clicks"), ("转化", "n_convs"),
            ("总花费¥", "spent"), ("平均出价CPM¥", "avg_bid"), ("平均pCTR", "avg_pctr")]
    for label, k in keys:
        lines.append(f"{label:<16}{results[0][1].get(k, 0):>16}{results[1][1].get(k, 0):>16}")
    def derive(r: dict) -> dict:
        imp = max(r["n_wins"], 1); clk = max(r["n_clicks"], 1); cv = max(r["n_convs"], 1)
        return {"CTR": r["n_clicks"] / imp, "CVR": r["n_convs"] / clk,
                "CPA¥": r["spent"] / cv, "CPM¥": r["spent"] / imp * 1000}
    da, db = derive(results[0][1]), derive(results[1][1])
    for label in ("CTR", "CVR", "CPM¥", "CPA¥"):
        lines.append(f"{label:<16}{da[label]:>16.4f}{db[label]:>16.4f}")
    lines += ["-" * 76,
              "  结论要点：ocpc 组用 pCTR 模型把预算集中到高转化流量上，对比 fixed 组的", 
              "  CTR/CVR 提升与 CPA 下降即为模型收益；预算耗尽说明 pacing 有效熔断。",
              "  ⚠️ 本报表全部为仿真随机数据，不代表任何真实广告效果。", "=" * 76]
    return "\n".join(lines)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    server = uvicorn.Server(uvicorn.Config(dsp_server.app, host="127.0.0.1",
                                           port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_ready()

    results = []
    for strategy in ("fixed", "ocpc"):
        print(f"[run] A/B 组 {strategy} 启动（预算 ¥{BUDGET}，QPS 曲线 {DURATION}s）...")
        results.append((strategy, run_arm(strategy, n)))
        print(f"      完成: {results[-1][1]}")

    print(report(results))


if __name__ == "__main__":
    main()
