"""建数任务表单：字段骨架 · 真实候选 · context 校验（中性位置，无对话依赖）。

**为什么在这里**：同一张同步表单有两个入口——Web 的任务面板、
MCP 的 `start_task_flow`/`open_task_form`。两处各建一份的话，
同一个同步任务在单发时问四个参数、在流程里只问两个，那不是两种体验，是两套事实。
此前这份骨架长在对话服务上，MCP 需要反向导入对话模块才能发表单；这里把它放在中性位置。

内容：任务类型常量 · context 缺项/校验 · 候选目录（Doris 落点、对象、口径、物化范围）·
四类任务的字段模板 · 预填对齐（`match_option`）。
不含：提案/起草（`app.agents` 的 Drafter）、对话编排、渲染。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import Property

# 数据任务类型白名单——agent 只在「物化/同步/加工」车道出提案；metric 归 propose_draft、
# cluster（基建）不开，避免与口径提案重叠混淆。
ACTION_KINDS: tuple[str, ...] = ("materialize", "sync", "transform", "metric")


ACTION_KIND_LABEL: dict[str, str] = {
    "materialize": "物化", "sync": "同步", "transform": "加工", "metric": "聚合",
}


_CRON_PRESETS: tuple[dict[str, str], ...] = (
    {"expr": "0 2 * * *", "label": "每天 02:00"},
    {"expr": "0 */6 * * *", "label": "每 6 小时"},
    {"expr": "0 * * * *", "label": "每小时"},
    {"expr": "0 3 * * 1", "label": "每周一 03:00"},
    {"expr": "0 4 1 * *", "label": "每月 1 日 04:00"},
    {"expr": "", "label": "不定时（仅手动触发）"},
)


# Flink source→Doris ODS 的三种接入语义。
_LOAD_STRATEGIES: tuple[dict[str, str], ...] = (
    {"value": "full", "label": "全量覆盖",
     "hint": "Flink batch 写 ODS staging，质量检查后 Doris atomic replace；失败不影响正式表"},
    {"value": "incremental", "label": "增量同步",
     "hint": "按 incremental_column + 成功水位做有界 JDBC batch，Doris Unique Key UPSERT"},
    {"value": "cdc", "label": "CDC 变更捕获",
     "hint": "Flink CDC detached 长期作业；必须配置主键、sequence、checkpoint 与 DELETE 策略"},
)


# 候选清单的回灌上限。物化契约会有几百条（一个 734 对象的域即如此），整份倒进上下文
# 既挤爆预算也没人读——按 search_* 的既有约定给 {total, returned, truncated, items}。
_TASK_OPTIONS_LIMIT: int = 30


# 由服务端注入的 context 键——当前会话的本体是确定的，模型不必（也无从）给。
AUTO_ACTION_CONTEXT_KEYS: frozenset[str] = frozenset({"ontology_id"})


ACTION_CONTEXT_HINT = (
    "这些是起草该任务必须先定下、且无法从本体推导的选项。用 MCP flow 把它们做成一张表单"
    "让用户选（候选项用本结果里给出的真实值），拿到回填后再重新生成提案。不要自己编 id。"
)


# 建表单时最多探几个数据源的库列表：每个源一次真实连接，全探会把发表单这一步拖成秒级。
_FORM_DATASOURCE_PROBE_LIMIT: int = 8


# 「数据源 → 库」合并候选的条数上限：一个源上几百个库时下拉本身就没法用了。
_FORM_LOCATION_LIMIT: int = 200


def sync_context_errors(
    db: Session, context: dict[str, Any], *, ontology_id: str | None = None
) -> list[str]:
    """Deterministic mirror of the Doris ODS sync prompt contract."""
    from app.models import DataSource, ObjectType

    errors: list[str] = []
    source_id = context.get("source_datasource_id")
    target_id = context.get("target_datasource_id")
    source = db.get(DataSource, source_id) if source_id else None
    target = db.get(DataSource, target_id) if target_id else None
    if source is not None and (source.purpose != "business_source" or not source.enabled):
        errors.append("source_datasource_id 必须是启用的 business_source")
    if source is not None and ontology_id and context.get("object_type"):
        from app.services.source_datasource import source_datasource_candidates

        obj = (
            db.query(ObjectType)
            .filter(
                ObjectType.ontology_id == ontology_id,
                ObjectType.name == str(context["object_type"]),
            )
            .first()
        )
        if obj is not None:
            allowed = {candidate.id for candidate in source_datasource_candidates(db, obj)}
            if source.id not in allowed:
                errors.append(
                    "source_datasource_id 与所选本体的 source_ref 平台/库/表来源不匹配"
                )
    if target is not None and not (
        target.purpose == "warehouse" and target.kind == "doris"
        and target.is_default_warehouse and target.enabled
        and bool((target.dsn_secret_ref or "").strip())
    ):
        errors.append("target_datasource_id 必须是启用、已配置连接的默认 Doris")
    mode = str(context.get("mode") or "full")
    if mode in {"incremental", "cdc"} and not context.get("primary_keys"):
        errors.append(f"{mode} 必须配置 primary_keys")
    if mode == "incremental":
        for key in ("incremental_column", "initial_watermark"):
            if context.get(key) in (None, ""):
                errors.append(f"incremental 必须配置 {key}")
    if mode == "cdc":
        for key in ("sequence_column", "delete_policy"):
            if context.get(key) in (None, ""):
                errors.append(f"CDC 必须配置 {key}")
        # checkpoint 目录是「这套部署长什么样」的事实：设置页配了全局默认就跟随，不逼每条
        # CDC 任务重填一遍（见 DEVELOPMENT_PRINCIPLES P1「全局配置 ≠ 唯一取值」）。
        # 两处都没有才拦——没有读位点持久化，CDC 作业一重启就从头重搬。
        if context.get("flink_checkpoint_dir") in (None, "") and not settings_checkpoint_dir(db):
            errors.append(
                "CDC 必须配置 flink_checkpoint_dir（或在设置页 → Airflow/Flink 配一个全局默认）"
            )
    return errors


def settings_checkpoint_dir(db: Session) -> str:
    """设置页配的 Flink checkpoint 目录（读不到返回空串，绝不因此炸校验）。"""
    try:
        from app.api.deps import settings_service

        return (settings_service.get_airflow_runtime(db).flink_checkpoint_dir or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def missing_action_context(kind: str, context: dict[str, Any]) -> list[str]:
    """该类任务起草前还缺哪些 context 键。

    判据取 Drafter 自己声明的 ``required_context``（其 ``require_context`` 的同一份字面值），
    不在这里另抄一份——否则两处迟早分叉。注意**不能**改用规约的
    ``required_metadata.per_artifact``：那约束的是 Spec 字段（如 sync 的 source/target），
    由 Drafter 从本体推导，不是调用方要给的 context 键。
    """
    # 局部导入：app.agents 在导入期注册 Drafter/Executor（连带拉起 materialization_runner），
    # 读侧模块不该为一次校验把整条写侧流水线拽进导入图。
    from app.agents import registry

    try:
        drafter = registry.get_drafter(kind)
    except registry.UnregisteredKindError:
        return []
    required = list(drafter.required_context)
    if kind in {"materialize", "transform", "metric"}:
        required.append("target_datasource_id")
    if kind == "materialize":
        required.append("target_database")
    if kind == "metric":
        required.append("business_logic_id")
    if kind == "sync":
        # 落点库不在其中：同步恒写 ODS（ods_naming.ODS_DATABASE），不是调用方的配置项。
        required.extend(("source_datasource_id", "target_datasource_id"))
    return [
        key
        for key in dict.fromkeys(required)
        if key not in AUTO_ACTION_CONTEXT_KEYS and not context.get(key)
    ]


def action_context_candidates(db: Session, missing: list[str]) -> dict[str, Any]:
    """缺失键的真实候选值——只说「缺 target_datasource_id」模型和用户都无从下手。

    只返回选项本身（id/名称/类型/连通状态），凭据不出现（DSN 存的本就是 ``dsn_secret_ref``）。
    """
    from app.models import DataSource

    rows = db.query(DataSource).order_by(DataSource.name).limit(50).all()
    out: dict[str, Any] = {}
    if "source_datasource_id" in missing:
        out["source_datasource_id_options"] = [
            {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status}
            for s in rows if s.purpose == "business_source" and s.enabled
        ]
    if "target_datasource_id" in missing:
        out["target_datasource_id_options"] = [
            {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status}
            for s in rows
            if s.purpose == "warehouse" and s.kind == "doris"
            and s.is_default_warehouse and s.enabled
        ]
    return out


def normalize_form_options(raw: Any) -> list[dict[str, str]]:
    """候选项归一为 ``{label, value}``（可带 disabled）。

    **显示什么和回填什么是两件事**：带 id 的候选（数据源、对象）此前只能写成「名称｜id」，
    那串 id 就直接糊在下拉里给人看。模型给纯字符串时 label = value，行为不变。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"label": text, "value": text})
            continue
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") if item.get("value") is not None else "").strip()
        label = str(item.get("label") or value).strip()
        if not value:
            value = label
        if not label:
            continue
        option: dict[str, Any] = {"label": label[:120], "value": value[:255]}
        if item.get("disabled"):
            option["disabled"] = True
        out.append(option)
    return out


