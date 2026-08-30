"""Validation Gate：数据标准规范的强制执行点。

标准不是一份文档，而是**代码里的闸门**——每个制品在人工确认**之前**必须过这里，
不过闸门就不能确认、更不能执行。

**判据来源（G1）**：闸门不再自持判据字面值，而是读 ``app.governance.active_standard(db)``
——必填字段、凭据词元、命名约定都由那份**声明式规约**给出。本文件只负责「怎么查」，
「查什么」归规约。这样 agent 提议（读规约当约束）与执行闸门（按规约拦截）咬同一份标准。
enforced 规则（missing_required_field / credential_in_spec）沿用原 code，行为逐字节不变。

复用而非另写：``ValidationIssue`` / ``DraftConsistencyError`` 直接取自
``services/draft_consistency.py``，本体范围的校验直接调用其 ``validate_ontology``。
若在此另立一套 issue 类型，两份校验语义迟早分叉。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.governance import GovernanceStandard, active_standard
from app.governance.lint import lint_spec
from app.models import DataSource, ObjectType, Ontology, Property
from app.services import flink_params
from app.services.draft_consistency import ValidationIssue, validate_ontology
from app.services.ods_naming import ODS_DATABASE
from app.warehouse import UnknownEngineError, get_adapter
from app.warehouse.policy import ALLOWED_EXECUTION_ENGINES, WAREHOUSE_ENGINE, require_doris

# 与规约无关的结构性 warning（引擎未核实、本体一致性）。规约条款的 error/warning
# 由其自身 severity 决定，不在这里维护——见 is_blocking。
_WARNING_CODES = frozenset(
    {
        "engine_unverified",
        "ontology_issue",
        # 提交前自检的**提醒项**（blocking=False）与「自检本身没跑成」——呈现但不拦。
        "preflight_warning",
        "preflight_unavailable",
    }
)


def _standard_warning_codes() -> frozenset[str]:
    """规约里 severity=warning 的条款码——这些进闸门也不阻断确认。"""
    return frozenset(
        r.code for r in active_standard().all_rules() if r.severity == "warning"
    )


def is_blocking(issue: ValidationIssue) -> bool:
    """error 级阻断确认；warning 级（结构性 + 规约声明的 warning 条款）呈现但不阻断。"""
    return issue.code not in _WARNING_CODES and issue.code not in _standard_warning_codes()


def validate_spec(
    db: Session, *, kind: str, spec: dict[str, Any], ontology_id: str | None
) -> list[ValidationIssue]:
    """校验一个治理制品的 Spec。返回问题清单（可能为空）。"""
    issues: list[ValidationIssue] = []

    if not isinstance(spec, dict) or not spec:
        issues.append(
            ValidationIssue(
                code="spec_empty",
                message="Spec 为空或不是对象，无法校验",
                entity_type="artifact",
            )
        )
        return issues

    standard = active_standard(db)
    issues.extend(_check_engines(db, kind, spec))
    issues.extend(_check_flink_params(spec))
    issues.extend(_check_schedule(spec))
    issues.extend(_check_ontology_refs(db, spec, ontology_id))
    issues.extend(_check_required_metadata(kind, spec, standard))
    issues.extend(_check_standard(kind, spec, standard))
    if kind in {"materialize", "sync"}:
        issues.extend(_check_execution_preflight(db, kind, spec, ontology_id))
    if kind == "sync":
        issues.extend(_check_doris_ingestion(db, spec))
    if kind == "transform":
        issues.extend(_check_doris_transform(db, spec))
    if kind == "metric":
        issues.extend(_check_doris_metric(db, spec))

    # 本体范围的制品：连带跑既有的发布前一致性校验（warning 级，不阻断制品本身）。
    if ontology_id and db.query(Ontology).filter(Ontology.id == ontology_id).first():
        issues.extend(_scoped_ontology_issues(db, kind, spec, ontology_id))
    return issues


def _artifact_entities(spec: dict[str, Any]) -> set[str]:
    """这份 Spec 实际碰到的本体实体名。空集 = 碰整个本体（如未裁剪的全量物化）。"""
    names: set[str] = set()
    for key in ("object_type", "target_table"):
        value = spec.get(key)
        if isinstance(value, str) and value:
            names.add(value)
    for key in ("object_types", "subject_objects", "dimension_objects", "selected_targets"):
        for value in spec.get(key) or ():
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _scoped_ontology_issues(
    db: Session, kind: str, spec: dict[str, Any], ontology_id: str
) -> list[ValidationIssue]:
    """本体一致性问题**只留与本制品相关的**，其余折成一条带计数的汇总。

    由来：ERP 本体一次校验产出 188 条，其中 185 条是「某关系表未落地」这类与本次任务
    毫不相干的存量问题。全量抄进每一份制品报告，等于把唯一那条真正该看的（如「指标未绑定
    主对象」）埋进 185 条噪声里——人翻不到，也就不会去看。

    折叠而不是丢弃：本体确实有那些问题，只是不该在这里逐条喊；给个数量和去处即可。
    """
    entities = _artifact_entities(spec)
    scoped: list[ValidationIssue] = []
    unrelated = 0
    for issue in validate_ontology(db, ontology_id):
        # 说不出碰了哪些实体（如全量物化）→ 本体的问题全都算数，一条不折。
        if entities and issue.entity_name and issue.entity_name not in entities:
            unrelated += 1
            continue
        scoped.append(
            ValidationIssue(
                code="ontology_issue",
                message=f"本体一致性：{issue.message}",
                entity_type=issue.entity_type,
                entity_id=issue.entity_id,
                entity_name=issue.entity_name,
            )
        )
    if unrelated:
        scoped.append(
            ValidationIssue(
                code="ontology_issue",
                message=(
                    f"本体一致性：另有 {unrelated} 条问题不涉及本任务的对象，已折叠"
                    "（到本体页统一处理，不影响本任务执行）"
                ),
                entity_type="ontology",
                entity_id=ontology_id,
            )
        )
    return scoped


def _check_flink_params(spec: dict[str, Any]) -> list[ValidationIssue]:
    """任务级 Flink 执行参数（并行度/队列/提交目标/checkpoint/额外 -D）的形态校验。

    这些值会拼进 DAG 里 ``flink run`` 的命令行与流作业的 SQL。放到 DagRun 里才炸，
    人拿到的是一条看不懂的 bash 报错；在闸门上拦，说的是"并行度须在 1~512 之间"。
    """
    try:
        flink_params.normalize(spec)
    except flink_params.FlinkParamError as exc:
        return [
            ValidationIssue(
                code="flink_param_invalid",
                message=f"Flink 执行参数：{exc}",
                entity_type="artifact",
            )
        ]
    return []


def _check_schedule(spec: dict[str, Any]) -> list[ValidationIssue]:
    """调度表达式的形态校验（``schedule`` / ``refresh_cron``）。

    与 Flink 参数同理：这两个值会逐字写进生成的 DAG。写错的 cron 让 Airflow **import
    不了这条 DAG**——而 import 失败在 ontoMeta 这边看不见（回执 ok、状态"已提交"），
    表却永远不更新。故在闸门上拦，说的是"星期字段只能是 0-7"。
    """
    from app.services.cron_spec import CronError, normalize_cron

    issues: list[ValidationIssue] = []
    for key in ("schedule", "refresh_cron"):
        if spec.get(key) in (None, ""):
            continue
        try:
            normalize_cron(str(spec[key]))
        except CronError as exc:
            issues.append(
                ValidationIssue(
                    code="schedule_invalid",
                    message=f"调度频率（{key}）：{exc}",
                    entity_type="artifact",
                    entity_name=key,
                )
            )
    return issues


def _check_engines(db: Session, kind: str, spec: dict[str, Any]) -> list[ValidationIssue]:
    """目标引擎必须存在；未核实能力矩阵的引擎给 warning。"""
    issues: list[ValidationIssue] = []
    engines = spec.get("engines") or ([spec["engine"]] if spec.get("engine") else [])
    # Historical artifacts remain readable until an explicit default Doris is
    # configured. That configuration is the migration cut-over switch for the
    # validation gate; after it, new warehouse work is Doris-only.
    target = db.get(DataSource, spec.get("target_datasource_id")) if spec.get("target_datasource_id") else None
    doris_only = bool(
        (target is not None and target.purpose == "warehouse"
         and target.kind == WAREHOUSE_ENGINE and target.is_default_warehouse and target.enabled)
        or db.query(DataSource).filter(
            DataSource.purpose == "warehouse",
            DataSource.kind == WAREHOUSE_ENGINE,
            DataSource.is_default_warehouse.is_(True),
            DataSource.enabled.is_(True),
        ).first()
    )
    if kind in {"materialize", "transform", "metric"} and not engines and doris_only:
        engines = [WAREHOUSE_ENGINE]
    allowed = ALLOWED_EXECUTION_ENGINES.get(kind) if doris_only else None
    if kind == "sync" and doris_only:
        allowed = frozenset({"doris", "flink"})
    for engine in engines:
        engine_name = str(engine).lower()
        try:
            adapter = get_adapter(engine_name)
        except UnknownEngineError as exc:
            issues.append(
                ValidationIssue(
                    code="engine_unknown",
                    message=str(exc),
                    entity_type="engine",
                    entity_name=str(engine),
                )
            )
            continue
        if allowed and engine_name not in allowed:
            issues.append(
                ValidationIssue(
                    code="engine_forbidden",
                    message=f"{kind} 制品只允许引擎：{', '.join(sorted(allowed))}；收到 {engine_name}",
                    entity_type="engine",
                    entity_name=engine_name,
                )
            )
            continue
        if not adapter.capabilities().verified:
            issues.append(
                ValidationIssue(
                    code="engine_unverified",
                    message=f"引擎 {engine} 的能力矩阵尚未逐项核实，需实施前验证",
                    entity_type="engine",
                    entity_name=str(engine),
                )
            )
    return issues


def _check_doris_metric(
    db: Session, spec: dict[str, Any]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    target_layer = str(spec.get("target_layer") or "ads").strip().lower()
    if target_layer != "ads":
        issues.append(ValidationIssue(
            code="metric_target_layer_forbidden",
            message=f"Doris metric 只能写 ADS 层，不能使用 {target_layer or '空'}",
            entity_type="artifact",
            entity_name="target_layer",
        ))
    target = db.get(DataSource, spec.get("target_datasource_id")) if spec.get("target_datasource_id") else None
    if target is not None and not (
        target.kind == "doris" and target.purpose == "warehouse"
        and target.is_default_warehouse and target.enabled
    ):
        issues.append(ValidationIssue(
            code="metric_target_not_default_doris",
            message="metric/tag/rule 只能在启用的默认 Doris 内执行",
            entity_type="datasource",
            entity_id=str(target.id),
        ))
    forbidden = sorted(key for key in spec if key.startswith("flink_") or key == "execution_mode")
    if forbidden:
        issues.append(ValidationIssue(
            code="metric_flink_params_forbidden",
            message="Doris metric 不接受 Flink/streaming 参数：" + ", ".join(forbidden),
            entity_type="artifact",
        ))
    return issues


def _check_doris_transform(
    db: Session, spec: dict[str, Any]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    target_layer = str(spec.get("target_layer") or "dim").strip().lower()
    if target_layer not in {"dim", "dwd", "dws"}:
        issues.append(ValidationIssue(
            code="transform_target_layer_forbidden",
            message=f"Doris transform 只能写 DIM/DWD/DWS 层，不能使用 {target_layer or '空'}",
            entity_type="artifact",
            entity_name="target_layer",
        ))
    target = db.get(DataSource, spec.get("target_datasource_id")) if spec.get("target_datasource_id") else None
    if target is not None and not (
        target.kind == "doris" and target.purpose == "warehouse"
        and target.is_default_warehouse and target.enabled
    ):
        issues.append(ValidationIssue(
            code="transform_target_not_default_doris",
            message="transform 只能在启用的默认 Doris 内执行",
            entity_type="datasource",
            entity_id=str(target.id),
        ))
    flink_keys = sorted(key for key in spec if key.startswith("flink_") or key in {
        "execution_mode", "source_ref_alias"
    })
    if flink_keys:
        issues.append(ValidationIssue(
            code="transform_flink_params_forbidden",
            message="Doris transform 不接受 Flink/streaming 参数：" + ", ".join(flink_keys),
            entity_type="artifact",
        ))
    issues.extend(_check_transform_sources(db, spec))
    return issues


def _check_transform_sources(
    db: Session, spec: dict[str, Any]
) -> list[ValidationIssue]:
    """多源加工（派生对象）：上游此刻还在不在、表建出来没有。

    在闸门里查而不是等执行：上游被删或还没搬完，SQL 照样生成得出来，跑到 Doris 才报
    「表不存在」——那时任务已经确认过、排进调度了。判据与执行器同源（都问数据集目录
    的 ``source_ready``），不会出现「闸门放行、执行器拒绝」。
    """
    refs = [str(r) for r in (spec.get("source_datasets") or []) if r]
    ontology_id = spec.get("ontology_id")
    if not refs or not ontology_id:
        return []
    from app.services import dataset_catalog

    catalog = {
        entry.ref: entry for entry in dataset_catalog.list_datasets(db, str(ontology_id))
    }
    issues: list[ValidationIssue] = []
    for ref in refs:
        entry = catalog.get(ref)
        if entry is None:
            issues.append(ValidationIssue(
                code="transform_source_missing",
                message=f"加工源「{ref}」已不在本体的数仓落点里，请重建这个任务",
                entity_type="artifact",
                entity_name=ref,
            ))
        elif not entry.source_ready:
            issues.append(ValidationIssue(
                code="transform_source_not_ready",
                message=(
                    f"加工源 {entry.physical}（{entry.entity_display_name}）尚未就绪"
                    f"：{entry.state}"
                ),
                entity_type="artifact",
                entity_name=entry.physical,
            ))
    mapped_refs = {
        str(item.get("from_ref"))
        for item in spec.get("field_mapping") or []
        if item.get("from_ref")
    }
    for ref in sorted(mapped_refs - set(refs)):
        issues.append(ValidationIssue(
            code="transform_field_source_unknown",
            message=f"字段映射引用了不在上游列表里的数据集「{ref}」",
            entity_type="artifact",
            entity_name=ref,
        ))
    return issues


def _check_doris_ingestion(
    db: Session, spec: dict[str, Any]
) -> list[ValidationIssue]:
    """New sync contracts must be business-source → default Doris ODS."""
    target_id = spec.get("target_datasource_id")
    if not target_id:
        return []  # required-metadata policy reports the missing target
    target = db.get(DataSource, str(target_id))
    if target is None or target.purpose != "warehouse":
        return []  # compatibility artifact; execution cannot create a Doris query route
    issues: list[ValidationIssue] = []
    if target.kind != WAREHOUSE_ENGINE or not target.is_default_warehouse or not target.enabled:
        issues.append(ValidationIssue(
            code="sync_target_not_default_doris",
            message="sync 只能写入启用的默认 Doris",
            entity_type="datasource",
            entity_id=str(target_id),
        ))
    source = db.get(DataSource, str(spec.get("source_datasource_id"))) if spec.get("source_datasource_id") else None
    if source is None or source.purpose != "business_source" or not source.enabled:
        issues.append(ValidationIssue(
            code="sync_source_invalid",
            message="sync 必须绑定启用的 business_source DataSource",
            entity_type="datasource",
            entity_id=str(spec.get("source_datasource_id") or ""),
        ))
    # 落点库不是配置项（见 ods_naming.ODS_DATABASE）；这里只兜住存量 Spec 里
    # 写着别的库的老制品——执行时会被改写成 ODS，先在校验里说清楚。
    target_db = str(spec.get("target_ods_database") or ODS_DATABASE)
    if target_db != ODS_DATABASE:
        issues.append(ValidationIssue(
            code="sync_target_not_ods",
            message=f"同步只写 Doris 的 {ODS_DATABASE} 库；该制品记录的是「{target_db}」，请重建任务",
            entity_type="artifact",
            entity_name=target_db,
        ))
    mode = str(spec.get("mode") or "full")
    pks = spec.get("primary_keys") or []
    if mode in {"incremental", "cdc"} and not pks:
        issues.append(ValidationIssue(
            code="sync_primary_key_missing",
            message=f"{mode} 同步必须配置 primary_keys",
            entity_type="artifact",
        ))
    if mode == "incremental":
        if not spec.get("incremental_column"):
            issues.append(ValidationIssue(
                code="sync_incremental_column_missing",
                message="incremental 同步必须配置 incremental_column",
                entity_type="artifact",
            ))
        if spec.get("initial_watermark") in (None, ""):
            issues.append(ValidationIssue(
                code="sync_initial_watermark_missing",
                message="incremental 同步必须配置 initial_watermark",
                entity_type="artifact",
            ))
    if mode == "cdc" and not spec.get("sequence_column"):
        issues.append(ValidationIssue(
            code="sync_sequence_column_missing",
            message="CDC 同步必须配置 sequence_column",
            entity_type="artifact",
        ))
    return issues


def _check_ontology_refs(
    db: Session, spec: dict[str, Any], ontology_id: str | None
) -> list[ValidationIssue]:
    """防 LLM 幻觉：Spec 引用的对象/字段必须在本体中真实存在。

    这是 Gate 最重要的一条——LLM 可能凭空造出不存在的表名或列名，
    若不在此拦下，会一路走到执行器才炸，且可能已产生副作用。
    """
    issues: list[ValidationIssue] = []
    object_names = [str(n) for n in (spec.get("object_types") or []) if n]
    property_names = [str(n) for n in (spec.get("properties") or []) if n]
    if not object_names and not property_names:
        return issues

    if not ontology_id:
        issues.append(
            ValidationIssue(
                code="ontology_missing",
                message="Spec 引用了本体对象，但制品未绑定 ontology_id",
                entity_type="artifact",
            )
        )
        return issues

    known_objects = {
        name
        for (name,) in db.query(ObjectType.name).filter(
            ObjectType.ontology_id == ontology_id
        )
    }
    for name in object_names:
        if name not in known_objects:
            issues.append(
                ValidationIssue(
                    code="unknown_object",
                    message=f"Spec 引用的对象 {name} 不在本体中（疑似 LLM 幻觉）",
                    entity_type="object_type",
                    entity_name=name,
                )
            )

    if property_names:
        known_properties = {
            name
            for (name,) in db.query(Property.name)
            .join(ObjectType, Property.object_type_id == ObjectType.id)
            .filter(ObjectType.ontology_id == ontology_id)
        }
        for name in property_names:
            if name not in known_properties:
                issues.append(
                    ValidationIssue(
                        code="unknown_property",
                        message=f"Spec 引用的字段 {name} 不在本体中（疑似 LLM 幻觉）",
                        entity_type="property",
                        entity_name=name,
                    )
                )
    return issues


# 各制品类型的必填字段与凭据规则，判据来自规约（app.governance）——此处不再自持字面值。
# enforced 规则沿用既有 code（missing_required_field / credential_in_spec），确保接线后
# 拒绝码分布与遥测不变。


def _check_required_metadata(
    kind: str, spec: dict[str, Any], standard: GovernanceStandard
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field in standard.required_metadata.per_artifact.get(kind, ()):
        if not spec.get(field):
            issues.append(
                ValidationIssue(
                    code="missing_required_field",
                    message=f"{kind} 制品缺少必填字段 {field}",
                    entity_type="artifact",
                    entity_name=field,
                )
            )
    # 指标必须绑定主对象：聚合 SQL 的 FROM 需要一张源表，缺失时以往会生成看似合法、
    # 实则无法执行的 `FROM <主对象未绑定>`（遗留1）。在起草期就阻断，不带窟窿执行。
    if kind == "metric" and not (
        spec.get("subject_objects") or spec.get("object_types")
    ):
        issues.append(
            ValidationIssue(
                code="missing_required_field",
                message="metric 制品未绑定主对象（subject_objects/object_types 均为空），"
                "无法确定聚合 SQL 的 FROM 源表",
                entity_type="artifact",
                entity_name="subject_objects",
            )
        )
    # 凭据绝不应出现在 Spec 里——LLM 上下文与制品存储都不得承载密钥。
    # 但 ``*_ref`` / ``*_alias`` 是**指向**密钥存储的引用，正是要鼓励的写法，须放行。
    sec = standard.security
    for key in spec:
        lowered = str(key).lower()
        if lowered.endswith(sec.allowed_ref_suffixes):
            continue
        if any(token in lowered for token in sec.forbidden_tokens):
            issues.append(
                ValidationIssue(
                    code="credential_in_spec",
                    message=f"Spec 中出现疑似凭据字段 {key}：凭据必须独立存储，"
                    f"Spec 只能承载主机别名",
                    entity_type="artifact",
                    entity_name=str(key),
                )
            )
    return issues


def _check_execution_preflight(
    db: Session, kind: str, spec: dict[str, Any], ontology_id: str | None
) -> list[ValidationIssue]:
    """物化/同步的提交前自检并入闸门。

    **为什么在这里**：物化弹窗强制「跑完自检且无阻断项」才让提交，而 Data Agent 那条路
    直接 validate→confirm→execute，同一件破坏性操作走了两套门槛——agent 提的任务会在
    三分钟后的任务日志里失败，而弹窗提的当场就被拦住。闸门是两条路唯一的公共必经点，
    判据放这里两边才守同一条线。

    自检本身跑不起来（缺 target_datasource_id、报错）不算阻断：那属于「没验成」，
    与「验了不通过」是两回事，故给 warning 级并说清原因。
    """
    target_datasource_id = spec.get("target_datasource_id")
    if not ontology_id or not target_datasource_id:
        # 缺这两样另有 missing_required_field 报，不在这里重复喊一遍。
        return []
    from app.services import flink_params
    from app.services.materialize_preflight import run_preflight
    from app.services.materialization_runner import resolve_engine

    is_sync = kind == "sync"
    try:
        report = run_preflight(
            db,
            ontology_id,
            target_datasource_id=str(target_datasource_id),
            engine=resolve_engine(db, str(target_datasource_id), spec.get("engine")),
            selected_targets=(
                [str(spec["object_type"])]
                if is_sync and spec.get("object_type")
                else spec.get("selected_targets")
            ),
            source_datasource_id=(
                str(spec["source_datasource_id"])
                if is_sync and spec.get("source_datasource_id")
                else None
            ),
            source_database=(
                str(spec["source"]).split(".", 1)[0]
                if is_sync and spec.get("source") and "." in str(spec["source"])
                else None
            ),
            managed_connections=True,
            # 搬运侧的自检必须照**本任务 Spec**预演，不能回头读契约：同一张表的契约写着
            # incremental，而这条任务人选的是「全量」——按契约预演出来的是另一个作业，
            # 报的阻断（缺 checkpoint）在真跑里根本不会发生。
            emit="dml" if is_sync else "ddl",
            load_strategy=str(spec["mode"]) if is_sync and spec.get("mode") else None,
            incremental_column=(
                str(spec["incremental_column"])
                if is_sync and spec.get("incremental_column")
                else None
            ),
            initial_watermark=(
                str(spec["initial_watermark"])
                if is_sync and spec.get("initial_watermark")
                else None
            ),
            flink_task_params=flink_params.from_spec(spec) if is_sync else None,
        )
    except Exception as exc:  # noqa: BLE001 — 自检炸了不该把校验一起带走
        return [
            ValidationIssue(
                code="preflight_unavailable",
                message=f"提交前自检未能运行（{exc}）；执行前请自行确认编排环境可用",
                entity_type="artifact",
            )
        ]
    return [
        ValidationIssue(
            code="preflight_blocked" if (item.status == "fail" and item.blocking)
            else "preflight_warning",
            message=f"提交前自检 · {item.label}：{item.detail}"
            + (f"（下一步：{item.next_step}）" if item.next_step else ""),
            entity_type="artifact",
            entity_name=item.key,
        )
        for item in report.items
        if item.status != "pass"
    ]


def _check_standard(
    kind: str, spec: dict[str, Any], standard: GovernanceStandard
) -> list[ValidationIssue]:
    """规约里可在 Spec 层校验的条款（命名 advisory）。

    判据委托 ``governance.lint.lint_spec``——命名规则只有那一处定义（agent 自检与本闸门
    共用同一份），此处只把 ``Violation`` 投影成 ``ValidationIssue``。这些条款 severity=warning，
    经 ``is_blocking`` 判为不阻断——**呈现而非拦截**，避免误伤存量、引入回归。
    """
    return [
        ValidationIssue(
            code=v.code,
            message=v.message,
            entity_type=v.entity_type,
            entity_name=v.entity_name,
        )
        for v in lint_spec(kind, spec, standard)
    ]
