# -*- coding: utf-8 -*-
"""
main.py — sim-dsp-agent 仿真入口

⚠️ 纯仿真 Demo：不接入真实 AdX 流量，不产生真实花费，仅用于学习。

运行方式：
    python main.py

流程：
    1. 初始化预算管控、智能出价体、仿真流量生成器、模拟交易所、统计器
    2. 循环产生模拟 BidRequest -> Agent 出价 -> MockExchange 仿真竞价
       -> 胜出则扣预算、记曝光、随机点击/转化
    3. 预算耗尽自动熔断 或 请求跑完 -> 输出最终仿真报表
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys

from agent import BiddingAgent
from budget import BudgetController
from openrtb_demo import MockBidRequestGenerator, MockExchange

# ---------------------------------------------------------------------------
# 加载本地 stat.py（与 Python 标准库 stat 模块重名，部分环境会优先解析到标准库，
# 这里做兼容回退：显式从文件加载本目录下的 stat.py）
# ---------------------------------------------------------------------------
try:
    from stat import AdStats
except ImportError:  # 命中了标准库 stat，回退为按路径显式加载
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location("sim_stat", os.path.join(_HERE, "stat.py"))
    _sim_stat = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("sim_stat", _sim_stat)
    _spec.loader.exec_module(_sim_stat)
    AdStats = _sim_stat.AdStats

# ---------------------------------------------------------------------------
# 仿真参数（可自行调整）
# ---------------------------------------------------------------------------
SIM_CONFIG = {
    "total_budget": 0.6,      # 仿真总预算（元，虚拟；调小可观察预算耗尽熔断）
    "base_bid": 3.0,          # 基础出价（CPM，元）
    "n_requests": 3000,       # 模拟竞价请求数
    "click_rate": 0.03,       # 仿真点击概率（胜出曝光后）
    "conversion_rate": 0.3,   # 仿真转化概率（点击后）
    "seed": 42,               # 随机种子，保证结果可复现
    "log_every": 500,         # 每多少条请求打印一次进度
}

DISCLAIMER = (
    "⚠️ NOTICE: This is a SIMULATION Demo for learning DSP / OpenRTB 2.5, "
    "NO REAL TRAFFIC, NOT FOR PRODUCTION."
)


def run_simulation(config: dict) -> None:
    """跑完整仿真流程"""
    print(DISCLAIMER)
    print("开始 sim-dsp-agent 竞价仿真 ...")
    print(f"仿真参数: {config}\n")

    rng = random.Random(config["seed"])

    budget = BudgetController(total_budget=config["total_budget"])
    agent = BiddingAgent(base_bid=config["base_bid"], budget_controller=budget)
    generator = MockBidRequestGenerator(seed=config["seed"])
    exchange = MockExchange()
    stats = AdStats()

    stopped_reason = "requests_finished"

    for i in range(1, config["n_requests"] + 1):
        # 1) 预算管控：耗尽即熔断，自动停止竞价
        if not budget.can_bid():
            budget.block_request()
            stopped_reason = "budget_exhausted"
            break

        # 2) 仿真流量：生成简化版 OpenRTB 2.5 BidRequest
        request = generator.next_request()

        # 3) 智能体出价：产出 BidResponse（可能不出价）
        response = agent.decide(request)
        bid_made = bool(response.seatbid)
        stats.on_request(bid=bid_made)

        if not bid_made:
            continue

        # 4) 模拟交易所：竞价裁决（胜出 -> 结算）
        outcome = exchange.decide(request, response)
        if outcome is None:
            agent.on_result(win=False)
            continue

        # 5) 胜出：预算扣减 + 曝光统计（双账本：预算模块与统计模块各自记账）
        cost = budget.commit_spend(request.id, outcome["clearing_price"])
        stats.on_win(outcome["clearing_price"])
        agent.on_result(win=True, converted=False)

        # 6) 仿真点击 / 转化（随机模拟用户行为）
        converted = False
        if rng.random() < config["click_rate"]:
            stats.on_click()
            if rng.random() < config["conversion_rate"]:
                stats.on_conversion()
                converted = True
                agent.on_result(win=False, converted=True)  # 补记转化（曝光已记过）

        # 进度日志
        if i % config["log_every"] == 0:
            print(f"  [进度 {i:>4}/{config['n_requests']}] "
                  f"已花费 ¥{budget.spent:.4f} / ¥{budget.total_budget:.2f} "
                  f"(剩余 {budget.remaining_ratio:.1%})")

    # ------------------------------------------------------------------
    # 仿真结束：输出报表
    # ------------------------------------------------------------------
    print(f"\n仿真结束，原因: {stopped_reason}\n")
    print(stats.report())

    print("BudgetController 摘要:")
    for k, v in budget.summary().items():
        print(f"  {k:<18}: {v}")

    print("\nBiddingAgent 策略摘要:")
    for k, v in agent.summary().items():
        print(f"  {k:<18}: {v}")


if __name__ == "__main__":
    # 允许通过命令行覆盖请求数: python main.py [n_requests]
    if len(sys.argv) > 1:
        try:
            SIM_CONFIG["n_requests"] = int(sys.argv[1])
        except ValueError:
            pass
    run_simulation(SIM_CONFIG)
