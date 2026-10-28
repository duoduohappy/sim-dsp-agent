# -*- coding: utf-8 -*-
"""
budget.py — BudgetController 预算管控模块

⚠️ 仿真实现：这里的"预算"是虚拟数值，不涉及任何真实资金。

职责：
    1. 管控仿真总预算，出价前检查剩余预算
    2. 竞价胜出后按结算价扣减预算（CPM 折算单次曝光成本）
    3. 预算耗尽自动熔断，停止后续竞价
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class SpendRecord:
    """一笔花费记录（用于审计与复盘）"""
    request_id: str     # 竞价请求ID
    cost_cpm: float     # 结算价（CPM，元）
    cost_actual: float  # 单次曝光实际扣减金额（CPM/1000）


class BudgetController:
    """
    预算管控器。

    用法：
        bc = BudgetController(total_budget=100.0)
        if bc.can_bid():
            ...  # 参与竞价
            bc.commit_spend(request_id, clearing_price_cpm)
    """

    def __init__(self, total_budget: float, currency: str = "CNY"):
        if total_budget <= 0:
            raise ValueError("total_budget 必须为正数")
        self.total_budget = float(total_budget)
        self.currency = currency
        self.spent = 0.0                       # 已花费金额
        self.records: List[SpendRecord] = []   # 花费明细
        self.exhausted_at: datetime | None = None  # 预算耗尽时间
        self._blocked_requests = 0             # 因预算耗尽被拦截的请求数

    # ------------------------------------------------------------------
    @property
    def remaining(self) -> float:
        """剩余预算"""
        return max(0.0, self.total_budget - self.spent)

    @property
    def remaining_ratio(self) -> float:
        """剩余预算比例 (0.0 ~ 1.0)，供出价策略做 pacing"""
        return self.remaining / self.total_budget

    @property
    def is_exhausted(self) -> bool:
        """预算是否耗尽（耗尽即熔断竞价）"""
        return self.remaining <= 0.0

    # ------------------------------------------------------------------
    def can_bid(self) -> bool:
        """出价前置检查：预算耗尽返回 False，自动停止竞价"""
        return not self.is_exhausted

    def block_request(self) -> None:
        """记录一次因预算耗尽被拦截的请求（仅统计）"""
        self._blocked_requests += 1

    def commit_spend(self, request_id: str, clearing_price_cpm: float) -> float:
        """
        竞价胜出后扣减预算。
        clearing_price_cpm 为 CPM 结算价，单次曝光成本 = CPM / 1000。
        返回实际扣减金额（不会超过剩余预算）。
        """
        if self.is_exhausted:
            return 0.0

        cost = min(clearing_price_cpm / 1000.0, self.remaining)
        self.spent += cost
        self.records.append(SpendRecord(
            request_id=request_id,
            cost_cpm=clearing_price_cpm,
            cost_actual=round(cost, 6),
        ))

        if self.is_exhausted and self.exhausted_at is None:
            self.exhausted_at = datetime.now()
        return round(cost, 6)

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """预算使用摘要"""
        return {
            "total_budget": round(self.total_budget, 4),
            "spent": round(self.spent, 4),
            "remaining": round(self.remaining, 4),
            "usage_ratio": round(self.spent / self.total_budget, 4),
            "spend_count": len(self.records),
            "blocked_requests": self._blocked_requests,
            "exhausted": self.is_exhausted,
            "exhausted_at": self.exhausted_at.isoformat(timespec="seconds")
                            if self.exhausted_at else None,
        }
