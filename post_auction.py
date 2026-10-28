# -*- coding: utf-8 -*-
"""
post_auction.py — Post-Auction 管线（P2）：IVT 反作弊 + 转化归因

⚠️ 仿真学习用途。对应面试问题："竞价/出价/归因/反作弊链路怎么设计？"

IVT（Invalid Traffic）规则引擎（控制面，离线批处理，不占竞价延迟预算）：
    R1 重复点击   : 同一曝光多次点击只计一次
    R2 速度异常   : win -> click 间隔 < 0.3s 视为机器行为
    R3 频次超限   : 同一用户在窗口内点击数超过频次上限
    R4 分段异常   : 某地域 CTR 超全体均值 3 个标准差，该地域点击作废

归因（Attribution）：
    last-click : 转化 100% 归最后一次有效点击
    linear MTA : 转化在归因窗口内的有效点击间均分
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Dict, List

CLICK_MIN_GAP = 0.05    # R2：最快人工点击间隔（秒；仿真流量时间已压缩，真实建议 0.3s）
FREQ_CAP = 5            # R3：单用户频次上限
ZSCORE_LIMIT = 3.0      # R4：CTR 离群阈值
ATTR_WINDOW = 7 * 86400  # 归因窗口 7 天


def ivt_filter(events: List[dict]) -> Dict[str, Any]:
    """输入事件流（win/clk/cvr），返回有效点击集与封禁明细"""
    clicks = [e for e in events if e["type"] == "clk"]
    wins = {e["req_id"]: e for e in events if e["type"] == "win"}
    blocked: Dict[str, List[str]] = defaultdict(list)   # req_id -> 命中规则
    valid: List[dict] = []

    # R1 + R2 + R3（逐条）
    seen_clk, user_cnt = set(), defaultdict(int)
    for c in sorted(clicks, key=lambda e: e["ts"]):
        rid = c["req_id"]
        if rid in seen_clk:
            blocked[rid].append("R1_dup_click")
            continue
        seen_clk.add(rid)
        w = wins.get(rid)
        if w and c["ts"] - w["ts"] < CLICK_MIN_GAP:
            blocked[rid].append("R2_too_fast")
            continue
        user = c.get("user", "?")
        user_cnt[user] += 1
        if user_cnt[user] > FREQ_CAP:
            blocked[rid].append("R3_freq_cap")
            continue
        valid.append(c)

    # R4 地域 CTR 离群（分段统计）
    by_geo = defaultdict(lambda: [0, 0])  # geo -> [clicks, wins]
    for e in events:
        if e["type"] == "win":
            by_geo[e.get("geo", "?")][1] += 1
    for c in valid:
        by_geo[c.get("geo", "?")][0] += 1
    ctrs = {g: c / max(w, 1) for g, (c, w) in by_geo.items() if w >= 10}
    if len(ctrs) >= 3:
        mu, sd = statistics.mean(ctrs.values()), statistics.pstdev(ctrs.values())
        if sd > 0:
            for c in valid:
                g = c.get("geo", "?")
                if g in ctrs and (ctrs[g] - mu) / sd > ZSCORE_LIMIT:
                    blocked[c["req_id"]].append("R4_geo_outlier")
    bad = {rid for rid, rules in blocked.items() for _ in rules}
    valid = [c for c in valid if c["req_id"] not in bad]

    return {"total_clicks": len(clicks), "valid_clicks": len(valid),
            "blocked": len(clicks) - len(valid),
            "reasons": {r: sum(1 for v in blocked.values() if r in v)
                        for r in ("R1_dup_click", "R2_too_fast", "R3_freq_cap", "R4_geo_outlier")},
            "valid": valid, "by_geo_ctr": {g: round(v, 4) for g, v in ctrs.items()}}


def attribute(events: List[dict], valid_clicks: List[dict]) -> Dict[str, Any]:
    """把转化按 last-click / linear MTA 两种模型归因到流量分段（os）"""
    clicks_by_id = {c["req_id"]: c for c in valid_clicks}
    last_click, linear = defaultdict(float), defaultdict(float)
    for e in (x for x in events if x["type"] == "cvr"):
        c = clicks_by_id.get(e["req_id"])
        if not c:
            continue  # 转化无有效点击（可能被 IVT 拦截）-> 不归因
        seg = c.get("os", "?")
        last_click[seg] += 1.0
    # linear MTA：本 Demo 单转化单点击链路，等价于 last-click；多点击场景按窗口内均分
    for seg, v in last_click.items():
        linear[seg] = v
    return {"last_click": dict(last_click), "linear_mta": dict(linear),
            "window_days": ATTR_WINDOW // 86400,
            "note": "多触点场景下 linear 会在窗口内所有有效点击间均分"}


def run_pipeline(events: List[dict]) -> Dict[str, Any]:
    """一键跑完整 post-auction 管线（供 Agent 控制面与报表调用）"""
    ivt = ivt_filter(events)
    attr = attribute(events, ivt["valid"])
    return {"ivt": {k: v for k, v in ivt.items() if k != "valid"},
            "attribution": attr}
