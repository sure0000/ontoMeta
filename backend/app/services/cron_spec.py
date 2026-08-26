"""调度表达式（cron）的形态校验——**唯一一处**判据。

为什么需要：任务 Spec 里的 ``refresh_cron`` / ``schedule`` 会被逐字写进生成的 DAG
（``schedule="…"``）。写错一个字段，DAG 在 Airflow 侧 **import 失败**——而 import 失败
是看不见的：ontoMeta 这边回执照样 ok、任务列表照样显示"已提交"，只是那条 DAG 从来
不存在，也就永远不会跑（同类坑见 ``docs`` 里生成模板的花括号事故）。故在建任务的闸门上
拦，报的是"星期字段只能是 0-7"，而不是三天后有人发现表一直没更新。

不引第三方依赖（croniter 未在依赖清单里）：只做**形态**校验，不算下一次触发时间。
形态过了但语义荒谬（如 ``0 0 30 2 *``，2 月 30 日）不在这里判——Airflow 收得下，
只是永远不触发，那属于用户的选择。
"""

from __future__ import annotations

import re

#: Airflow 认的调度预设（`@once` 不含在内：它不是周期，任务要的是周期）。
PRESETS: frozenset[str] = frozenset(
    {"@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually", "@midnight"}
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DOWS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

#: (字段名, 最小值, 最大值, 允许的别名)
_FIELDS: tuple[tuple[str, int, int, tuple[str, ...]], ...] = (
    ("分钟", 0, 59, ()),
    ("小时", 0, 23, ()),
    ("日", 1, 31, ()),
    ("月", 1, 12, _MONTHS),
    ("星期", 0, 7, _DOWS),
)

_STEP_RE = re.compile(r"^(?P<range>[^/]+)(?:/(?P<step>\d+))?$")


class CronError(ValueError):
    """cron 表达式非法，面向用户可读。"""


def _check_atom(atom: str, label: str, low: int, high: int, aliases: tuple[str, ...]) -> None:
    m = _STEP_RE.match(atom)
    if not m:
        raise CronError(f"{label}字段的「{atom}」不是合法取值")
    step = m.group("step")
    if step is not None and int(step) < 1:
        raise CronError(f"{label}字段的步长必须 ≥ 1，收到「{atom}」")
    body = m.group("range")
    if body == "*":
        return

    def value(token: str) -> int:
        token = token.strip().lower()
        if token in aliases:
            return aliases.index(token) + (1 if aliases is _MONTHS else 0)
        if not token.isdigit():
            raise CronError(f"{label}字段的「{token}」不是数字或合法别名")
        return int(token)

    for part in body.split("-"):
        if not part:
            raise CronError(f"{label}字段的「{atom}」不是合法区间")
    bounds = [value(p) for p in body.split("-")]
    if len(bounds) > 2:
        raise CronError(f"{label}字段的「{atom}」区间写法不合法")
    for v in bounds:
        if not low <= v <= high:
            raise CronError(f"{label}字段只能是 {low}-{high}，收到 {v}")
    if len(bounds) == 2 and bounds[0] > bounds[1]:
        raise CronError(f"{label}字段的区间 {atom} 起点大于终点")


def normalize_cron(value: str | None) -> str | None:
    """校验并归一一个调度表达式。空 / 全空白 → ``None``（= 仅手动触发）。

    Raises:
        CronError: 表达式非法。
    """
    text = (value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("@"):
        if lowered not in PRESETS:
            raise CronError(
                f"不认识的调度预设「{text}」；可用：{', '.join(sorted(PRESETS))}，"
                "或写五段 cron（如 0 2 * * *）"
            )
        return lowered
    fields = text.split()
    if len(fields) != 5:
        raise CronError(
            f"cron 须为五段「分 时 日 月 周」，收到 {len(fields)} 段：{text!r}"
        )
    for field, (label, low, high, aliases) in zip(fields, _FIELDS, strict=True):
        for atom in field.split(","):
            if not atom:
                raise CronError(f"{label}字段有空的枚举项：{field!r}")
            _check_atom(atom, label, low, high, aliases)
    return " ".join(fields)