def match_option(field: dict, value: Any) -> Any | None:
    """把一个预填值对到该字段的真实候选上；对不上返回 None。

    对不上就**丢掉**：一个听错的库名若原样落进 default，用户看到的是一张「系统已经替我
    确认过」的表单，而它是错的——错得比空着更贵。
    """
    options = field.get("options") or []
    if not options:
        # 无候选的字段（text/number/cron/…）自由取值，原样采用。
        return value
    text = str(value).strip()
    if not text:
        return None
    if field.get("type") == "autocomplete":
        # 候选是**建议**不是闭集：分区键可以是本体没建模的物理列，对不上也照填。
        for option in options:
            if text in (option["value"], option["label"]):
                return option["value"]
        return text
    for option in options:
        if option.get("disabled"):
            continue  # 选不了的候选不能被预填绕过（如执行侧不支持的装载方式）
        if text == option["value"] or text == option["label"]:
            return option["value"]
    lowered = text.lower()
    for option in options:
        if option.get("disabled"):
            continue
        if lowered in (option["value"].lower(), option["label"].lower()):
            return option["value"]
    # 退到「唯一子串命中」：用户说「落到 dw 库」，候选是「仓库（hive） → dw」。
    hits = [
        o for o in options
        if not o.get("disabled") and (lowered in o["label"].lower() or lowered in o["value"].lower())
    ]
    return hits[0]["value"] if len(hits) == 1 else None


def apply_prefill(fields: list[dict], raw: Any) -> list[str]:
    """把模型读到的「用户已经说过的取值」核对后填成默认值。返回命中的字段名。"""
    if not isinstance(raw, dict):
        return []
    by_name = {f["name"]: f for f in fields}
    hit: list[str] = []
    for name, value in raw.items():
        field = by_name.get(str(name).strip())
        if field is None or value is None or value == "":
            continue
        if field["type"] == "multiselect":
            values = value if isinstance(value, list) else [value]
            matched = [m for m in (match_option(field, v) for v in values) if m is not None]
            if matched:
                field["default"] = matched
                hit.append(field["name"])
            continue
        if isinstance(value, list):
            continue
        matched = match_option(field, value)
        if matched is not None:
            field["default"] = matched
            hit.append(field["name"])
    return hit


def _doris_target_catalog(db: Session) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """任务可选的 Doris warehouse；唯一可执行默认项单独返回。

    所有数仓任务共用这一处，避免 sync/transform/metric/materialize 对“目标能不能选”
    各持一套条件。非默认/停用/缺连接项仍返回供 UI 置灰说明，但绝不能成为 default。
    """
    from app.models import DataSource

    rows = (
        db.query(DataSource)
        .filter(
            DataSource.purpose == "warehouse",
            DataSource.kind == "doris",
        )
        .order_by(DataSource.name, DataSource.id)
        .all()
    )
    items = [
        {
            "id": source.id,
            "name": source.name,
            "kind": source.kind,
            "status": source.status,
            "enabled": source.enabled,
            "is_default": source.is_default_warehouse,
            "has_connection": bool((source.dsn_secret_ref or "").strip()),
            "executable": bool(
                source.enabled
                and source.is_default_warehouse
                and (source.dsn_secret_ref or "").strip()
            ),
        }
        for source in rows
    ]
    return items, next((item for item in items if item["executable"]), None)

