# -*- coding: utf-8 -*-
"""
agent.py — BiddingAgent 智能出价体

⚠️ 仿真实现：所有出价均为虚拟数值，不涉及真实广告流量或真实计费。

策略概览（纯规则驱动的轻量智能体，无外部 LLM 依赖）：
    出价 = 基础出价 × 底价因子 × 预算因子 × 转化因子

    1. 底价因子：出价不得低于广告位底价 bidfloor，否则放弃响应（nbr=1 不出价）
    2. 预算因子：剩余预算比例越低，出价越保守（预算 pacing）
    3. 转化因子：基于历史转化率估计 pCVR（拉普拉斯平滑），转化越好的流量出价越高
"""

from __future__ import annotations

from typing import Optional

from openrtb_demo import BidRequest, BidResponse, Bid, SeatBid

# 不出价原因码（OpenRTB nbr 枚举的仿真子集）
NBR_UNKNOWN = 0            # 未知原因
NBR_BLOWN_BUDGET = 1       # 预算不足 / 流量不划算
NBR_BELOW_FLOOR = 2        # 出价低于底价

# 真实 DSP 常见做法：给 CTR/CVR 估计加平滑先验，避免小样本导致过拟合
PSEUDO_IMPRESSIONS = 200   # 拉普拉斯平滑：虚拟曝光数
PSEUDO_CONVERSIONS = 2     # 拉普拉斯平滑：虚拟转化数


class BiddingAgent:
    """
    智能出价体。

    用法：
        agent = BiddingAgent(base_bid=6.0, budget_controller=bc)
        response = agent.decide(bid_request)
        agent.on_result(win=True, converted=True)  # 竞价结果回填，驱动策略进化
    """

    def __init__(
        self,
        base_bid: float = 6.0,
        budget_controller=None,
        target_cpa: float = 0.05,
        currency: str = "CNY",
    ):
        """
        :param base_bid:        基础出价（CPM，元）
        :param budget_controller: BudgetController 实例，用于预算感知
        :param target_cpa:      目标单次转化成本（单次曝光计，仿真值）
        """
        if base_bid <= 0:
            raise ValueError("base_bid 必须为正数")
        self.base_bid = float(base_bid)
        self.bc = budget_controller
        self.target_cpa = float(target_cpa)
        self.currency = currency

        # 历史统计（策略状态）
        self.n_requests = 0     # 收到请求数
        self.n_bids = 0         # 出价数
        self.n_wins = 0         # 胜出数
        self.n_impressions = 0  # 曝光数
        self.n_conversions = 0  # 转化数

    # ------------------------------------------------------------------
    # 出价核心
    # ------------------------------------------------------------------
    def decide(self, request: BidRequest) -> BidResponse:
        """对一条 BidRequest 产出 BidResponse（可能不出价）"""
        self.n_requests += 1
        imp = request.imp[0]
        bidfloor = imp.bidfloor

        # 因子 0：预算耗尽 -> 熔断，不出价
        if self.bc is not None and not self.bc.can_bid():
            self.bc.block_request()
            return self._no_bid(request, NBR_BLOWN_BUDGET)

        bid = self._compute_bid(bidfloor)
        if bid is None:
            return self._no_bid(request, NBR_BELOW_FLOOR)

        self.n_bids += 1
        return BidResponse(
            id=request.id,
            seatbid=[SeatBid(
                seat="dsp-sim",
                bid=[Bid(
                    impid=imp.id,
                    price=round(bid, 4),
                    adm=f"<sim-ad os={request.device.os} geo={request.device.geo} "
                        f"w={imp.banner.w} h={imp.banner.h}>",
                    nurl="https://sim-dsp.invalid/win/notify",  # 仿真占位，不真实请求
                    cid="creative-001",
                    adid="ad-001",
                )],
            )],
            cur=self.currency,
        )

    def _compute_bid(self, bidfloor: float) -> Optional[float]:
        """
        出价公式：base × 底价因子 × 预算因子 × 转化因子
        若最终出价低于底价则放弃（返回 None）。
        """
        floor_factor = 1.0 + bidfloor / 10.0          # 底价越高的流量越贵，相应提价
        budget_factor = 0.5 + 0.5 * self._budget_ratio()  # 剩余预算越少越保守
        cvr_factor = 1.0 + self._estimated_pcvr()     # 历史转化越好出价越高

        bid = self.base_bid * floor_factor * budget_factor * cvr_factor
        # 出价至少要比底价高 5% 才值得参与（仿真简化策略）
        if bid < bidfloor * 1.05:
            return None
        return min(bid, bidfloor * 4.0)  # 封顶：不超过底价的4倍，避免失控出价

    # ------------------------------------------------------------------
    # 策略因子
    # ------------------------------------------------------------------
    def _budget_ratio(self) -> float:
        """剩余预算比例 0~1；未接 BudgetController 时视为 1（不 pacing）"""
        return self.bc.remaining_ratio if self.bc is not None else 1.0

    def _estimated_pcvr(self) -> float:
        """
        历史转化率估计（拉普拉斯平滑）：
            pCVR = (真实转化 + 先验转化) / (真实曝光 + 先验曝光)
        """
        return ((self.n_conversions + PSEUDO_CONVERSIONS)
                / (self.n_impressions + PSEUDO_IMPRESSIONS))

    # ------------------------------------------------------------------
    # 结果回填（强化学习思想的极简版：用结果更新策略状态）
    # ------------------------------------------------------------------
    def on_result(self, win: bool, converted: bool = False) -> None:
        """竞价结果回填：胜出 -> 胜出/曝光 +1；发生转化 -> 转化 +1"""
        if win:
            self.n_wins += 1
            self.n_impressions += 1
        if converted:
            self.n_conversions += 1

    def _no_bid(self, request: BidRequest, reason: int) -> BidResponse:
        """构造不出价响应（nbr 标注原因）"""
        return BidResponse(id=request.id, seatbid=[], nbr=reason)

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """智能体策略摘要"""
        return {
            "requests": self.n_requests,
            "bids": self.n_bids,
            "wins": self.n_wins,
            "impressions": self.n_impressions,
            "conversions": self.n_conversions,
            "bid_rate": round(self.n_bids / max(self.n_requests, 1), 4),
            "win_rate": round(self.n_wins / max(self.n_bids, 1), 4),
            "estimated_pcvr": round(self._estimated_pcvr(), 6),
        }
