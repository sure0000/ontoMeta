"""任务级 Flink 执行参数：设置页那份是**默认值**，每个任务可以自己覆盖。

一条全量搬 300 张表的同步、一条常驻的 CDC 流作业、一条几秒钟的指标聚合，对并行度、
YARN 队列、checkpoint 的要求根本不是一回事。把它们钉死在全局设置上，只能二选一：
按最大的那条配（小任务白占集群槽位），或按小的那条配（大任务跑不动）。故这几个参数
下放到 Spec，逐任务可填；**留空 = 跟随设置页**，不是"回退到某个硬编码默认"。

可覆盖的是「这次作业怎么跑」，不是「Flink 装在哪」——``flink_sql_runner_jar`` /
``flink_sql_runner_class`` / ``flink_bin`` 是部署事实（jar 由 ontoMeta 侧随包投递、
flink 命令是 Airflow 主机上的路径），逐任务改只会让投递的 jar 与命令行对不上，
故它们仍只在设置页一处配（见 :mod:`app.services.settings_service`）。

安全：``flink_extra_args`` 与 ``flink_yarn_queue`` 最终会拼进 DAG 里 BashOperator 的
``bash_command``（见 ``airflow_dag_builder._flink_run_command``，纯 ``" ".join``）。
故这里按闭集/正则**严格**校验，含空白或 shell 元字符一律拒绝——不做转义兜底：
能过校验的形态本就不需要转义，过不了的应当在建任务时就报错，而不是到 DAG 运行期
才变成一条谁也看不懂的命令。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.services.airflow_dag_builder import FlinkSubmitConfig

#: 可逐任务覆盖的 Spec 键。字面值与前端 ``specFields.ts`` 的 Flink 字段同名，
#: 也与设置页 extra 的键同名（只差"任务级 vs 全局"这一层语义）。
TASK_FLINK_KEYS: tuple[str, ...] = (
    "flink_parallelism",
    "flink_yarn_queue",
    "flink_deploy_target",
    "flink_checkpoint_dir",
    "flink_extra_args",
)

#: ``flink run -t`` 的取值闭集（与设置页下拉同源）。
DEPLOY_TARGETS: tuple[str, ...] = ("yarn-per-job", "yarn-session", "remote", "local")

_MAX_PARALLELISM = 512  # 与设置页 InputNumber 的上限同值
_MAX_EXTRA_ARGS = 32
_QUEUE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
# checkpoint 目录进 SQL 字面量（SET 'state.checkpoints.dir' = '…'），故连引号一起拒。
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"`;|&<>$()\\]+$")
_EXTRA_ARG_RE = re.compile(r"^-[A-Za-z][A-Za-z0-9_.\-]*(=[^\s'\"`;|&<>$()\\]*)?$")


class FlinkParamError(ValueError):
    """任务级 Flink 参数非法，面向用户可读（建任务/校验闸门处抛）。"""


def _blank(value: Any) -> bool:
    """空值 = 该项没填 = 跟随设置页。空串/空列表与 None 等价。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


def _parallelism(value: Any) -> int:
    try:
        parallelism = int(str(value).strip())
    except (TypeError, ValueError):
        raise FlinkParamError(f"并行度必须是整数，收到 {value!r}") from None
    if not 1 <= parallelism <= _MAX_PARALLELISM:
        raise FlinkParamError(f"并行度须在 1~{_MAX_PARALLELISM} 之间，收到 {parallelism}")
    return parallelism


def _yarn_queue(value: Any) -> str:
    queue = str(value).strip()
    if not _QUEUE_RE.match(queue):
        raise FlinkParamError(
            f"YARN 队列名 {queue!r} 非法：只允许字母/数字/下划线/点/连字符"
        )
    return queue


def _deploy_target(value: Any) -> str:
    target = str(value).strip()
    if target not in DEPLOY_TARGETS:
        raise FlinkParamError(
            f"提交目标 {target!r} 不在可选范围：{', '.join(DEPLOY_TARGETS)}"
        )
    return target


def _checkpoint_dir(value: Any) -> str:
    path = str(value).strip()
    if not _URI_RE.match(path):
        raise FlinkParamError(
            f"Checkpoint 目录 {path!r} 须是带 scheme 的 URI（file:///var/… 或 hdfs://…），"
            "且不含空白与 shell 元字符"
        )
    return path


