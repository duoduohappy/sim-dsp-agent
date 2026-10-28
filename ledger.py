# -*- coding: utf-8 -*-
"""
ledger.py — 并发安全预算账本（P2）

解决技术总监面试高频问题："多线程/多实例并发竞价下预算怎么扣？"

单机方案（本文件，可直接运行）：
    reserve  -> 竞价时预扣（in-flight），防止超卖
    settle   -> 胜出通知到达后按实际结算价校准
    reverse  -> 超时未收到 win notice 的 reservation 到期冲正（in-flight 冲正）

生产映射（Redis + Lua 原子脚本，见下方 REDIS_LUA 常量）：
    单实例用 threading.Lock 即可；多实例部署时 reserve/settle/reverse 必须
    原子化，把预算余量放 Redis，用 Lua 脚本保证"读-判-扣"三步原子执行。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

# 生产环境 Redis+Lua 参考实现：reserve 的原子"查余量+预扣"
REDIS_LUA_RESERVE = """
local remaining = tonumber(redis.call('HGET', KEYS[1], 'remaining') or '0')
local need = tonumber(ARGV[1])
if remaining < need then return -1 end
redis.call('HINCRBYFLOAT', KEYS[1], 'remaining', -need)
redis.call('HINCRBYFLOAT', KEYS[1], 'in_flight', need)
return remaining - need
"""


class Reservation:
    __slots__ = ("req_id", "est_cpm", "ts")

    def __init__(self, req_id: str, est_cpm: float):
        self.req_id, self.est_cpm, self.ts = req_id, est_cpm, time.time()


class ConcurrentBudgetLedger:
    """线程安全预算账本：预算扣减三阶段（reserve -> settle/reverse）"""

    RESERVATION_TTL = 10.0  # in-flight 超时秒数，超时自动冲正（对应真实 win notice 丢失）

    def __init__(self, total_budget: float):
        self.total_budget = float(total_budget)
        self._lock = threading.RLock()  # 可重入锁：sweep/reserve 内部会调用 reverse
        self._pending: Dict[str, Reservation] = {}
        self.spent = 0.0          # 已结算（元）
        self.reversed_cpm = 0.0   # 累计冲正金额（CPM）
        self.events: List[dict] = []  # 事件流（生产中发 Kafka，本 Demo 内存队列）

    # -- 兼容 BudgetController 的只读接口（dsp_server /stats 沿用）--
    @property
    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self.total_budget - self.spent)

    @property
    def in_flight(self) -> float:
        with self._lock:
            return sum(r.est_cpm for r in self._pending.values()) / 1000.0

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= 0.0

    def can_bid(self) -> bool:
        return not self.is_exhausted

    # -- 三阶段扣减 --
    def reserve(self, req_id: str, est_cpm: float) -> bool:
        """出价前预扣：余量不足或预算耗尽返回 False（含过期冲正清扫）"""
        with self._lock:
            self._sweep_locked()
            if self.total_budget - self.spent - self._in_flight_cpm() < est_cpm / 1000.0:
                return False
            self._pending[req_id] = Reservation(req_id, est_cpm)
            self.events.append({"type": "reserve", "req_id": req_id, "ts": time.time()})
            return True

    def settle(self, req_id: str, final_cpm: float) -> float:
        """胜出：按结算价校准预扣差额，返回实际扣减（元）"""
        with self._lock:
            r = self._pending.pop(req_id, None)
            est = r.est_cpm if r else final_cpm
            cost = min(final_cpm / 1000.0, max(0.0, self.total_budget - self.spent))
            self.spent += cost
            self.events.append({"type": "win", "req_id": req_id, "ts": time.time()})
            return cost

    def reverse(self, req_id: str, reason: str = "timeout") -> bool:
        """冲正：竞价失败/超时未回 win notice，释放预扣额度"""
        with self._lock:
            r = self._pending.pop(req_id, None)
            if r:
                self.reversed_cpm += r.est_cpm
                self.events.append({"type": "reverse", "req_id": req_id,
                                    "reason": reason, "ts": time.time()})
            return r is not None

    # -- 内部 --
    def _in_flight_cpm(self) -> float:
        return sum(r.est_cpm for r in self._pending.values()) / 1000.0

    def _sweep_locked(self) -> int:
        """清扫过期 reservation 并冲正（生产中用 Redis TTL/ZSET 定时任务）"""
        now = time.time()
        expired = [k for k, r in self._pending.items()
                   if now - r.ts > self.RESERVATION_TTL]
        for k in expired:
            self.reverse(k)
        return len(expired)

    def sweep(self) -> int:
        with self._lock:
            return self._sweep_locked()

    def snapshot(self) -> dict:
        with self._lock:
            self._sweep_locked()
            return {"total_budget": round(self.total_budget, 4),
                    "spent": round(self.spent, 4),
                    "in_flight": round(self._in_flight_cpm(), 4),
                    "remaining": round(max(0.0, self.total_budget - self.spent), 4),
                    "reversed_cpm": round(self.reversed_cpm, 4),
                    "pending": len(self._pending),
                    "exhausted": self.total_budget - self.spent <= 0.0}
