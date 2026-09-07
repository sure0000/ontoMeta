"""接数据：目录 · 登记数据源 · 起草本体。

「把一个新库接进来」这条路由 Web 设置页/工作区与 MCP 共同提供。
删掉对话模块，agent 侧就只剩「让用户自己去点」。这里把它做成工具。

两条不能松的边界：

1. **凭据永远不经过 agent**。``create_datasource`` 只建**连接骨架**（名称/类型/catalog），
   DSN、用户名、口令由人在设置页填。一串明文口令若进了工具入参，它就会跟着对话记录、
   trace、审计一起落盘——那不该是一次工具调用造成的后果。所以带凭据的入参会被丢弃并
   如实回报，而不是默默忽略。
2. **域 id 不许编**。``start_ontology_draft`` 的 domain_id 必须来自
   ``list_onboarding_targets``；编一个 id 出来，这次生成只会在别处失败或者更糟——落到
   另一个域上。
"""

from __future__ import annotations

from app.config import settings
from app.models import DomainContext, ObjectType, Ontology, OntologyStatus

from . import AuthContext, ToolResult, register_tool
from ._common import session

#: 可登记的源库类型。Doris 是数仓（唯一执行目标），由运维在设置页配，不从这里建。
_DATASOURCE_KINDS = (
    "mysql", "postgres", "starrocks", "hive", "clickhouse", "sqlite", "duckdb",
)
#: 草稿生成范围 → workspace 的三个入口。
_DRAFT_SCOPES = ("draft", "objects", "relations")
_SCOPE_LABEL = {"draft": "对象+关系", "objects": "业务对象", "relations": "业务关系"}
#: 凭据类入参：出现即丢弃。
_CREDENTIAL_KEYS = frozenset(
    {"dsn", "dsn_secret_ref", "password", "passwd", "username", "user", "secret", "token"}
)


@register_tool
class ListOnboardingTargetsTool:
    """接数据时可选什么：域、数据源、DataHub 状态"""

    name = "list_onboarding_targets"
    required_role = "reader"
    description = (
        "读取接数据时**可选什么**：DataHub 配没配、已同步的数据域（各自草稿/发布状态与对象数）、"
        "已登记的数据源（id/名称/类型/catalog/连通状态）。\n"
        "**接数据开工第一步就调它**：create_datasource 要靠它避免建重复的源，"
        "start_ontology_draft 的 domain_id 必须是这里返回的真实域 id，不得自己编。\n"
        "DataHub 这里只报「配没配」，不去拨测——拨测要走网络，会把一次目录查询变成一次可能"
        "超时的外呼。"
    )
    input_schema = {"type": "object", "properties": {}}

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.models.data_app import DataSource
        from app.services.settings_service import SettingsService

        try:
            with session() as db:
                gms_url = ""
                try:
                    gms_url = (SettingsService().get_datahub_runtime(db).gms_url or "").strip()
                except Exception:  # noqa: BLE001 —— 设置读不出来不该让整个目录查询挂掉
                    gms_url = ""

                draft_statuses = (OntologyStatus.DRAFT.value, OntologyStatus.IN_REVIEW.value)
                domains = []
                for d in db.query(DomainContext).order_by(DomainContext.name.asc()).all():
                    ontos = db.query(Ontology).filter(Ontology.domain_context_id == d.id).all()
                    published = [o for o in ontos if o.status == OntologyStatus.PUBLISHED.value]
                    drafts = [o for o in ontos if o.status in draft_statuses]
                    obj_count = (
                        db.query(ObjectType)
                        .filter(ObjectType.ontology_id == published[0].id)
                        .count()
                        if published
                        else 0
                    )
                    domains.append(
                        {
                            "domain_id": d.id,
                            "name": d.name,
                            "has_published_ontology": bool(published),
                            "published_ontology_id": published[0].id if published else None,
                            "draft_count": len(drafts),
                            "published_object_count": obj_count,
                        }
                    )

                sources = [
                    {
                        "data_source_id": s.id,
                        "name": s.name,
                        "kind": s.kind,
                        "purpose": s.purpose,
                        "catalog_name": s.catalog_name,
                        "status": s.status,
                        "connection_configured": bool((s.dsn_secret_ref or "").strip()),
                    }
                    for s in db.query(DataSource).order_by(DataSource.name.asc()).all()
                ]

                return ToolResult(
                    success=True,
                    data={
                        "datahub_configured": bool(gms_url),
                        "datahub_note": (
                            "元数据采集由 DataHub 侧完成，ontoMeta 只读取已采集的域与表；"
                            "库尚未被 DataHub 采集时，这一步要在 DataHub 配采集，本系统触发不了。"
                        ),
                        "domains": domains,
                        "data_sources": sources,
                    },
                    metadata={"domain_count": len(domains), "datasource_count": len(sources)},
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"读取接数目录失败：{exc}")


