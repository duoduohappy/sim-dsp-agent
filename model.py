# -*- coding: utf-8 -*-
"""
model.py — pCTR/pCVR 出价模型（P1）

⚠️ 仿真学习用途。支持两种数据来源：
  1. 真实 iPinYou 数据集：将下载的 csv 放到 data/ipinyou/ 目录（含 click 列即可自动识别）
  2. 自动生成 iPinYou 风格合成数据（默认，保证开箱即跑的闭环）

模型：Logistic Regression + One-Hot 特征，输出 pCTR / pCVR。
用法：python model.py  （训练并打印 AUC，模型保存为 pctr_model.pkl / pcvr_model.pkl）
"""
from __future__ import annotations

import glob
import os
import pickle
import random
from datetime import datetime

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(MODEL_DIR, "data", "ipinyou")
N_SYNTH = 20000

OS_POOL = ["android", "ios", "windows"]
REGION_POOL = ["CN-BJ", "CN-SH", "CN-GD", "CN-ZJ", "CN-SC", "CN-JS"]
SLOT_H_POOL = [50, 90, 250, 600]  # 广告位高度档位

FEATURE_FIELDS = ["os", "region", "slot_h", "hour_bucket"]


# ---------------------------------------------------------------------------
# 数据：真实 iPinYou（自动识别 click 列）或合成数据
# ---------------------------------------------------------------------------
def _load_ipinyou_dir() -> list[dict]:
    """尝试解析 data/ipinyou/ 下的 csv/tsv（要求含 click 列）。列名自适应。"""
    files = glob.glob(os.path.join(DATA_DIR, "**", "*.csv"), recursive=True) + \
            glob.glob(os.path.join(DATA_DIR, "**", "*.txt"), recursive=True)
    rows = []
    for fp in files[:3]:  # 最多取3个文件，控制训练规模
        with open(fp, encoding="utf-8", errors="ignore") as f:
            header = None
            first = f.readline().rstrip("\n\r")
            sep = "\t" if "\t" in first else ","
            cols = first.split(sep)
            if any(c.strip().lower() == "click" for c in cols):
                header = [c.strip().lower() for c in cols]
                click_i = header.index("click")
                def map_cols(parts):
                    return {c: parts[i] for i, c in enumerate(header) if i < len(parts)}
            else:  # 无表头：iPinYou 常见格式按位置取（0=click, 8=region 等）
                click_i = 0
                def map_cols(parts):
                    return {"click": parts[0], "useragent": parts[6] if len(parts) > 6 else "",
                            "region": parts[8] if len(parts) > 8 else "",
                            "slotwidth": parts[14] if len(parts) > 14 else "",
                            "slotheight": parts[15] if len(parts) > 15 else ""}
            for line in f:
                parts = line.rstrip("\n\r").split(sep)
                if len(parts) > click_i:
                    rows.append(map_cols(parts))
        if len(rows) >= N_SYNTH:
            break
    if rows:
        print(f"[data] 从 {DATA_DIR} 加载真实 iPinYou 数据 {len(rows)} 行")
    return rows


def _to_sample(r: dict) -> dict:
    """把一行数据规整成统一特征结构"""
    ua = str(r.get("useragent", "")).lower()
    os_name = next((o for o in OS_POOL if o in ua), OS_POOL[0])
    region = str(r.get("region", REGION_POOL[0]))
    try:
        h = int(r.get("slotheight", SLOT_H_POOL[0]))
    except (TypeError, ValueError):
        h = SLOT_H_POOL[0]
    slot_h = min(SLOT_H_POOL, key=lambda x: abs(x - h))
    hour = r.get("hour") or r.get("hour_bucket") or 0
    try:
        hb = int(hour) % 24 // 6
    except (TypeError, ValueError):
        hb = 0
    return {"os": os_name, "region": region, "slot_h": slot_h, "hour_bucket": hb,
            "click": int(float(r.get("click", 0)))}


