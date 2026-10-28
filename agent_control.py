# -*- coding: utf-8 -*-
"""
agent_control.py — LangGraph Multi-Agent 控制面（P3）

核心设计论点（面试话术）：
    LLM / Agent 只进「控制面」（离线/准实时调参、诊断、报告），
    绝不进「竞价关键路径」——100ms 竞价超时内等不起一次 LLM 推理。
    竞价面：FastAPI + 模型推理（毫秒级）；控制面：LangGraph 编排的
    分析 Agent -> 调参 Agent -> 执行 Agent，闭环改进投放效果。

本 Demo 的 Agent 为规则驱动（零 LLM 成本、确定性可复现），
LangGraph 的 analyze -> tune -> apply 图结构与真实 LLM Agent 完全一致，
只需把 tune 节点里的规则替换成 LLM 调用即可。

用法：python agent_control.py [每组请求数，默认800]
"""
from __future__ import annotations

import sys
import threading
import typing

import requests
import uvicorn
from langgraph.graph import StateGraph, START, END

import dsp_server
import post_auction
import run_ab

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"


# ---------------------------------------------------------------------------
# 共享状态
# ---------------------------------------------------------------------------
class AgentState(typing.TypedDict):
    stats: dict          # DSP 运行指标
    events: list         # 事件流
    ledger: dict         # 预算账本快照
    pipeline: dict       # post-auction 结果（IVT + 归因）
    params: dict         # 当前控制参数
    decisions: list      # Agent 决策日志


def node_analyze(state: AgentState) -> dict:
    """分析 Agent：拉取指标 + 事件流，跑 post-auction 管线（IVT + 归因）"""
    stats = requests.get(f"{BASE}/stats", timeout=5).json()
    ev = requests.get(f"{BASE}/events", timeout=5).json()
    pipeline = post_auction.run_pipeline(ev["events"])
    wins = max(stats["n_wins"], 1)
    analysis = {
        "ctr": stats["n_clicks"] / wins,
        "cpa": stats["spent"] / max(stats["n_convs"], 1),
        "ivt_rate": pipeline["ivt"]["blocked"] / max(pipeline["ivt"]["total_clicks"], 1),
        "win_rate": stats["n_wins"] / max(stats["n_bids"], 1),
    }
    return {"stats": stats, "events": ev["events"], "ledger": ev["ledger"],
            "pipeline": pipeline,
            "decisions": [f"[analyze] 曝光{stats['n_wins']} 点击{stats['n_clicks']} "
                          f"CTR={analysis['ctr']:.3f} IVT拦截率={analysis['ivt_rate']:.1%} "
                          f"预算剩余={ev['ledger']['remaining']:.3f}"]}


def node_tune(state: AgentState) -> dict:
    """调参 Agent：基于指标做规则决策（生产中此处替换为 LLM 推理）"""
    stats, ev = state["stats"], state["events"]
    ledger = state["ledger"]
    usage = ledger["spent"] / max(ledger["total_budget"], 1e-9)
    params = {"target_cpa": 3.0, "kp": 1.2}
    decisions = []

    # 预算 Agent：仅干预极端情况；中间区间说明 pacing 正常工作，不干预
    if usage > 0.9:
        params["target_cpa"] = round(params["target_cpa"] * 0.75, 2)
        decisions.append(f"[budget-agent] 预算使用率{usage:.0%}即将耗尽 -> target_cpa={params['target_cpa']}")
    elif usage < 0.3:
        params["target_cpa"] = round(params["target_cpa"] * 1.25, 2)
        decisions.append(f"[budget-agent] 预算使用率{usage:.0%}偏低 -> target_cpa={params['target_cpa']}")
    else:
        decisions.append(f"[budget-agent] 预算使用率{usage:.0%}符合 pacing 目标，不干预")

    # Pacing Agent：in-flight 积压说明出价过猛，降低 PID 增益
    if ledger["in_flight"] > 0.02 * ledger["total_budget"]:
        params["kp"] = 0.8
        decisions.append(f"[pacing-agent] in-flight={ledger['in_flight']:.3f} 积压 -> kp={params['kp']}")

    # 风控 Agent：IVT 拦截率过高时上报（生产中会收紧频控/加黑名单）
    ivt_rate = post_auction.ivt_filter(ev)
    decisions.append(f"[risk-agent] IVT 拦截 {ivt_rate['blocked']} 次点击，规则命中: {ivt_rate['reasons']}")

    # （LLM 决策点：此处的规则可替换为 LLM 对指标的解读与参数建议，竞价面不等待 LLM）
    return {"params": params, "decisions": state["decisions"] + decisions}