def _doris_target_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Doris 目录 → 表单下拉候选；非法项可见但置灰。"""
    options: list[dict[str, Any]] = []
    for target in items:
        if target.get("executable"):
            label = f"{target['name']}（默认 Doris）"
        else:
            reason = (
                "未启用"
                if not target.get("enabled")
                else "未设为默认数仓"
                if not target.get("is_default")
                else "未配置连接"
            )
            label = f"{target['name']}（不可选：{reason}）"
        options.append({
            "label": label,
            "value": target["id"],
            "disabled": not target.get("executable"),
        })
    return options

def _engine_of_datasource(ds: Any) -> str | None:
    """由数据源类型推导物化引擎（DDL/ETL 方言）。仅仓库类型可作物化目标。

    与前端 MaterializeModal.engineOfKind 同口径：数据源 kind 命中已注册引擎即用它。
    """
    from app.warehouse import list_engines

    key = (getattr(ds, "kind", None) or "").lower()
    return key if key in set(list_engines()) else None

def _partition_key_candidates(
    db: Session, ontology_id: str, contracts: list[Any]
) -> list[dict[str, Any]]:
    """整批可用的分区键候选 = **业务属性**，按覆盖的实体数排序。

    分区键是逐表的列名，凭空给一个「全域最常见」的默认值会填进一个这张表根本没有的
    字段（实测就发生过）。故这里不给默认值，只把**真实属性**摆成候选，并如实标注它
    覆盖了几个待物化实体——覆盖不全的键整批用会让没这列的表退化成无谓词追加。

    排序把已被现有契约用作分区键的排在最前：那是人已经认过的选择。
    """
    from app.models import ObjectType

    object_ids = [
        c.target_id for c in contracts if c.target_kind == "object_type"
    ]
    if not object_ids:
        return []
    # 只认本体内的对象，防止契约里残留的陈旧 target_id 把别的域的属性带进来。
    alive = {
        row.id
        for row in db.query(ObjectType.id)
        .filter(ObjectType.ontology_id == ontology_id, ObjectType.id.in_(object_ids))
        .all()
    }
    if not alive:
        return []
    coverage: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    semantic: dict[str, str | None] = {}
    for p in db.query(Property).filter(Property.object_type_id.in_(alive)).all():
        name = (p.name or "").strip()
        if not name:
            continue
        coverage.setdefault(name, set()).add(p.object_type_id)
        display.setdefault(name, p.display_name or name)
        semantic.setdefault(name, p.semantic_type or p.data_type)
    in_use = {
        (c.partition_key or "").strip() for c in contracts if (c.partition_key or "").strip()
    }
    total = len(alive)
    items = [
        {
            "name": name,
            "display_name": display.get(name) or name,
            "semantic_type": semantic.get(name),
            "covers": len(ids),
            "total": total,
            # 已在用的键放在最前：那是人已经认过的选择，不该被一个覆盖面更广的挤下去。
            "in_use": name in in_use,
        }
        for name, ids in coverage.items()
    ]
    items.sort(key=lambda it: (not it["in_use"], -it["covers"], it["name"]))
    return items[:_TASK_OPTIONS_LIMIT]

def _materialize_locations(
    db: Session, sources: list[Any]
) -> list[dict[str, Any]]:
    """逐个可写数据源列出它上面的库，供「某某数据源下的某某库」合并成一次选择。

    物化弹窗里目标库是**选完数据源才去连它列库**的联动下拉；表单是一次性提交、没有
    联动，故把两级摊平成一级：候选本身就是「数据源 → 库」这一对。

    列不出库的源不静默丢掉——记下原因，让它以「手填库名」的形式仍能被选到，否则一个
    连接暂时不通的仓就凭空从候选里消失了。
    """
    from app.services.data_app import DataAppService

    svc = DataAppService()
    out: list[dict[str, Any]] = []
    for s in sources:
        try:
            databases = svc.list_databases(db, s.id)
            error = None
        except Exception as exc:  # noqa: BLE001 — 列不出库只降级为手填，不该中断建数
            databases, error = [], f"列不出该数据源的库（{exc}）"
        out.append({
            "id": s.id,
            "name": s.name,
            "kind": s.kind,
            "engine": _engine_of_datasource(s),
            "databases": list(databases or []),
            "error": error,
        })
    return out

def materialize_options(
        db: Session,
    *,
    ontology_id: str,
    datasource_id: str,
    keyword: str,
    limit: int | None = _TASK_OPTIONS_LIMIT,
) -> tuple[dict, str, bool]:
    from app.models import DataSource, ObjectType
    from app.services.materialization_contract import MaterializationContractService
    from app.warehouse import DEFAULT_ENGINE

    contracts_svc = MaterializationContractService()

    sources = (
        db.query(DataSource)
        .filter(DataSource.purpose == "warehouse", DataSource.kind == "doris")
        .order_by(DataSource.name, DataSource.id)
        .all()
    )
    target_catalog, default_doris = _doris_target_catalog(db)
    datasources = [
        {
            **target,
            "engine": "doris",
            "writable": target["executable"],
        }
        for target in target_catalog
    ]

    # 物化固定写唯一可执行默认 Doris；其它 datasource_id 不参与。
    chosen_id = default_doris["id"] if default_doris else ""
    chosen = next((s for s in sources if s.id == chosen_id), None)
    engine = DEFAULT_ENGINE

    # 目标库：只有指定了数据源才去连它列库。连不上不是错误——弹窗那边也是降级成手填。
    databases: list[str] | None = None
    databases_error: str | None = None
    if chosen is not None:
        from app.services.data_app import DataAppService

        try:
            databases = DataAppService().list_databases(db, chosen.id)
        except Exception as exc:  # noqa: BLE001 — 列不出库只降级为手填，不该中断建数
            databases_error = f"列不出该数据源的库（{exc}）；请让用户手填库名"

    # 候选读取保持**纯读**：尚未持久化契约时，用 derive() 的确定性结果补齐；已有契约
    # （含人工钉住字段）优先。执行入口会按既有流程 sync() 后再生成 DDL。
    rows = contracts_svc.list_contracts(db, ontology_id, materialized_only=True)
    persisted = {(row.target_kind, row.target_id): row for row in rows}
    derived = [item for item in contracts_svc.derive(db, ontology_id) if item["materialized"]]

    from app.models import BusinessLogic, RelationType

    object_names = {
        row.id: (row.name, row.display_name)
        for row in db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id)
    }
    relation_names = {
        row.id: (row.name, row.display_name)
        for row in db.query(RelationType).filter(RelationType.ontology_id == ontology_id)
    }
    logic_names = {
        row.id: (row.name, row.display_name)
        for row in db.query(BusinessLogic).filter(BusinessLogic.ontology_id == ontology_id)
    }
    names_by_kind = {
        "object_type": object_names,
        "relation_type": relation_names,
        "business_logic": logic_names,
    }
    entities: list[dict[str, Any]] = []
    for item in derived:
        key = (item["target_kind"], item["target_id"])
        contract = persisted.get(key)
        name, display = names_by_kind.get(item["target_kind"], {}).get(
            item["target_id"], (None, None)
        )
        if keyword and keyword.lower() not in f"{name or ''}{display or ''}".lower():
            continue
        entities.append({
            "contract_id": contract.id if contract else None,
            "entity": name or item["target_id"],
            "display_name": display,
            "layer": contract.target_layer if contract else item["target_layer"],
            "derived_only": contract is None,
        })
        if limit is not None and len(entities) >= limit:
            break

    configured_database = None
    if default_doris:
        from app.models import DorisWarehouseConfig

        config = (
            db.query(DorisWarehouseConfig)
            .filter(DorisWarehouseConfig.warehouse_datasource_id == default_doris["id"])
            .first()
        )
        configured_database = (
            str(config.default_database).strip()
            if config and config.default_database else None
        )

    result = {
        "kind": "materialize",
        "engine": engine,
        "datasources": datasources,
        "default_doris": default_doris,
        "databases": databases,
        "configured_database": configured_database,
        "databases_error": databases_error,
        "layers": sorted({e["layer"] for e in entities}),
        "entities": entities,
        "total_entities": len(derived),
        "returned": len(entities),
        "truncated": len(entities) < len(derived),
        "usage": (
            "物化只建结构：target_datasource_id 固定为唯一可执行默认 Doris，"
            "target_database 必须来自真实数据库目录；selected_targets 控制范围，空=全部。"
            "装载方式、分区键和调度属于后续同步任务，不在物化任务中配置。"
        ),
    }
    summary = (
        f"可选项：{len(datasources)} 个数据源 / "
        f"{len(databases) if databases is not None else '—'} 个库 / "
        f"{len(derived)} 个待物化实体"
    )
    return result, summary, False

def metric_task_options(
        db: Session,
    *,
    ontology_id: str,
    keyword: str,
    limit: int | None = _TASK_OPTIONS_LIMIT,
) -> tuple[dict, str, bool]:
    from app.models import BusinessLogic, EntityStatus

    q = db.query(BusinessLogic).filter(
        BusinessLogic.ontology_id == ontology_id,
        BusinessLogic.status == EntityStatus.PUBLISHED.value,
        BusinessLogic.expression_json.is_not(None),
    )
    logics = q.order_by(BusinessLogic.name).all()
    if keyword:
        logics = [
            logic for logic in logics
            if keyword.lower() in f"{logic.name}{logic.display_name}".lower()
        ]
    target_datasources, default_doris = _doris_target_catalog(db)
    items = [
        {
            "business_logic_id": logic.id,
            "name": logic.name,
            "display_name": logic.display_name,
            "logic_type": logic.logic_type,
            "suggested_context": (
                {
                    "business_logic_id": logic.id,
                    "target_datasource_id": default_doris["id"],
                }
                if default_doris else None
            ),
        }
        for logic in (logics if limit is None else logics[:limit])
    ]
    return (
        {
            "kind": "metric",
            "target_datasources": target_datasources,
            "default_doris": default_doris,
            "business_logics": items,
            "required_context": ["business_logic_id", "target_datasource_id"],
            "note": "只列已发布且 expression_json 完整的 metric/tag/rule；执行固定写 Doris ADS。",
        },
        f"可选项：{len(items)} 个已形式化业务逻辑",
        False,
    )

def entity_task_options(
        db: Session,
    *,
    kind: str,
    ontology_id: str,
    keyword: str,
    limit: int | None = _TASK_OPTIONS_LIMIT,
) -> tuple[dict, str, bool]:
    """同步/加工的候选对象。

    两者的 Drafter 在没给 object_type / target_table 时会用 ``select_by_intent`` **猜**
    一个对象——把候选摆出来让用户选，猜就不必发生了。
    """
    from app.models import ObjectType
    from app.services.ods_naming import target_ods_table_name
    from app.services.source_ref import (
        has_physical_source,
        is_derived_source_ref,
        source_table_of,
    )

    q = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id)
    rows = q.order_by(ObjectType.name).all()
    transform_ready: set[str] | None = None
    if kind == "transform":
        from app.models import (
            Ontology,
            OntologyWarehouseDeployment,
            WarehouseObjectProjection,
        )

        _targets, transform_doris = _doris_target_catalog(db)
        transform_ready = set()
        # 派生对象没有自己的 ODS projection：它的输入是 DerivedDefinition 里声明的
        # 多张数仓数据集，TransformDrafter/validation 会在起草和校验时检查这些上游。
        # 手动向导也按这个语义允许派生对象，因此候选阶段不能把它们误删掉。
        transform_ready.update(
            o.name for o in rows if is_derived_source_ref(o.source_ref)
        )
        ontology = db.get(Ontology, ontology_id)
        if transform_doris and ontology is not None:
            deployment = (
                db.query(OntologyWarehouseDeployment)
                .filter(
                    OntologyWarehouseDeployment.ontology_id == ontology_id,
                    OntologyWarehouseDeployment.ontology_version == ontology.version,
                    OntologyWarehouseDeployment.doris_datasource_id == transform_doris["id"],
                )
                .first()
            )
            if deployment is not None:
                transform_ready.update({
                    name
                    for (name,) in (
                        db.query(ObjectType.name)
                        .join(
                            WarehouseObjectProjection,
                            WarehouseObjectProjection.object_type_id == ObjectType.id,
                        )
                        .filter(
                            ObjectType.ontology_id == ontology_id,
                            WarehouseObjectProjection.deployment_id == deployment.id,
                            WarehouseObjectProjection.sync_status == "ready",
                            WarehouseObjectProjection.ods_table.is_not(None),
                        )
                        .all()
                    )
                })

    objects: list[dict[str, Any]] = []
    for o in rows:
        if keyword and keyword.lower() not in f"{o.name or ''}{o.display_name or ''}".lower():
            continue
        if transform_ready is not None and o.name not in transform_ready:
            continue
        # 同步要从源表搬。没有物理源表的对象（无 source_ref，或人工建模的 manual: 引用）
        # 定位不到源，不该进候选——它们的去处是物化。
        if kind == "sync" and not has_physical_source(o.source_ref):
            continue
        objects.append({
            "name": o.name,
            "display_name": o.display_name,
            "description": o.description,
            "source_table": source_table_of(o.source_ref),
            "target_ods_table": (
                target_ods_table_name(db, ontology_id, o) if kind == "sync" else None
            ),
        })
        if limit is not None and len(objects) >= limit:
            break

    eligible = (
        len([o for o in rows if has_physical_source(o.source_ref)])
        if kind == "sync"
        else len(transform_ready or set())
        if kind == "transform"
        else len(rows)
    )
    result: dict[str, Any] = {
        "kind": kind,
        # 键名对齐各自 Drafter 认的 context 键，模型照抄即可，不必自己映射。
        "context_key": "object_type" if kind == "sync" else "target_table",
        "objects": objects,
        "total_objects": eligible,
        "returned": len(objects),
        "truncated": len(objects) < eligible,
    }
    if kind == "sync":
        from app.models import DataSource
        from app.services.source_datasource import source_datasource_candidates

        sources = (
            db.query(DataSource)
            .filter(
                DataSource.purpose == "business_source",
                DataSource.enabled.is_(True),
            )
            .order_by(DataSource.name, DataSource.id)
            .all()
        )
        rows_by_name = {o.name: o for o in rows}
        matched_by_object: dict[str, list[DataSource]] = {}
        for item in objects:
            obj = rows_by_name.get(item["name"])
            matched = (
                source_datasource_candidates(db, obj, sources=sources) if obj else []
            )
            matched_by_object[item["name"]] = matched
            item["source_datasources"] = [
                {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status}
                for s in matched
            ]
        target_datasources, default_doris = _doris_target_catalog(db)
        # 顶层保留并集供旧调用方读取；每个对象自己的精确候选在
        # objects[].source_datasources，表单据此随本体选择联动。
        matched_ids = {
            source.id for matched in matched_by_object.values() for source in matched
        }
        result["source_datasources"] = [
            {"id": s.id, "name": s.name, "kind": s.kind, "status": s.status}
            for s in sources if s.id in matched_ids
        ]
        result["target_datasources"] = target_datasources
        result["default_doris"] = default_doris
        result["required_context"] = [
            "object_type", "source_datasource_id", "target_datasource_id", "mode",
        ]
        # 每个本体对象单独推导来源；只有该对象唯一命中时才给可直接复制的推荐 context。
        if default_doris:
            for item in objects:
                matched = matched_by_object.get(item["name"]) or []
                if len(matched) != 1:
                    continue
                item["suggested_context"] = {
                    "object_type": item["name"],
                    "source_datasource_id": matched[0].id,
                    "target_datasource_id": default_doris["id"],
                    # 落点不进 context：Drafter 按 ODS_DATABASE + ods_{数据域}_{原始表名} 固定生成。
                    "mode": "full",
                }
        result["load_strategies"] = [dict(s) for s in _LOAD_STRATEGIES]
        result["cron_presets"] = [dict(c) for c in _CRON_PRESETS]
        result["note"] = (
            "同步只允许 business_source → Flink → 默认 Doris ODS。落点固定为 "
            "ods.ods_{数据域}_{原始表名}，不接受自定义库/表；无 source_ref 的对象已排除；"
            "incremental/CDC 还必须按 load_strategies 的 hint 补齐主键、水位或 sequence/checkpoint。"
            "另外要问清 refresh_cron（调度频率）：入仓作业跑一次不叫管道，"
            "留空只会产出一条手动触发的 DAG。"
        )
    else:
        from app.agents.drafters.transform import SUPPORTED_CLEANSING_RULES

        target_datasources, default_doris = _doris_target_catalog(db)
        result["target_datasources"] = target_datasources
        result["default_doris"] = default_doris
        if default_doris:
            for item in objects:
                item["suggested_context"] = {
                    "target_table": item["name"],
                    "target_datasource_id": default_doris["id"],
                }

        # 清洗规则是**闭集**：Drafter 只认这几条，说不出的需求会被静默丢掉，
        # 故把词表交给模型，让它当场告诉用户哪些做得了。
        result["cleansing_rules"] = [
            {"rule": code, "description": desc} for code, desc in SUPPORTED_CLEANSING_RULES
        ]
        result["note"] = (
            "只列当前默认 Doris 中 ODS Projection 已同步就绪的对象；没有候选时须先建同步任务"
            "（同步自己会幂等建出 ODS 表，不必先单独物化）。"
            "清洗需求只有落到上述规则才会进 Spec，词表外需求不能假装执行。"
        )
    return result, f"可选项：{len(objects)}/{eligible} 个候选对象", False

def task_form_template(
        db: Session,
    *,
    kind: str,
    ontology_id: str,
    datasource_id: str,
    intent: str = "",
) -> list[dict]:
    """建数任务的必问字段骨架，候选取自 get_task_options 的同一份目录。

    取值要能原样回到 context，而表单回填是**纯文本**（无后端会话态，见 P6）——故 id 类
    候选把 id 放进 ``value``，界面只显示 ``label``。此前二者是同一个字符串（``名称｜id``），
    那串 id 就糊在下拉里给人看。

    **与专属界面同构**：物化的这几个控件必须和 MaterializeModal 是同一套事实与同一套
    呈现——目标库跟着数据源走、执行侧不支持的装载方式摆出来但置灰、分区键从业务属性里
    选、调度频率用 cron 选择器。对话里配出来的任务与弹窗里配出来的应当没有差别。
    """
    if kind == "metric":
        return _metric_form_template(db, ontology_id=ontology_id, intent=intent)
    if kind != "materialize":
        return _entity_form_template(
            db, kind=kind, ontology_id=ontology_id, intent=intent
        )
    return _materialize_form_template(
        db, ontology_id=ontology_id, datasource_id=datasource_id, intent=intent
    )

def _entity_form_template(
    db: Session, *, kind: str, ontology_id: str, intent: str = ""
) -> list[dict]:
    """同步 / 加工的字段骨架。

    ``get_task_options`` 给模型的目录仍限制条数以节省上下文；真正给人操作的表单则必须
    带上全部候选，否则 Select 虽然有搜索框，也只能搜到按名称排序后的前几十项。
    """
    opts, _s, err = entity_task_options(
        db, kind=kind, ontology_id=ontology_id, keyword="", limit=None
    )
    if err:
        return []
    raw_objects = opts.get("objects") or []
    # 选项文案只给**业务名**。此前一条选项长这样：
    #   「团队成员介绍（about_us_team_member） · _d71df877e93eac81.tabAbout Us Team
    #    Member → ods_erpnext_tab_about_us_team_member」
    # ——四个技术标识（技术名、源库的哈希名、物理源表、ODS 表名）挤在一行，人要在几百
    # 条这样的字符串里挑一个。要确认的是「同步哪个业务对象」，源表与 ODS 落点是后端
    # 按固定规则派生的结果（ods_naming），不该在选之前就摊一遍；它们在任务详情的
    # 「源表 / 目标表」两行里仍看得到，核对不丢。
    objects = [
        {"label": o.get("display_name") or o["name"], "value": o["name"]}
        for o in raw_objects
    ]
    recommended = None
    if intent and raw_objects:
        from app.agents.common import select_by_intent

        recommended = select_by_intent(
            intent,
            raw_objects,
            key=lambda o: (o.get("name"), o.get("display_name"), o.get("description")),
        )
    is_sync = kind == "sync"
    fields: list[dict] = [{
        "name": "task_requirement",
        "label": "同步任务需求" if is_sync else "加工任务需求",
        "type": "textarea",
        "required": True,
        "default": intent,
        "confirmation_node": "requirement",
    }]
    fields.append({
        "name": opts["context_key"],
        "label": "确认同步本体" if is_sync else "目标表（业务对象）",
        "type": "select",
        "required": True,
        # 搜索匹配的是 label（业务名），placeholder 就照实说，别再承诺搜技术名。
        "placeholder": "搜索对象名称" if is_sync and objects else None,
        **({} if is_sync else {"help": "这里选定的就是最终目标，Drafter 不再按意图猜"}),
        "confirmation_node": "ontology",
        **({"options": objects} if objects else {}),
        **({"default": recommended["name"]} if recommended else {}),
    })
    if is_sync:
        source_options_by_object = {
            o["name"]: [
                {"label": f"{s['name']}（{s['kind']}）", "value": s["id"]}
                for s in o.get("source_datasources") or []
            ]
            for o in raw_objects
        }
        recommended_name = recommended["name"] if recommended else None
        source_options = source_options_by_object.get(recommended_name or "", [])
        default_doris = opts.get("default_doris")
        target_options = _doris_target_options(
            opts.get("target_datasources") or []
        )
        # 落点不是选项：同步就是「源头数据 → 数仓 ODS」，库名恒为
        # ods_naming.ODS_DATABASE、表名恒为 ods_{数据域}_{原始表名}，两者都摆在
        # 上面那个对象下拉的选项文案里（源表 → ODS 表），不必再要人填一个库。
        fields.extend([
            {
                "name": "source_datasource_id", "label": "源数据源",
                "type": "select", "required": True,
                "placeholder": "按所选对象的来源筛出",
                "confirmation_node": "data",
                "depends_on": "object_type",
                "options_by_value": source_options_by_object,
                **({"options": source_options} if source_options else {}),
                **({"default": source_options[0]["value"]} if len(source_options) == 1 else {}),
            },
            {
                "name": "target_datasource_id", "label": "目标数仓",
                "type": "select", "required": True,
                "placeholder": "默认 Doris",
                "confirmation_node": "data",
                # 正常情况不写说明；只有「没有可写的目标」这种挡路的事实才值得占一行。
                **({} if default_doris else {"help": "请先到设置页配置默认 Doris"}),
                **({"options": target_options} if target_options else {}),
                **({"default": default_doris["id"]} if default_doris else {}),
            },
            {
                "name": "mode", "label": "装载方式", "type": "radio",
                "required": True, "default": "full", "confirmation_node": "data",
                "options": [
                    {"label": s["label"], "value": s["value"]} for s in _LOAD_STRATEGIES
                ],
                # 不写说明：三个选项的名字已经说清是哪种同步，而 hint 那几句是给模型
                # 读的实现口径（Flink batch / Doris atomic replace / JDBC 有界批），
                # 摆在人眼前只是一段看不懂的实现细节。选了增量/CDC 之后要填什么，由
                # 随之出现的那几个格子自己说。
            },
            *sync_strategy_fields(db),
            {
                "name": "refresh_cron", "label": "调度频率", "type": "cron",
                "default": "", "confirmation_node": "data",
                "help": "入仓作业跑一次不算管道；留空 = 仅手动触发",
            },
        ])
    else:
        target_catalog, default_doris = _doris_target_catalog(db)
        target_options = _doris_target_options(target_catalog)
        rules = opts.get("cleansing_rules") or []
        fields.extend([
            {
                "name": "target_datasource_id", "label": "目标数仓",
                "type": "select", "required": True,
                "placeholder": "请选择已登记的默认 Doris 数仓",
                "confirmation_node": "data",
                "options": target_options,
                **({} if default_doris else {"help": "请先到设置页配置默认 Doris"}),
                **({"default": default_doris["id"]} if default_doris else {}),
            },
            {
                "name": "target_layer", "label": "目标层", "type": "select",
                "required": True, "default": "dim", "confirmation_node": "data",
                "options": [
                    {"label": "维度层 DIM", "value": "dim"},
                    {"label": "明细层 DWD", "value": "dwd"},
                    {"label": "汇总层 DWS", "value": "dws"},
                ],
            },
            {
                "name": "cleansing_rules", "label": "清洗规则", "type": "multiselect",
                "confirmation_node": "data",
                "options": [
                    {"label": r["description"], "value": r["rule"]} for r in rules
                ],
                "help": "只有这些确定性算子可执行",
            },
            {
                "name": "database_prefix", "label": "库名前缀", "type": "text",
                "confirmation_node": "data",
                "help": "可选；留空使用默认分层库",
            },
            {
                "name": "refresh_cron", "label": "调度频率", "type": "cron",
                "default": "", "confirmation_node": "data",
                "help": "留空 = 仅手动触发",
            },
            {
                "name": "notes", "label": "备注", "type": "textarea",
                "confirmation_node": "data",
                "help": "可选；记录未被清洗规则覆盖的补充要求",
            },
        ])
    return fields

def sync_strategy_fields(db: Session) -> list[dict]:
    """装载方式选了增量/CDC 之后才要填的那几项。

    **为什么必须有**：装载方式那个单选给了三个选项，但表单此前只到那里为止。选「增量
    同步」的人填完整张表单，提交时被 ``sync_context_errors`` 打回「incremental 必须配置
    primary_keys / incremental_column / initial_watermark」——而表单里根本没有这三个格子。
    三选一里两个是死路，等于只有全量能用。

    候选（主键/增量字段/sequence 列）随所选对象实时取（``options_from``），不静态摊进
    表单：一个几百对象的本体，把每个对象的字段全摊开是几 MB 的消息负载。

    ``visible_when`` 决定可见性：全量同步的人不该看到六个填不着的格子——这是**同一张
    表单在三种装载语义下的三副面孔**，不是六个可选项。
    """
    incremental_only = {"field": "mode", "in": ["incremental"]}
    cdc_only = {"field": "mode", "in": ["cdc"]}
    fields: list[dict] = [
        {
            "name": "primary_keys", "label": "业务主键", "type": "multiselect",
            "required": True, "confirmation_node": "data",
            "depends_on": "object_type", "options_from": "object_properties",
            "visible_when": {"field": "mode", "in": ["incremental", "cdc"]},
            "help": "增量/CDC 靠它做 UPSERT 去重；命中 <对象>_id / id 约定的字段已预选，"
                    "没命中就必须自己指定——猜错会让重跑变成插重复行",
        },
        {
            "name": "incremental_column", "label": "增量字段", "type": "select",
            "required": True, "confirmation_node": "data",
            "depends_on": "object_type", "options_from": "object_properties",
            "visible_when": incremental_only,
            "help": "每轮只搬该字段 ≥ 上次成功水位的行；通常是更新时间列",
        },
        {
            "name": "initial_watermark", "label": "初始水位", "type": "text",
            "required": True, "confirmation_node": "data",
            "visible_when": incremental_only,
            "placeholder": "如 2026-01-01 00:00:00",
            "help": "第一次跑从这里开始；之后由每轮成功的水位自动推进",
        },
        {
            "name": "sequence_column", "label": "Sequence 列", "type": "select",
            "required": True, "confirmation_node": "data",
            "depends_on": "object_type", "options_from": "object_properties",
            "visible_when": cdc_only,
            "help": "同一主键的多条变更按它定新旧，避免乱序回放把旧值覆盖成最新",
        },
        {
            "name": "delete_policy", "label": "DELETE 策略", "type": "select",
            "required": True, "default": "ignore", "confirmation_node": "data",
            "visible_when": cdc_only,
            "options": [
                {"label": "忽略删除（源删了 ODS 保留）", "value": "ignore"},
                {"label": "软删除（打标记）", "value": "soft_delete"},
                {"label": "传播删除（ODS 同步删除）", "value": "hard_delete"},
            ],
        },
        # 与手动 Spec 的任务级覆盖保持同一协议；留空表示跟随设置页默认。
        {
            "name": "flink_parallelism", "label": "并行度", "type": "number",
            "confirmation_node": "data", "placeholder": "留空跟随设置页",
            "help": "范围 1~512",
        },
        {
            "name": "flink_yarn_queue", "label": "YARN 队列", "type": "text",
            "confirmation_node": "data", "placeholder": "留空跟随设置页",
        },
        {
            "name": "flink_deploy_target", "label": "提交目标", "type": "select",
            "confirmation_node": "data", "placeholder": "留空跟随设置页",
            "options": [
                {"label": target, "value": target}
                for target in ("yarn-per-job", "yarn-session", "remote", "local")
            ],
        },
        {
            "name": "flink_extra_args", "label": "额外 Flink 参数", "type": "text",
            "confirmation_node": "data",
            "placeholder": "如 -Dtaskmanager.memory.process.size=2g",
            "help": "多个参数用空格分隔；留空跟随设置页",
        },
    ]
    # checkpoint 目录是「这套部署长什么样」：设置页配了就跟随，不逼每条 CDC 任务重填
    # 一遍（见 DEVELOPMENT_PRINCIPLES P1「全局配置 ≠ 唯一取值」）。只有设置页也没有时
    # 才非填不可——没有读位点持久化，CDC 作业一重启就从头重搬。
    if not settings_checkpoint_dir(db):
        fields.append({
            "name": "flink_checkpoint_dir", "label": "Checkpoint 目录", "type": "text",
            "required": True, "confirmation_node": "data",
            "visible_when": cdc_only,
            "placeholder": "如 hdfs:///flink/checkpoints 或 file:///var/flink/ck",
            "help": "CDC 是常驻流作业，读位点存这里；设置页配了全局默认就不必逐条填",
        })
    return fields

def _metric_form_template(
    db: Session, *, ontology_id: str, intent: str = ""
) -> list[dict]:
    """聚合任务：确认需求、形式化口径、默认 Doris 和调度。"""
    catalog, _summary, err = metric_task_options(
        db, ontology_id=ontology_id, keyword="", limit=None
    )
    if err:
        return []
    logics = catalog.get("business_logics") or []
    options = [
        {
            "label": (
                f"{logic['display_name']}（{logic['name']}） · {logic['logic_type']}"
                if logic.get("display_name") and logic["display_name"] != logic["name"]
                else f"{logic['name']} · {logic['logic_type']}"
            ),
            "value": logic["business_logic_id"],
        }
        for logic in logics
    ]
    recommended = None
    if intent and logics:
        from app.agents.common import select_by_intent

        recommended = select_by_intent(
            intent,
            logics,
            key=lambda logic: (logic.get("name"), logic.get("display_name")),
        )
    default_doris = catalog.get("default_doris")
    return [
        {
            "name": "task_requirement", "label": "聚合任务需求", "type": "textarea",
            "required": True, "default": intent, "confirmation_node": "requirement",
        },
        {
            "name": "business_logic_id", "label": "确认业务口径", "type": "select",
            "required": True, "placeholder": "搜索已发布且形式化的指标/标签/规则",
            "confirmation_node": "ontology", "options": options,
            "help": "只能选已形式化的口径",
            **({"default": recommended["business_logic_id"]} if recommended else {}),
        },
        {
            "name": "target_datasource_id", "label": "目标数仓", "type": "select",
            "required": True, "placeholder": "请选择已登记的默认 Doris 数仓",
            "confirmation_node": "data",
            "options": _doris_target_options(catalog.get("target_datasources") or []),
            **({} if default_doris else {"help": "请先到设置页配置默认 Doris"}),
            **({"default": default_doris["id"]} if default_doris else {}),
        },
        {
            "name": "target_layer", "label": "目标层", "type": "select",
            "required": True, "default": "ads", "confirmation_node": "data",
            "options": [{"label": "应用层 ADS", "value": "ads"}],
        },
        {
            "name": "database_prefix", "label": "库名前缀", "type": "text",
            "confirmation_node": "data",
            "help": "可选；留空使用默认 ADS 库",
        },
        {
            "name": "refresh_cron", "label": "调度频率", "type": "cron",
            "default": "", "confirmation_node": "data",
            "help": "留空 = 仅手动触发",
        },
    ]

def _materialize_form_template(
        db: Session,
    *,
    ontology_id: str,
    datasource_id: str,
    intent: str = "",
) -> list[dict]:
    """物化字段：需求 / 契约范围 / 唯一默认 Doris / 真实目标数据库。"""
    catalog, _summary, err = materialize_options(
        db, ontology_id=ontology_id, datasource_id=datasource_id, keyword="", limit=None
    )
    if err:
        return []
    default_doris = catalog.get("default_doris")
    target_options = _doris_target_options(catalog.get("datasources") or [])
    entities = catalog.get("entities") or []
    entity_options = [
        {"label": f"全部契约实体（{len(entities)} 项）", "value": "__all__"},
        *[
        {
            "label": (
                f"{entity.get('display_name')}（{entity['entity']}） · {entity['layer'].upper()}"
                if entity.get("display_name") and entity["display_name"] != entity["entity"]
                else f"{entity['entity']} · {entity['layer'].upper()}"
            ),
            "value": entity["entity"],
        }
        for entity in entities
        ],
    ] if entities else []

    # 需求点名了某个实体，范围就默认成它——而不是「全部契约实体（几百项）」。
    # 「把客户分组物化到数仓」配上一个默认全选的范围，人一路确认下来，最后建的是
    # 整本体几百张表：确认的是 A、执行的是 B。点不准就退回全部（原行为）。
    recommended_entity = None
    if intent and entities:
        from app.agents.common import select_by_intent

        recommended_entity = select_by_intent(
            intent,
            entities,
            key=lambda e: (e.get("entity"), e.get("display_name")),
        )

    databases = list(catalog.get("databases") or [])
    configured_database = catalog.get("configured_database")
    if configured_database and configured_database not in databases:
        databases.insert(0, configured_database)
    database_options = [{"label": name, "value": name} for name in databases]
    fields: list[dict] = [
        {
            "name": "task_requirement", "label": "物化任务需求", "type": "textarea",
            "required": True, "default": intent or "将本体结构物化到默认 Doris",
            "confirmation_node": "requirement",
            "help": "物化只建结构，不搬数据",
        },
        {
            "name": "selected_targets", "label": "确认物化范围",
            "type": "multiselect", "required": True,
            "confirmation_node": "ontology", "options": entity_options,
            "help": (
                "已按需求定位，可继续增删"
                if recommended_entity
                else "默认全部；只物化部分请先删除「全部契约实体」"
                if entity_options
                else "当前本体没有可物化契约实体，请先生成并确认物化契约"
            ),
            **(
                {"default": [recommended_entity["entity"]]}
                if recommended_entity
                else {"default": ["__all__"]}
                if entity_options
                else {}
            ),
        },
        {
            "name": "target_datasource_id", "label": "目标数仓", "type": "select",
            "required": True, "placeholder": "请选择已登记的默认 Doris 数仓",
            "confirmation_node": "data", "options": target_options,
            **({} if default_doris else {"help": "请先到设置页配置默认 Doris"}),
            **({"default": default_doris["id"]} if default_doris else {}),
        },
        {
            "name": "target_database", "label": "目标数据库", "type": "select",
            "required": True, "confirmation_node": "data",
            "placeholder": "请选择默认 Doris 中已存在的数据库",
            "help": (
                "候选来自默认 Doris 实时目录，物化不会自动建库"
                if database_options
                else "读不到默认 Doris 的库目录，请先修复连接；不接受手填"
            ),
            "options": database_options,
            **(
                {"default": configured_database}
                if configured_database and configured_database in databases
                else {"default": database_options[0]["value"]}
                if len(database_options) == 1
                else {}
            ),
        },
    ]

    # 物化只建 DDL，不负责装载。load_strategy / partition_key / refresh_cron 属于同步契约，
    # 不应在物化表单中制造“填了就会生效”的错觉。
    return fields

def _target_location_fields(
    db: Session, *, writable: list[dict], datasource_id: str
) -> list[dict]:
    """「目标数据源 + 目标库」的字段。

    能列出库时合并成**一次**选择（「某某数据源 → 某某库」），因为这两者在物化弹窗里
    本来就是联动的：先选源、再从这个源上列出的库里挑。表单一次性提交、没有联动，两个
    独立下拉就会让人选出「A 源 + B 源上的库」这种根本不存在的组合。

    候选的 ``value`` 直接写成 ``键=值`` 对，故回填文本自解释，模型不必再猜哪段是 id。
    一个库都列不出来时退回两个字段（数据源下拉 + 库名手填）。
    """
    if not writable:
        return [
            {"name": "target_datasource_id", "label": "目标数据源", "type": "text",
             "required": True,
             "help": "尚无可写数据源（未配连接串的源不能作物化目标），请先到 系统设置 → 数据源 配置"},
            {"name": "target_database", "label": "目标库", "type": "text", "required": True,
             "help": "各分层的表都建在这个库里；物化不会自动建库"},
        ]

    # 已定下数据源就只探它，否则探全部可写源（每个源一次连接，故限个数）。
    probe = (
        [d for d in writable if d["id"] == datasource_id] or writable
        if datasource_id
        else writable
    )[:_FORM_DATASOURCE_PROBE_LIMIT]
    from app.models import DataSource

    rows = db.query(DataSource).filter(DataSource.id.in_([d["id"] for d in probe])).all()
    by_id = {r.id: r for r in rows}
    locations = _materialize_locations(
        db, [by_id[d["id"]] for d in probe if d["id"] in by_id]
    )

    options: list[dict] = []
    unreachable: list[str] = []
    for loc in locations:
        if not loc["databases"]:
            unreachable.append(loc["name"])
            continue
        for database in loc["databases"]:
            options.append({
                "label": f"{loc['name']}（{loc['kind']}） → {database}",
                "value": f"target_datasource_id={loc['id']},target_database={database}",
            })
    if not options:
        ds_options = [
            {"label": f"{d['name']}（{d['kind']}）", "value": d["id"]} for d in writable
        ]
        return [
            {
                "name": "target_datasource_id", "label": "目标数据源",
                "type": "select", "required": True,
                "options": ds_options[:50],
                "help": "物化落库的目标仓；引擎由数据源类型决定。未配连接串的源不在候选里",
                **({"default": ds_options[0]["value"]} if len(ds_options) == 1 else {}),
            },
            {
                "name": "target_database", "label": "目标库", "type": "text",
                "required": True,
                "help": "列不出这些源上的库（连接不通或缺驱动），请手填库名；"
                        "各分层的表都建在这个库里，物化不会自动建库",
            },
        ]
    help_text = "选「哪个数据源下的哪个库」；各分层的表都建在这个库里，物化不会自动建库"
    if unreachable:
        help_text += f"。列不出库的源未展开：{'、'.join(unreachable[:3])}"
    return [{
        "name": "target_location", "label": "目标数据源与库", "type": "select",
        "required": True,
        "options": options[:_FORM_LOCATION_LIMIT],
        "help": help_text,
        **({"default": options[0]["value"]} if len(options) == 1 else {}),
    }]

def build_task_form(
        db: Session,
    *,
    kind: str,
    ontology_id: str,
    title: str,
    intent: str = "",
    datasource_id: str = "",
    prefill: dict | None = None,
) -> dict:
    """一个数据任务的**六环确认表单**（骨架字段 + 六环之旅 + 本次确认 id）。

    MCP 交互流程与 Web 任务向导走的是同一张表单——两处各建一份的话，
    同一个同步任务在单发时问四个参数、在链里只问两个，那不是两种体验，是两套事实。
    """
    fields = task_form_template(
        db, kind=kind, ontology_id=ontology_id, datasource_id=datasource_id, intent=intent
    )
    prefilled = apply_prefill(fields, prefill) if prefill else []
    return {
        "title": title[:120],
        "intent": intent[:200],
        "fields": fields,
        "task_kind": kind,
        "ontology_id": ontology_id,
        "confirmation_id": str(uuid.uuid4()),
        "prefilled": prefilled,
    }
