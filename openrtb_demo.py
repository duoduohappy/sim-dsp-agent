# -*- coding: utf-8 -*-
"""
openrtb_demo.py — 简化版 OpenRTB 2.5 协议 Demo

⚠️ 本模块为纯仿真实现，用于学习 OpenRTB 2.5 竞价协议核心链路。
   不接入任何真实 AdX / SSP 流量，不产生真实计费。

简化说明：
    完整 OpenRTB 2.5 规范有数百个字段，这里只保留竞价核心最小字段集：
      BidRequest:  id / imp(id, banner w/h, bidfloor, bidfloorcur) / device(os, geo) / user(id)
      BidResponse: id / seatbid[].bid(impid, price, adm, nurl) / cur
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 协议版本标识
# ---------------------------------------------------------------------------
OPENRTB_VERSION = "2.5"

# 模拟的操作系统与地域池（纯随机仿真数据）
SIM_OS_POOL = ["android", "ios"]
SIM_GEO_POOL = ["CN-GD", "CN-BJ", "CN-SH", "CN-ZJ", "CN-SC"]

# 模拟广告位尺寸池
SIM_BANNER_POOL = [(320, 50), (300, 250), (640, 100), (1080, 1920)]


# ---------------------------------------------------------------------------
# BidRequest（简化）
# ---------------------------------------------------------------------------
@dataclass
class Banner:
    """横幅素材位，简化自 OpenRTB 的 banner 对象"""
    w: int                      # 宽
    h: int                      # 高


@dataclass
class Imp:
    """impression 曝光机会，简化自 OpenRTB 的 imp 对象"""
    id: str                     # 曝光位编号（同一请求内唯一）
    banner: Banner              # 广告位尺寸
    bidfloor: float             # 底价（CPM，元）
    bidfloorcur: str = "CNY"    # 底价币种


@dataclass
class Device:
    """设备信息，简化自 OpenRTB 的 device 对象"""
    os: str                     # 操作系统
    geo: str                    # 地域


@dataclass
class User:
    """用户信息，简化自 OpenRTB 的 user 对象"""
    id: str                     # 仿真用户ID（非真实用户）


@dataclass
class BidRequest:
    """DSP 收到的竞价请求（简化版 OpenRTB 2.5 bid request）"""
    id: str                     # 竞价请求ID
    imp: List[Imp]              # 曝光机会列表
    device: Device              # 设备
    user: User                  # 用户
    test: int = 1               # test=1 表示仿真测试流量
    tmax: int = 100             # 竞价超时（ms，仿真值）
    at: int = 2                 # 拍卖类型：1=一价 2=二价（仿真使用二价）

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 OpenRTB 风格的 dict（演示用）"""
        return asdict(self)


# ---------------------------------------------------------------------------
# BidResponse（简化）
# ---------------------------------------------------------------------------
@dataclass
class Bid:
    """单个曝光位的出价（简化自 OpenRTB 的 bid 对象）"""
    impid: str                  # 对应 BidRequest.imp.id
    price: float                # 出价（CPM，元）
    adm: str = ""               # 广告物料（仿真中使用占位符）
    nurl: str = ""              # 胜出通知 URL（仿真占位符，不真实请求）
    cid: str = ""               # 创意ID
    adid: str = ""              # 广告ID


@dataclass
class SeatBid:
    """seatbid：按席位分组的出价集合"""
    seat: str                   # 席位ID（仿真固定为 dsp-sim）
    bid: List[Bid] = field(default_factory=list)


@dataclass
class BidResponse:
    """DSP 返回的竞价响应（简化版 OpenRTB 2.5 bid response）"""
    id: str                     # 回填请求ID
    seatbid: List[SeatBid] = field(default_factory=list)
    cur: str = "CNY"            # 出价币种
    nbr: Optional[int] = None   # 不出价原因码（不出价时使用）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 仿真流量生成器（Mock Exchange 侧）
# ---------------------------------------------------------------------------
class MockBidRequestGenerator:
    """生成模拟的 BidRequest 流量，纯随机，无真实流量"""

    def __init__(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)

    def next_request(self) -> BidRequest:
        w, h = self._rng.choice(SIM_BANNER_POOL)
        # 底价 1.0 ~ 8.0 CPM 的仿真分布
        bidfloor = round(self._rng.uniform(1.0, 8.0), 2)
        return BidRequest(
            id=uuid.uuid4().hex[:16],
            imp=[Imp(
                id="1",
                banner=Banner(w=w, h=h),
                bidfloor=bidfloor,
                bidfloorcur="CNY",
            )],
            device=Device(
                os=self._rng.choice(SIM_OS_POOL),
                geo=self._rng.choice(SIM_GEO_POOL),
            ),
            user=User(id=uuid.uuid4().hex[:12]),
            test=1,
        )


# ---------------------------------------------------------------------------
# 模拟竞价交易所（Mock Exchange）
# ---------------------------------------------------------------------------
class MockExchange:
    """
    模拟广告交易所：接收 DSP 的 BidResponse，仿真"竞价胜出 -> 曝光 -> 转化"链路。
    胜出规则（简化）：出价高于底价即有概率胜出，出价越高胜率越高。
    """

    CLEARING_PRICE_MULTIPLIER = 0.85  # 简化二价：以出价 * 0.85 作为仿真结算价

    def decide(self, request: BidRequest, response: BidResponse) -> Optional[Dict[str, Any]]:
        """
        根据出价仿真竞价结果。
        返回:
            None                      -> 未胜出（或 DSP 未出价）
            {"price": 结算价CPM, ...}  -> 胜出并进入曝光
        """
        imp = request.imp[0]
        if not response.seatbid or not response.seatbid[0].bid:
            return None

        bid = response.seatbid[0].bid[0]

        # 出价低于底价直接判负（OpenRTB 中交易所会直接过滤）
        if bid.price < imp.bidfloor:
            return None

        # 胜出概率随出价/底价比值上升，封顶 0.95（仿真简化拍卖）
        ratio = bid.price / imp.bidfloor
        win_prob = min(0.95, 1.0 - 1.0 / max(ratio, 1.0) + 0.35)
        if random.random() > win_prob:
            return None

        clearing_price = round(bid.price * self.CLEARING_PRICE_MULTIPLIER, 4)
        return {
            "impid": bid.impid,
            "clearing_price": clearing_price,   # 仿真结算价（CPM，元）
            "banner": (imp.banner.w, imp.banner.h),
            "device_os": request.device.os,
            "geo": request.device.geo,
        }
