"""Slice 2 —— 置信度冷启动校准器（离线，不进热路径）。

目标：把 role 分类器输出的 `role_confidence` 校准到**真实人工采纳率**上——让
「0.8」真的约等于「人工 80% 会采纳」，替代 `object_classifier._score_to_confidence`
的死映射。方法用 Platt scaling（1D 逻辑回归，少参不易过拟合）。

弱标签来自现有治理痕迹（无需新标注）：
- `deleted_by_user=True` → 该判定被拒（负）。
- `overridden_fields` 含 `table_role` → 角色被人工改过（负）。
- `origin∈{machine,machine_edited}` 且 `status=published` 且角色未被改 → 采纳（正）。
- 其余（suggested/未复核）→ 无标签，跳过。

**冷启动铁律**：真实人工复核信号不足（样本太少或只有单一类别）时**不拟合**，返回
恒等校准并置 `cold_start=True`，绝不用管道噪声（如全量同组 overridden 字段）拟出
垃圾映射。校准产物默认**不接入热路径**，由上层在确有真实复核数据后显式启用。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

# 触发真实拟合所需的最小弱标签数（且两类都要有）。不足则冷启动恒等。
_MIN_SAMPLES = 50
_ROLE_FIELD_KEYS = ("table_role", "role", "role_confidence", "role_reason")


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass
class WeakLabel:
    predicted: float  # 模型给的 role_confidence
    correct: bool  # 人工是否采纳该角色判定
    source: str = ""


@dataclass
class ReliabilityBin:
    lo: float
    hi: float
    count: int
    pred_mean: float
    empirical: float  # 该桶真实采纳率


class Calibrator:
    """校准器基类（恒等）。"""

    cold_start = True

    def calibrate(self, p: float) -> float:
        return max(0.0, min(1.0, p))


@dataclass
class PlattCalibrator(Calibrator):
    """校准后概率 = sigmoid(w * p + b)。"""

    w: float
    b: float
    cold_start: bool = False

    def calibrate(self, p: float) -> float:
        return _sigmoid(self.w * p + self.b)


@dataclass
class CalibrationReport:
    calibrator: Calibrator
    n: int
    ece: float
    bins: list[ReliabilityBin] = field(default_factory=list)
    cold_start: bool = True
    note: str = ""


def _parse_overridden(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if isinstance(data, dict):
        return set(data.keys())
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def collect_role_weak_labels(db) -> list[WeakLabel]:
    """从 ObjectType 治理痕迹提取 role 判定的弱标签。"""
    from app.models import ObjectType

    labels: list[WeakLabel] = []
    rows = (
        db.query(ObjectType)
        .filter(ObjectType.role_confidence.isnot(None))
        .all()
    )
    for ot in rows:
        conf = ot.role_confidence
        if conf is None:
            continue
        if ot.deleted_by_user:
            labels.append(WeakLabel(conf, False, "deleted"))
            continue
        overridden = _parse_overridden(ot.overridden_fields)
        role_overridden = bool(overridden & set(_ROLE_FIELD_KEYS))
        origin = (ot.origin or "").lower()
        status = (ot.status or "").lower()
        if origin in ("machine", "machine_edited") and status == "published":
            labels.append(WeakLabel(conf, not role_overridden,
                                    "override" if role_overridden else "adopted"))
        # suggested / 未发布 / 未复核：无采纳信号，跳过。
    return labels


def fit_platt(labels: list[WeakLabel], *, iters: int = 500, lr: float = 0.1) -> PlattCalibrator:
    """1D 逻辑回归拟合 sigmoid(w*p + b)。梯度下降，少参稳定。"""
    w, b = 1.0, 0.0
    n = len(labels)
    if n == 0:
        return PlattCalibrator(1.0, 0.0, cold_start=True)
    xs = [l.predicted for l in labels]
    ys = [1.0 if l.correct else 0.0 for l in labels]
    for _ in range(iters):
        gw = gb = 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(w * x + b)
            err = p - y
            gw += err * x
            gb += err
        w -= lr * gw / n
        b -= lr * gb / n
    return PlattCalibrator(round(w, 4), round(b, 4), cold_start=False)


def reliability_bins(
    labels: list[WeakLabel],
    calibrator: Calibrator | None = None,
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """把预测（或校准后）概率分桶，算每桶均值预测 vs 真实采纳率。"""
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for lab in labels:
        p = calibrator.calibrate(lab.predicted) if calibrator else lab.predicted
        idx = min(int(p * n_bins), n_bins - 1)
        buckets[idx].append((p, lab.correct))
    out: list[ReliabilityBin] = []
    for i, items in enumerate(buckets):
        if not items:
            continue
        preds = [p for p, _ in items]
        emp = sum(1 for _, c in items if c) / len(items)
        out.append(
            ReliabilityBin(
                lo=i / n_bins,
                hi=(i + 1) / n_bins,
                count=len(items),
                pred_mean=round(sum(preds) / len(preds), 4),
                empirical=round(emp, 4),
            )
        )
    return out


def expected_calibration_error(
    labels: list[WeakLabel],
    calibrator: Calibrator | None = None,
    n_bins: int = 10,
) -> float:
    """ECE：各桶 |均值预测 - 真实采纳率| 按样本占比加权求和。"""
    bins = reliability_bins(labels, calibrator, n_bins)
    n = sum(b.count for b in bins)
    if n == 0:
        return 0.0
    return round(
        sum(b.count / n * abs(b.pred_mean - b.empirical) for b in bins), 4
    )


def build_calibration(db, *, min_samples: int = _MIN_SAMPLES) -> CalibrationReport:
    """端到端：采集弱标签→（够量且两类齐全才）拟合→出 reliability/ECE。

    冷启动（数据不足/单一类别）返回恒等校准 + cold_start=True，不拟合噪声。
    """
    labels = collect_role_weak_labels(db)
    n = len(labels)
    n_pos = sum(1 for l in labels if l.correct)
    n_neg = n - n_pos
    if n < min_samples or n_pos == 0 or n_neg == 0:
        cal = Calibrator()
        return CalibrationReport(
            calibrator=cal,
            n=n,
            ece=expected_calibration_error(labels) if n else 0.0,
            bins=reliability_bins(labels) if n else [],
            cold_start=True,
            note=(
                f"冷启动：弱标签 {n}（正{n_pos}/负{n_neg}），不足 {min_samples} 或类别单一，"
                "保持恒等映射，待真实人工复核积累后再拟合。"
            ),
        )
    cal = fit_platt(labels)
    ece_before = expected_calibration_error(labels, None)
    ece_after = expected_calibration_error(labels, cal)
    return CalibrationReport(
        calibrator=cal,
        n=n,
        ece=ece_after,
        bins=reliability_bins(labels, cal),
        cold_start=False,
        note=f"已拟合 Platt（w={cal.w}, b={cal.b}）；ECE {ece_before}→{ece_after}。",
    )
