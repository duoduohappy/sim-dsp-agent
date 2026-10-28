# -*- coding: utf-8 -*-
"""
stat.py — AdStats 广告效果统计模块

⚠️ 仿真实现：所有指标均基于模拟数据计算，仅用于学习竞价效果度量。

职责：
    1. 记录竞价请求、出价、胜出、曝光、点击、转化、花费
    2. 计算核心指标：CPM / CPC / CPA / CTR / CVR / 胜出率
    3. 仿真结束输出可读报表
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

REPORT_TITLE = "=" * 72
REPORT_LINE = "-" * 72


class AdStats:
    """广告效果统计器（线程不安全，本仿真为单线程使用）"""

    def __init__(self):
        self.n_requests = 0        # 收到的竞价请求数
        self.n_bids = 0            # DSP 出价数
        self.n_no_bids = 0         # 不出价数
        self.n_wins = 0            # 竞价胜出数（≈曝光前奏）
        self.n_impressions = 0     # 曝光数
        self.n_clicks = 0          # 点击数
        self.n_conversions = 0     # 转化数
        self.total_cost = 0.0      # 总花费（元，仿真结算价累计）
        self.cost_history: List[float] = []  # 每笔花费流水
        self.started_at = datetime.now()

    # ------------------------------------------------------------------
    # 事件记录
    # ------------------------------------------------------------------
    def on_request(self, bid: bool) -> None:
        """记录一条竞价请求，bid=True 表示 DSP 出价"""
        self.n_requests += 1
        if bid:
            self.n_bids += 1
        else:
            self.n_no_bids += 1

    def on_win(self, clearing_price_cpm: float) -> None:
        """竞价胜出：记曝光并累计花费（CPM 折算单次曝光成本）"""
        self.n_wins += 1
        self.n_impressions += 1
        cost = clearing_price_cpm / 1000.0
        self.total_cost += cost
        self.cost_history.append(cost)

    def on_click(self) -> None:
        self.n_clicks += 1

    def on_conversion(self) -> None:
        self.n_conversions += 1

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------
    def metrics(self) -> Dict[str, float]:
        """计算核心广告指标"""
        req = max(self.n_requests, 1)
        bid = max(self.n_bids, 1)
        imp = max(self.n_impressions, 1)
        clk = max(self.n_clicks, 1)
        cvt = max(self.n_conversions, 1)
        return {
            "bid_rate": self.n_bids / req,            # 参竞率
            "win_rate": self.n_wins / bid,            # 胜出率（出价维度）
            "ctr": self.n_clicks / imp,               # 点击率 CTR
            "cvr": self.n_conversions / clk,          # 转化率 CVR（点击->转化）
            "cpm": self.total_cost / imp * 1000.0,    # 千次曝光成本 CPM（元）
            "cpc": self.total_cost / clk,             # 单次点击成本 CPC（元）
            "cpa": self.total_cost / cvt,             # 单次转化成本 CPA（元）
        }

    # ------------------------------------------------------------------
    # 报表输出
    # ------------------------------------------------------------------
    def report(self) -> str:
        """生成仿真结束报表"""
        m = self.metrics()
        ended_at = datetime.now()
        duration = (ended_at - self.started_at).total_seconds()
        lines = [
            REPORT_TITLE,
            "         sim-dsp-agent 竞价仿真报表（纯仿真数据，非真实流量）",
            REPORT_TITLE,
            f"  开始时间   : {self.started_at.isoformat(timespec='seconds')}",
            f"  结束时间   : {ended_at.isoformat(timespec='seconds')}",
            f"  仿真时长   : {duration:.2f} 秒",
            REPORT_LINE,
            "  [ 竞价漏斗 ]",
            f"  竞价请求      : {self.n_requests:>10,}",
            f"  DSP 出价      : {self.n_bids:>10,}   (参竞率 {m['bid_rate']:.2%})",
            f"  不出价        : {self.n_no_bids:>10,}",
            f"  竞价胜出      : {self.n_wins:>10,}   (胜出率 {m['win_rate']:.2%})",
            REPORT_LINE,
            "  [ 效果指标 ]",
            f"  曝光 Impressions : {self.n_impressions:>10,}",
            f"  点击 Clicks      : {self.n_clicks:>10,}   (CTR {m['ctr']:.4%})",
            f"  转化 Conversions : {self.n_conversions:>10,}   (CVR {m['cvr']:.4%})",
            REPORT_LINE,
            "  [ 成本指标 ]（单位：元，仿真结算价）",
            f"  总花费   : {self.total_cost:>12.4f}",
            f"  CPM      : {m['cpm']:>12.4f}",
            f"  CPC      : {m['cpc']:>12.4f}",
            f"  CPA      : {m['cpa']:>12.4f}",
            REPORT_LINE,
            "  ⚠️ 本报表全部为仿真随机数据，不代表任何真实广告效果。",
            REPORT_TITLE,
        ]
        return "\n".join(lines)

    def summary(self) -> Dict[str, float]:
        """机器可读摘要（dict）"""
        return {
            "requests": self.n_requests,
            "bids": self.n_bids,
            "no_bids": self.n_no_bids,
            "wins": self.n_wins,
            "impressions": self.n_impressions,
            "clicks": self.n_clicks,
            "conversions": self.n_conversions,
            "total_cost": round(self.total_cost, 4),
            **{k: round(v, 6) for k, v in self.metrics().items()},
        }