def _synth_dataset(n: int = N_SYNTH, seed: int = 7) -> list[dict]:
    """生成 iPinYou 风格合成数据：存在可学习的真实模式（os/地域/时段/版位影响 CTR）"""
    rng = random.Random(seed)
    os_w = {"android": 0.0, "ios": 0.6, "windows": -0.5}
    region_w = {r: w for r, w in zip(REGION_POOL, [0.5, 0.3, -0.2, 0.0, -0.4, 0.2])}
    slot_w = {h: w for h, w in zip(SLOT_H_POOL, [-0.3, 0.0, 0.4, 0.2])}
    hour_w = [0.4, -0.3, 0.0, 0.5]  # 4个时段
    base, cvr_base = -3.2, -2.8
    rows = []
    for _ in range(n):
        os_, rg, sl = (rng.choice(p) for p in (OS_POOL, REGION_POOL, SLOT_H_POOL))
        hb = rng.randrange(4)
        z = base + os_w[os_] + region_w[rg] + slot_w[sl] + hour_w[hb]
        pctr = 1 / (1 + np.exp(-z))
        click = int(rng.random() < pctr)
        pcvr = 1 / (1 + np.exp(-(cvr_base + 0.5 * (click and os_ == "ios")))) if click else 0.0
        conv = int(click and rng.random() < pcvr)
        rows.append({"os": os_, "region": rg, "slot_h": sl, "hour_bucket": hb,
                     "click": click, "conv": conv, "pctr_true": pctr})
    print(f"[data] 生成 iPinYou 风格合成数据 {n} 行（真实 CTR 模式可学习）")
    return rows


def load_dataset() -> list[dict]:
    rows = _load_ipinyou_dir()
    if len(rows) >= 1000:
        norm = []
        for r in rows[:N_SYNTH]:
            s = _to_sample(r)
            s.setdefault("conv", 0)
            norm.append(s)
        return norm
    return _synth_dataset()


# ---------------------------------------------------------------------------
# 特征与训练
# ---------------------------------------------------------------------------
def featurize(s: dict) -> dict[str, int]:
    return {f"{k}={s.get(k)}": 1 for k in FEATURE_FIELDS}


def _fit(rows: list[dict], label: str):
    feats = sorted({k for r in rows for k in featurize(r)})
    idx = {f: i for i, f in enumerate(feats)}
    def X(rs):
        m = np.zeros((len(rs), len(feats)))
        for i, r in enumerate(rs):
            for k in featurize(r):
                m[i, idx[k]] = 1
        return m
    y = np.array([r[label] for r in rows])
    Xtr, Xte, ytr, yte = train_test_split(X(rows), y, test_size=0.2, random_state=42)
    clf = LogisticRegression(max_iter=500)
    clf.fit(Xtr, ytr)
    auc = float(roc_auc_score(yte, clf.predict_proba(Xte)[:, 1]))
    return clf, feats, auc


def train_and_save() -> dict:
    rows = load_dataset()
    pctr, feats, auc_c = _fit(rows, "click")
    pcvr, _, auc_v = _fit(rows, "conv")
    with open(os.path.join(MODEL_DIR, "pctr_model.pkl"), "wb") as f:
        pickle.dump({"model": pctr, "feats": feats, "auc": auc_c, "label": "click"}, f)
    with open(os.path.join(MODEL_DIR, "pcvr_model.pkl"), "wb") as f:
        pickle.dump({"model": pcvr, "feats": feats, "auc": auc_v, "label": "conv"}, f)
    print(f"[train] pCTR AUC={auc_c:.4f}  pCVR AUC={auc_v:.4f}")
    return {"pctr_auc": auc_c, "pcvr_auc": auc_v, "n": len(rows)}


def load_model(name: str):
    with open(os.path.join(MODEL_DIR, name), "rb") as f:
        return pickle.load(f)


def predict(bundle: dict, sample: dict) -> float:
    x = np.zeros((1, len(bundle["feats"])))
    f = featurize(sample)
    for k, i in ((k, bundle["feats"].index(k)) for k in f if k in bundle["feats"]):
        x[0, i] = 1
    return float(bundle["model"].predict_proba(x)[0, 1])


if __name__ == "__main__":
    t0 = datetime.now()
    train_and_save()
    print(f"[done] 耗时 {(datetime.now() - t0).total_seconds():.1f}s")