def _extra_args(value: Any) -> tuple[str, ...]:
    """额外 ``flink run`` 参数。收字符串（按空白切）或列表，逐个按闭集形态校验。"""
    if isinstance(value, str):
        items = value.split()
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        raise FlinkParamError(f"额外参数须是字符串或字符串列表，收到 {type(value).__name__}")
    if len(items) > _MAX_EXTRA_ARGS:
        raise FlinkParamError(f"额外参数最多 {_MAX_EXTRA_ARGS} 项，收到 {len(items)} 项")
    for item in items:
        if not _EXTRA_ARG_RE.match(item):
            raise FlinkParamError(
                f"额外参数 {item!r} 形态非法：须形如 -Dkey=value 或 -flag，"
                "且不含空白与 shell 元字符（; | & $ ` 等）"
            )
    return tuple(items)


_NORMALIZERS = {
    "flink_parallelism": _parallelism,
    "flink_yarn_queue": _yarn_queue,
    "flink_deploy_target": _deploy_target,
    "flink_checkpoint_dir": _checkpoint_dir,
    "flink_extra_args": _extra_args,
}


def normalize(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """校验并归一化任务级覆盖，返回**只含真正填了的键**的 dict。

    Raises:
        FlinkParamError: 任一项形态非法（数值越界 / 不在闭集 / 含 shell 元字符）。
    """
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key in TASK_FLINK_KEYS:
        value = raw.get(key)
        if _blank(value):
            continue  # 没填 = 跟随设置页，不落进 Spec
        out[key] = _NORMALIZERS[key](value)
    return out


def from_spec(
    spec: Mapping[str, Any] | None, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """从 Spec（优先）+ context（兜底）取任务级 Flink 覆盖。

    与执行器里 ``spec.get(k) or context.get(k)`` 的既有口径一致：手工建的独立任务把参数
    填在 Spec 里，链上游派发的任务可由 context 带入。
    """
    merged: dict[str, Any] = {}
    for source in (context or {}, spec or {}):
        for key in TASK_FLINK_KEYS:
            value = source.get(key)
            if not _blank(value):
                merged[key] = value
    return normalize(merged)


def from_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drafter 用：把表单/对话给的 Flink 参数收进 Spec（空的不落键，保持 Spec 干净）。"""
    return normalize(context or {})


def resolve_config(
    airflow: Any,
    params: Mapping[str, Any] | None = None,
    *,
    runner_jar: str | None = None,
    queue_fallback: str = "",
) -> FlinkSubmitConfig:
    """设置页默认值 + 任务级覆盖 → 这次提交真正用的 :class:`FlinkSubmitConfig`。

    Args:
        airflow: ``SettingsService.get_airflow_runtime`` 的运行期配置（默认值来源）。
        params: 任务级覆盖（已 :func:`normalize` 或原始皆可）。
        runner_jar: 覆盖 jar 路径（调用方通常已 strip 过一份，传进来免得再算）。
        queue_fallback: 设置页与任务都没填队列时的兜底值（搬运侧传 ""＝不加 ``-D``，
            计算侧传 "default"，两者行为逐字节保持原样）。
    """
    over = normalize(params) if params else {}
    jar = runner_jar if runner_jar is not None else (airflow.flink_sql_runner_jar or "").strip()
    queue = over.get("flink_yarn_queue") or (airflow.flink_yarn_queue or "").strip() or queue_fallback
    return FlinkSubmitConfig(
        runner_jar=jar,
        runner_class=airflow.flink_sql_runner_class,
        flink_bin=airflow.flink_bin,
        deploy_target=over.get("flink_deploy_target") or airflow.flink_deploy_target,
        parallelism=over.get("flink_parallelism") or airflow.flink_parallelism,
        yarn_queue=queue,
        extra_args=over.get("flink_extra_args", ()),
        checkpoint_dir=resolve_checkpoint_dir(airflow, over),
    )


def resolve_checkpoint_dir(airflow: Any, params: Mapping[str, Any] | None = None) -> str:
    """这次作业用的 checkpoint 目录：任务级优先，其次设置页，都没有则空串。"""
    over = normalize(params) if params else {}
    return over.get("flink_checkpoint_dir") or (airflow.flink_checkpoint_dir or "").strip()


def effective(config: FlinkSubmitConfig, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """回执用：这次**真正生效**的提交参数 + 其中哪些是任务级覆盖的。

    回执不写清用了什么参数，人就只能去翻 DAG 源码反推——而参数现在逐任务不同，
    "设置页写着 4" 已经不再是答案。
    """
    return {
        "deploy_target": config.deploy_target,
        "parallelism": config.parallelism,
        "yarn_queue": config.yarn_queue,
        "checkpoint_dir": config.checkpoint_dir,
        "extra_args": list(config.extra_args),
        "overrides": sorted(normalize(overrides) if overrides else {}),
    }
