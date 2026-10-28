# -*- coding: utf-8 -*-
"""
dsp_server.py — DSP 服务化（P0）：FastAPI 实现 OpenRTB 2.5 竞价端点

⚠️ 仿真学习用途，不接入真实 AdX 流量。

端点：
  POST /bid        竞价：解析 OpenRTB 2.5 BidRequest，返回 BidResponse（可能不出价）
  GET  /win        胜出通知：扣预算（in-flight -> settled），返回曝光监测宏
  GET  /clk        曝光->点击事件
  GET  /cvr        点击->转化事件
  GET  /stats      仿真指标（供 A/B 对比）

策略开关（A/B 对比）：
  configure(strategy="fixed"|"ocpc")   fixed=固定CPM出价  ocpc=eCPM=pCPC×pCTR×1000
预算：复用 budget.BudgetController；pacing：PID 控制器动态调节出价系数。
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from budget import BudgetController

MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="sim-dsp-agent DSP", docs_url=None, redoc_url=None)

STATE: Dict[str, Any] = {}


def configure(strategy: str = "ocpc", total_budget: float = 0.6,
              target_cpa: float = 2.0, use_model: bool = True) -> None:
    """（重新）初始化 DSP 状态，供 A/B 两组实验分别使用"""
    STATE.clear()
    STATE.update({
        "strategy": strategy,
        "budget": BudgetController(total_budget=total_budget),
        "target_cpa": target_cpa,
        "use_model": use_model,
        "pctr": None, "pcvr": None,
        "bids": {},                # req_id -> 出价上下文
        "kp": 1.2,                 # PID 比例系数
        "pid_i": 0.0,              # 积分项
        "start_ts": time.time(),
    })
    if use_model:
        import model
        STATE["pctr"] = model.load_model("pctr_model.pkl")
        STATE["pcvr"] = model.load_model("pcvr_model.pkl")


def _pacing_factor() -> float:
    """PID pacing：目标匀速花费（剩余比例 ≈ 剩余时间比例），偏差积分修正"""
    s = STATE
    elapsed = min(1.0, (time.time() - s["start_ts"]) / s.get("sim_duration", 30.0))
    spent_ratio = s["budget"].spent / s["budget"].total_budget
    err = spent_ratio - elapsed                    # 花快了 err>0 -> 降出价
    s["pid_i"] = max(-0.5, min(0.5, s["pid_i"] + err * 0.02))
    return max(0.2, min(1.8, 1.0 - s["kp"] * err - s["pid_i"]))


@app.post("/bid")
async def bid(req: Dict[str, Any]):
    """OpenRTB 2.5 竞价端点（简化字段校验）"""
    s = STATE
    budget: BudgetController = s["budget"]
    req_id = req.get("id", uuid.uuid4().hex[:16])
    if not budget.can_bid():
        return JSONResponse({"id": req_id, "seatbid": [], "nbr": 1})  # 预算耗尽

    imp = (req.get("imp") or [{}])[0]
    floor = float(imp.get("bidfloor", 0.0))
    banner = imp.get("banner") or {}
    device = req.get("device") or {}
    sample = {
        "os": str(device.get("os", "android")).lower(),
        "region": str(device.get("geo", "CN-BJ")),
        "slot_h": int(banner.get("h") or 50),
        "hour_bucket": int(time.time() // 3600 % 24 // 6),
    }

    if s["strategy"] == "ocpc" and s["pctr"]:
        import model
        pctr = model.predict(s["pctr"], sample)
        pcvr = model.predict(s["pcvr"], sample) if s["pcvr"] else 0.0
        # oCPC 目标转化出价：bid_CPM = 目标CPA × pCTR × pCVR × 1000
        # 低价值流量出价自然跌破底价被过滤，预算自动集中到高转化流量
        bid_cpm = s["target_cpa"] * pctr * pcvr * 1000.0
    else:
        pctr = pcvr = 0.0
        bid_cpm = 3.0  # fixed 策略：固定 CPM 出价

    bid_cpm *= _pacing_factor()
    if bid_cpm < floor * 1.05:                     # 低于底价放弃
        return JSONResponse({"id": req_id, "seatbid": [], "nbr": 2})

    s["bids"][req_id] = {"cpm": bid_cpm, "pctr": pctr, "pcvr": pcvr, "ts": time.time()}
    return {
        "id": req_id,
        "seatbid": [{"seat": "dsp-sim", "bid": [{
            "impid": imp.get("id", "1"), "price": round(bid_cpm, 4),
            "adm": f"<sim-ad {sample}>",
            "nurl": f"/win?req_id={req_id}&price={{AUCTION_PRICE}}",
            "cid": "creative-001",
        }]}],
        "cur": "CNY",
    }


def _event(req_id: str, kind: str) -> Dict[str, Any]:
    ctx = STATE["bids"].get(req_id) or {"cpm": 0.0, "pctr": 0.0, "pcvr": 0.0}
    if kind == "win":
        ctx["win"] = True
        STATE["budget"].commit_spend(req_id, ctx["cpm"])
    elif kind == "clk":
        ctx["clk"] = True
    elif kind == "cvr":
        ctx["cvr"] = True
    STATE["bids"][req_id] = ctx
    return {"ok": True, "event": kind}


@app.get("/win")
async def win(req_id: str, price: Optional[float] = None):
    return _event(req_id, "win")


@app.get("/clk")
async def clk(req_id: str):
    return _event(req_id, "clk")


@app.get("/cvr")
async def cvr(req_id: str):
    return _event(req_id, "cvr")


@app.get("/stats")
async def stats():
    s = STATE
    bids = s.get("bids", {})
    wins = [b for b in bids.values() if b.get("win")]
    clicks = sum(1 for b in wins if b.get("clk"))
    convs = sum(1 for b in wins if b.get("cvr"))
    return {
        "strategy": s.get("strategy", "unknown"),
        "n_bids": len(bids),
        "n_wins": len(wins),
        "n_clicks": clicks,
        "n_convs": convs,
        "spent": round(s["budget"].spent, 4),
        "exhausted": s["budget"].is_exhausted,
        "avg_bid": round(sum(b["cpm"] for b in wins) / max(len(wins), 1), 4),
        "avg_pctr": round(sum(b["pctr"] for b in wins) / max(len(wins), 1), 6),
    }


# 模块导入即初始化默认状态，避免服务启动初期 /stats 等端点访问空 STATE
# （导入期不加载模型，run_ab 会显式 configure 加载；独立部署时请先 python model.py）
configure(strategy="fixed", use_model=False)