def node_apply(state: AgentState) -> dict:
    """执行 Agent：把新参数推给 DSP（/tune 热更新，竞价面零重启）"""
    requests.post(f"{BASE}/tune", json=state["params"], timeout=5)
    return {"decisions": state["decisions"] +
            [f"[apply-agent] 已热更新参数 {state['params']}"]}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("analyze", node_analyze)
    g.add_node("tune", node_tune)
    g.add_node("apply", node_apply)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "tune")
    g.add_edge("tune", "apply")
    g.add_edge("apply", END)
    return g.compile()


# ---------------------------------------------------------------------------
# 主流程：Episode1(默认参数) -> Agent 调参 -> Episode2(调参后) -> 对比报告
# ---------------------------------------------------------------------------
def wait_ready() -> None:
    import time
    t0 = time.time()
    while time.time() - t0 < 15:
        try:
            requests.get(f"{BASE}/stats", timeout=1)
            return
        except requests.RequestException:
            time.sleep(0.2)
    raise RuntimeError("DSP server 启动超时")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    server = uvicorn.Server(uvicorn.Config(dsp_server.app, host="127.0.0.1",
                                           port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_ready()

    graph = build_graph()
    print(f"[P3] LangGraph Agent 控制面启动，每组流量 {n} 条...\n")

    # Episode 1：默认参数跑基线
    e1 = run_ab.run_arm("ocpc", n, reset=True)
    state = graph.invoke({"stats": {}, "events": [], "pipeline": {},
                          "params": {}, "decisions": []})

    # Episode 2：Agent 调参后跑同分布流量
    e2 = run_ab.run_arm("ocpc", n, reset=False)
    _ = graph.invoke({"stats": {}, "events": [], "pipeline": {},
                      "params": {}, "decisions": []})

    # ------------------------------------------------------------------
    def kpis(e: dict) -> tuple:
        imp, clk, cv = max(e["n_wins"], 1), max(e["n_clicks"], 1), max(e["n_convs"], 1)
        return e["n_clicks"] / imp, e["n_convs"] / clk, e["spent"] / cv

    c1, c2 = kpis(e1), kpis(e2)
    print("=" * 72)
    print("     Agent 控制面调参前后对比（同分布流量，纯仿真数据）")
    print("=" * 72)
    print(f"  {'指标':<12}{'Episode1(默认)':>18}{'Episode2(Agent调参)':>22}")
    print(f"  {'CTR':<12}{c1[0]:>18.4f}{c2[0]:>22.4f}")
    print(f"  {'CVR':<12}{c1[1]:>18.4f}{c2[1]:>22.4f}")
    print(f"  {'CPA(¥)':<12}{c1[2]:>18.4f}{c2[2]:>22.4f}")
    print(f"  {'花费(¥)':<12}{e1['spent']:>18.4f}{e2['spent']:>22.4f}")
    print("-" * 72)
    print("  Agent 决策日志：")
    for d in state["decisions"]:
        print(f"    {d}")
    print("-" * 72)
    print(f"  Post-Auction 管线（ Episode1 事件流）:")
    ivt = post_auction.run_pipeline(
        requests.get(f"{BASE}/events", timeout=5).json()["events"])["ivt"]
    print(f"    IVT : 总点击 {ivt['total_clicks']} -> 有效 {ivt['valid_clicks']}"
          f"（拦截 {ivt['blocked']}），规则命中 {ivt['reasons']}")
    print("  ⚠️ 全部为仿真数据。Agent 为规则驱动占位，LangGraph 结构与 LLM 版一致。")
    print("=" * 72)


if __name__ == "__main__":
    main()