@register_tool
class CreateDatasourceTool:
    """登记一个数据源连接骨架（不含凭据）"""

    name = "create_datasource"
    # 建连接是运维动作，且建完要有人去填凭据——publisher。
    required_role = "publisher"
    description = (
        "登记一个新的业务源库连接**骨架**：名称、类型、catalog。\n"
        "**凭据不由你提供**：主机、库、用户名、口令一律由人在 ontoMeta 设置页 → 数据源里填；"
        "入参里出现 dsn/password/username 之类会被丢弃并在返回里告诉你丢了哪些。\n"
        "建出来的源在有人填完连接信息之前**不可用**（`connection_configured=false`），"
        "别拿它去建同步任务。\n"
        "先调 list_onboarding_targets 查重——同名或同 catalog 的源建两遍，后面没人分得清用哪个。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "数据源显示名"},
            "kind": {
                "type": "string",
                "enum": list(_DATASOURCE_KINDS),
                "description": "源库类型",
            },
            "catalog_name": {
                "type": "string",
                "description": "在数仓里的 catalog 名（可选）",
            },
        },
        "required": ["name", "kind"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.models.data_app import DataSource
        from app.services.data_app import DataAppService

        name = str(arguments.get("name") or "").strip()
        if not name:
            return ToolResult(success=False, error="需要 name（数据源显示名）")
        kind = str(arguments.get("kind") or "").strip().lower()
        if kind not in _DATASOURCE_KINDS:
            return ToolResult(
                success=False,
                error=f"kind 须为 {'/'.join(_DATASOURCE_KINDS)}，收到「{kind}」",
                data={"available": list(_DATASOURCE_KINDS)},
            )
        catalog_name = str(arguments.get("catalog_name") or "").strip() or None
        dropped = sorted(k for k in arguments if k.lower() in _CREDENTIAL_KEYS)

        try:
            with session() as db:
                clash = db.query(DataSource).filter(DataSource.name == name).first()
                if clash is not None:
                    return ToolResult(
                        success=False,
                        error=f"已存在同名数据源「{name}」",
                        data={"data_source_id": clash.id, "kind": clash.kind},
                        metadata={"hint": "先看 list_onboarding_targets，别建重复的源"},
                    )
                ds = DataAppService().create_data_source(
                    db,
                    name=name,
                    kind=kind,
                    dsn_secret_ref=None,  # 凭据由人来填，见模块首段
                    catalog_name=catalog_name,
                    purpose="business_source",
                )
                return ToolResult(
                    success=True,
                    data={
                        "data_source_id": ds.id,
                        "name": ds.name,
                        "kind": ds.kind,
                        "catalog_name": ds.catalog_name,
                        "connection_configured": False,
                        "dropped_args": dropped,
                        "next_step": (
                            "请让用户到 ontoMeta 设置页 → 数据源，为这个源填连接信息并测试连接；"
                            "在那之前它不能用于同步任务。"
                        ),
                    },
                    metadata={"written": True, "credentials_required": True},
                )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"登记数据源失败：{exc}")


