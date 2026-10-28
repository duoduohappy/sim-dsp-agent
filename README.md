⚠️ NOTICE: This is a SIMULATION Demo for learning DSP / OpenRTB 2.5, NO REAL TRAFFIC, NOT FOR PRODUCTION.

# sim-dsp-agent

DSP + AI Agent 程序化广告竞价仿真 Demo（DSP Programmatic Advertising + AI Agent Dynamic Bidding Simulation）。

> ⚠️ 本项目**仅用于技术学习与演示**：
> - 不接入任何真实 AdX / SSP 广告流量
> - 不产生任何真实广告花费
> - 不可用于生产环境

## 项目简介

`sim-dsp-agent` 是一个轻量化的 DSP（Demand-Side Platform，需求方平台）竞价仿真系统，
用纯 Python 模拟 OpenRTB 2.5 协议的竞价核心流程，并由一个 **BiddingAgent（智能出价体）**
根据底价、剩余预算、历史转化率自动调整出价策略。

核心流程：

```
MockExchange(仿真流量)          BiddingAgent(智能体)          BudgetController        AdStats
      │  BidRequest(简化OpenRTB2.5)  │                            │                     │
      │ ──────────────────────────► │  检查预算是否耗尽           │                     │
      │                             │ ─────────────────────────► │                     │
      │                             │  动态出价(底价/预算/转化)    │                     │
      │  BidResponse(简化OpenRTB2.5)│ ◄───────────────────────── │                     │
      │ ◄────────────────────────── │                            │                     │
      │  模拟竞价胜出/曝光/转化       │                            │      记录曝光/转化/CPA │
      │ ─────────────────────────────────────────────────────────────────────────────► │
      │                             │                            │        仿真结束输出报表│
```

## 快速开始

```bash
pip install -r requirements.txt
python main.py        # 轻量仿真（进程内，秒级跑完）
```

### 进阶：DSP 服务化 + 出价模型 + A/B 对比（P0+P1）

```bash
python model.py       # 训练 pCTR/pCVR 出价模型（支持真实 iPinYou 数据集，见下）
python run_ab.py      # FastAPI 服务化 + 同流量 A/B 对比（fixed vs ocpc）
```

- `dsp_server.py`：FastAPI 实现的 OpenRTB 2.5 竞价服务（`POST /bid` 100ms 超时、
  `/win` `/clk` `/cvr` 事件回传、`/stats` 指标），内置 PID 预算 pacing
- `model.py`：pCTR/pCVR 逻辑回归模型。默认自动生成 iPinYou 风格合成数据开箱即跑；
  若将真实 iPinYou 数据集（含 `click` 列的 csv/tsv）放入 `data/ipinyou/` 目录则自动加载
- `run_ab.py`：同一带早晚高峰 QPS 曲线的仿真流量分别打两组策略，
  A 组固定 CPM 出价，B 组 oCPC（`目标CPA × pCTR × pCVR × 1000`），
  输出对比报表。示例结果：CTR +27%，CPA -59%（仿真数据）

### P2：工程架构（并发预算 / 压测 / Post-Auction 管线）

```bash
python bench.py 600 8   # /bid 延迟压测：P99 延迟、容量估算、延迟预算分解（SLA 100ms）
```

- `ledger.py`：并发安全预算账本，扣减三阶段 `reserve -> settle/reverse`
  （in-flight 预扣防超卖、超时冲正），内附生产环境 Redis+Lua 原子脚本映射
- `post_auction.py`：IVT 反作弊规则引擎（重复点击/速度异常/频次超限/地域 CTR 离群）
  + 转化归因（last-click / linear MTA，7 天窗口）
- 压测参考结果：P99 ≈ 30ms（SLA 100ms 通过），单实例 ~700 QPS 本机回环

### P3：LangGraph Multi-Agent 控制面

```bash
python agent_control.py   # Agent 调参前后两轮投放对比 + 决策日志
```

- `agent_control.py`：LangGraph 编排 `analyze -> tune -> apply` 三节点控制面
  （预算 Agent / Pacing Agent / 风控 Agent），通过 `/tune` 端点热更新 DSP 参数
- **核心设计论点：LLM/Agent 只进控制面，绝不进竞价关键路径**——
  100ms 竞价超时内等不起一次 LLM 推理；本 Demo Agent 为规则驱动占位
  （零 LLM 成本、结果可复现），替换为 LLM 调用即可，图结构不变

运行结束会输出一份仿真报表，包含：请求数、竞价数、胜出数、曝光数、
转化数、总花费、CPM、CPC、CPA、CTR、CVR 等核心指标。

## 项目结构

| 文件 | 说明 |
| --- | --- |
| `openrtb_demo.py` | 简化版 OpenRTB 2.5 协议模型（BidRequest / BidResponse）+ 仿真流量生成器 + 模拟竞价交易所 |
| `budget.py` | BudgetController 预算管控器：预算耗尽自动停止竞价 |
| `agent.py` | BiddingAgent 智能出价体：根据底价、剩余预算、历史转化动态调整出价 |
| `stat.py` | AdStats 统计模块：曝光 / 点击 / 转化 / CPA 等指标与最终报表 |
| `main.py` | 仿真入口，`python main.py` 一键跑完整流程 |
| `requirements.txt` | 依赖清单（仅 requests） |

## 简化版 OpenRTB 2.5 说明

完整 OpenRTB 2.5 规范字段非常庞大（数百个字段），本项目只保留**竞价核心链路**
的最小字段集，便于理解协议本质：

- **BidRequest**：`id` / `imp`（id, banner w/h, bidfloor, bidfloorcur）/ `device`（os, geo）/ `user`（id）
- **BidResponse**：`id` / `seatbid[].bid`（impid, price, adm, nurl）/ `cur`

真实流量、AdX 对接、Cookie Mapping、频控、反作弊等均不在本 Demo 范围内。

## 智能出价策略（BiddingAgent）

出价 = 基础出价 × 三因子动态调节：

1. **底价因子**：出价不低于 adslot 的 `bidfloor`（否则直接放弃响应）
2. **预算因子**：剩余预算比例越低 → 出价越保守（ pacing 控制）
3. **转化因子**：基于历史转化率（拉普拉斯平滑估计 pCVR）动态上调出价

预算耗尽时，`BudgetController` 会熔断竞价，仿真立即停止并输出报表。

## 免责声明（重复强调）

本项目为**纯仿真**学习项目，所有流量、竞价、转化均为随机模拟数据，
**没有任何真实广告流量、真实计费或真实用户数据**，不得用于任何生产用途。