@register_tool
class StartOntologyDraftTool:
    """为某个数据域启动本体草稿生成"""

    name = "start_ontology_draft"
    # 一次生成要烧几十万 token 并会把 needs_review 重新灌满——publisher。
    required_role = "publisher"
    description = (
        "为某个数据域**启动**本体草稿生成（LLM 跑，异步）。产出的对象/关系仍需人工复核、"
        "再发布。\n"
        "domain_id 必须来自 list_onboarding_targets。scope：draft=对象+关系全量（首次用它）、"
        "objects=只补业务对象、relations=只补业务关系（需已有含对象的草稿）。\n"
        "**该域已有发布本体时要先告诉用户**：重跑会产生新草稿并进入合并流程，不是原地覆盖；"
        "而且会把复核标记重新灌满、把部分发布的门闸打回去。\n"
        "返回 task_id；进度用 get_ops_record(family=\"draft_run\") 回读，不要反复调本工具。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "domain_id": {
                "type": "string",
                "description": "数据域 id（必须来自 list_onboarding_targets）",
            },
            "scope": {
                "type": "string",
                "enum": list(_DRAFT_SCOPES),
                "description": "生成范围",
                "default": "draft",
            },
            "acknowledge_republish": {
                "type": "boolean",
                "description": (
                    "该域已有已发布本体时必须显式给 true——表示你已经把「重跑=产生新草稿走"
                    "合并、复核标记会被重新灌满」告诉用户并得到同意。"
                ),
                "default": False,
            },
        },
        "required": ["domain_id"],
    }

    async def execute(self, arguments: dict, auth: AuthContext) -> ToolResult:
        from app.services.draft_generator import LlmNotConfiguredError
        from app.services.draft_launch import launch_draft_task
        from app.services.draft_task_service import DraftGenerationAlreadyRunning
        from app.services.workspace_service import WorkspaceService

        domain_id = str(arguments.get("domain_id") or "").strip()
        if not domain_id:
            return ToolResult(
                success=False,
                error="需要 domain_id",
                metadata={"hint": "先调 list_onboarding_targets 拿真实域 id，不要自己编"},
            )
        scope = str(arguments.get("scope") or "draft").strip()
        if scope not in _DRAFT_SCOPES:
            return ToolResult(
                success=False,
                error=f"scope 须为 {'/'.join(_DRAFT_SCOPES)}",
                data={"available": list(_DRAFT_SCOPES)},
            )

        workspace = WorkspaceService()
        domain_name = ""
        try:
            with session() as db:
                domain = db.get(DomainContext, domain_id)
                if domain is None:
                    return ToolResult(
                        success=False,
                        error=f"数据域 {domain_id} 不存在",
                        metadata={"hint": "domain_id 必须来自 list_onboarding_targets 的返回"},
                    )
                # 名字在会话内取出来：出了 with 就是 detached 实例，再读属性会炸。
                domain_name = domain.name
                has_published = (
                    db.query(Ontology)
                    .filter(
                        Ontology.domain_context_id == domain_id,
                        Ontology.status == OntologyStatus.PUBLISHED.value,
                    )
                    .first()
                    is not None
                )
                if has_published and not arguments.get("acknowledge_republish"):
                    return ToolResult(
                        success=False,
                        error=(
                            f"「{domain_name}」已有发布本体：重跑会产生新草稿并进入合并流程"
                            "（不是原地覆盖），复核标记会被重新灌满、部分发布的门闸会被打回。"
                            "把这句话告诉用户，得到同意后带 acknowledge_republish=true 再来。"
                        ),
                        data={"domain_id": domain_id, "has_published_ontology": True},
                    )

                starter = {
                    "draft": workspace.start_draft_generation,
                    "objects": workspace.start_object_generation,
                    "relations": workspace.start_relation_generation,
                }[scope]
                try:
                    progress = starter(db, domain_id)
                except LlmNotConfiguredError as exc:
                    return ToolResult(
                        success=False,
                        error=f"LLM 未配置：{exc}",
                        metadata={"hint": "到 ontoMeta 设置页配好模型再来"},
                    )
                except DraftGenerationAlreadyRunning as exc:
                    return ToolResult(
                        success=False,
                        error=f"该域已有草稿生成在跑：{exc}",
                        metadata={"hint": "用 get_ops_record(family=\"draft_run\") 看进度"},
                    )
                except ValueError as exc:
                    return ToolResult(success=False, error=str(exc))

            runner = {
                "draft": workspace._run_draft_generation,
                "objects": workspace._run_object_generation,
                "relations": workspace._run_relation_generation,
            }[scope]
            launch_draft_task(
                progress.task_id, lambda task_id: runner(domain_id, task_id)
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"启动草稿生成失败：{exc}")

        return ToolResult(
            success=True,
            data={
                "task_id": progress.task_id,
                "domain_id": domain_id,
                "scope": scope,
                "status": getattr(progress, "status", None),
                "next_step": (
                    "生成是异步的。用 get_ops_record(family=\"draft_run\") 回读进度；"
                    "跑完后对象/关系仍是草稿，要人在工作区复核、再发布。"
                ),
            },
            metadata={
                "written": True,
                "accepted": True,
                "summary": f"已为「{domain_name}」启动{_SCOPE_LABEL[scope]}草稿生成",
                "subprocess": settings.draft_worker_subprocess,
            },
        )
