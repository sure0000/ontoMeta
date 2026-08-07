from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import settings as env_settings
from app.models import (
    AirflowSetting,
    CubeSetting,
    DatahubSetting,
    DraftGenerationSetting,
    LlmServiceConfig,
)
from app.services.common import make_http_client

# 自建/OpenAI 兼容端点(vLLM、Ollama、LM Studio、企业网关等)常无需鉴权：
# 缺 API Key 时用占位符满足 OpenAI SDK 的非空 key 要求。
OPENAI_COMPATIBLE_PROVIDERS = {"openai-compatible"}
OPENAI_COMPATIBLE_PLACEHOLDER_KEY = "EMPTY"

# 产物投递目录属部署基础设施（不在设置页配）：优先用 config 环境变量，
# 缺省落到仓库自带的 docker/orchestration 本地验证栈目录（与 compose 挂载点对齐）。
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _default_dags_dir() -> str:
    return env_settings.airflow_dags_dir or str(
        _REPO_ROOT / "docker" / "orchestration" / "dags"
    )


def _default_jobs_dir() -> str:
    return env_settings.airflow_jobs_dir or str(
        _REPO_ROOT / "docker" / "orchestration" / "seatunnel" / "jobs"
    )


def parse_tool_images(raw: str | None) -> dict[str, str]:
    """``工具名=镜像,工具名=镜像`` → ``{工具名: 镜像}``。格式不对的项直接跳过，不猜。

    设置页存的是这串原文（一个输入框比一张动态表格好填），解析放在读取侧。
    """
    mapping: dict[str, str] = {}
    for item in (raw or "").split(","):
        name, sep, image = item.partition("=")
        if not sep:
            continue
        name, image = name.strip().lower(), image.strip()
        if name and image:
            mapping[name] = image
    return mapping


DEEPSEEK_MODELS = [
    {
        "id": "deepseek-v4-flash",
        "label": "deepseek-v4-flash",
        "description": "DeepSeek-V4-Flash · 高性价比默认模型，1M 上下文，支持思考/非思考模式",
        "deprecated": False,
    },
    {
        "id": "deepseek-v4-pro",
        "label": "deepseek-v4-pro",
        "description": "DeepSeek-V4-Pro · 旗舰模型，1M 上下文，适合复杂推理与 Agent 任务",
        "deprecated": False,
    },
    {
        "id": "deepseek-chat",
        "label": "deepseek-chat",
        "description": "兼容模型 · 等同 V4-Flash 非思考模式（2026/07/24 弃用）",
        "deprecated": True,
    },
    {
        "id": "deepseek-reasoner",
        "label": "deepseek-reasoner",
        "description": "兼容模型 · 等同 V4-Flash 思考模式（2026/07/24 弃用）",
        "deprecated": True,
    },
]


@dataclass
class DatahubRuntimeConfig:
    gms_url: str
    frontend_url: str
    token: str | None
    fabric: str = "PROD"


@dataclass
class LlmRuntimeConfig:
    api_base_url: str
    api_key: str | None
    model: str


@dataclass
class DraftGenerationRuntimeConfig:
    object_chunk_concurrency: int
    relation_chunk_concurrency: int


@dataclass
class AirflowRuntimeConfig:
    """Airflow 编排的运行期配置。``available`` 为假时物化无法执行（报错，不再回退直连）。"""

    endpoint: str
    username: str | None
    password: str | None
    token: str | None
    api_version: str
    # 投递目录在设置页配（空则退默认路径）。
    dags_dir: str
    jobs_dir: str
    # DAG 投递方式与 git-sync 参数（设置页配）：method=local 时 git_* 均忽略；
    # method=git 时落盘后 commit + push 到远程仓，Airflow 侧用 git-sync sidecar 拉取。
    dag_delivery_method: str
    git_remote: str
    git_branch: str
    git_auto_init: bool
    git_author: str
    git_email: str
    # 搬运容器接的 Docker 网络，同属部署基础设施（见 config.airflow_docker_network）。
    docker_network: str
    # JDBC 驱动目录（宿主机路径），挂进搬运容器的 lib 目录。
    drivers_dir: str
    # 搬运工具的镜像覆盖 ``{工具名: 镜像}``（见 config.sync_tool_images）。
    # 无官方镜像的工具（DataX）只有在这里配了才可选。
    sync_tool_images: dict[str, str]
    # 强制指定搬运工具，空 = 自动（见 services/sync_tool_resolver）。物化弹窗不再逐次选。
    sync_tool: str
    # 搬运执行通道与 runner 地址（M14）。channel=runner 时向常驻 runner 发 HTTP，
    # docker_network/drivers_dir/sync_tool_images 这三项只服务 docker 旧通道。
    sync_channel: str
    sync_runner_endpoint: str
    # 调 runner 的 Bearer token（runner 侧设了才需要）。
    sync_runner_token: str | None
    # DAG 形状与时序。dag_parse_timeout 要大于 Airflow 的 dag_dir_list_interval，
    # 否则首次提交必然报「尚未解析到」。
    max_tasks_per_dag: int
    max_active_tasks_per_dag: int
    dag_parse_timeout: float
    preflight_sentinel_timeout: float
    staging_swap: bool
    enabled: bool

    @property
    def available(self) -> bool:
        # 没有投递目录就没法把 DAG 交出去（缺省已由 config 给了），未启用/无 endpoint 也不可用。
        return bool(self.enabled and self.endpoint and self.dags_dir and self.jobs_dir)

    def build_delivery(self):
        """按 dag_delivery_method 构造投递器（local 默认 / git-sync）。

        放在运行期配置上，调用侧（materialization_runner / flink_job_runner）拿到后
        传给 ``DagBundle.write(..., delivery=...)``。
        """
        from app.services.dag_delivery import get_delivery

        return get_delivery(
            self.dag_delivery_method,
            git_remote=self.git_remote,
            git_branch=self.git_branch,
            git_auto_init=self.git_auto_init,
            git_author=self.git_author or None,
            git_email=self.git_email or None,
        )


@dataclass
class CubeRuntimeConfig:
    api_url: str
    api_secret: str | None
    preagg_refresh: str
    tenant_dimension: str | None
    timeout_seconds: int


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


class SettingsService:
    # 进程级缓存：标记默认配置已在数据库中初始化过。
    # 一旦确认存在，后续请求可跳过 ensure_defaults 的探针查询；
    # 当唯一 LLM 行被删除时由 delete_llm_service 重置。
    _defaults_initialized: bool = False

    def __init__(self) -> None:
        # Phase 1：DataHub / Cube / LLM 读取侧改为从统一注册表投影，委托给 DependencyComponentService。
        # Airflow 仍读旧表（编排参数与连接混合，待后续迁移）。
        from app.services.dependency_service import DependencyComponentService
        self._deps = DependencyComponentService()

    def list_llm_models(self) -> list[dict]:
        return DEEPSEEK_MODELS

    def list_llm_services(self, db: Session) -> list[dict]:
        self.ensure_defaults(db)
        return self._deps.list_llm(db)

    def get_llm_service(self, db: Session, service_id: str) -> dict | None:
        self.ensure_defaults(db)
        return self._deps.get_llm(db, service_id)

    def create_llm_service(self, db: Session, data: dict) -> dict:
        self.ensure_defaults(db)
        return self._deps.create_llm(db, data)

    def update_llm_service(self, db: Session, service_id: str, data: dict) -> dict | None:
        self.ensure_defaults(db)
        return self._deps.update_llm(db, service_id, data)

    def delete_llm_service(self, db: Session, service_id: str) -> bool:
        self.ensure_defaults(db)
        ok = self._deps.delete_llm(db, service_id)
        if ok and not self._deps.list_llm(db):
            # 全部 LLM 配置被删除：下次访问需重新初始化默认项
            SettingsService._defaults_initialized = False
        return ok

    def get_datahub_settings(self, db: Session) -> dict:
        self.ensure_defaults(db)
        return self._deps.get_datahub(db)

    def update_datahub_settings(self, db: Session, data: dict) -> dict:
        self.ensure_defaults(db)
        return self._deps.save_datahub(db, data)

    def get_datahub_runtime(self, db: Session) -> DatahubRuntimeConfig:
        self.ensure_defaults(db)
        c = self._deps.get_datahub(db)
        return DatahubRuntimeConfig(
            gms_url=c.get("gms_url", ""),
            frontend_url=c.get("frontend_url", ""),
            token=c.get("token"),
            fabric=c.get("fabric") or "PROD",
        )

    def get_draft_generation_settings(self, db: Session) -> DraftGenerationSetting:
        self.ensure_defaults(db)
        row = db.get(DraftGenerationSetting, "default")
        assert row is not None
        return row

    def update_draft_generation_settings(
        self, db: Session, data: dict
    ) -> DraftGenerationSetting:
        row = self.get_draft_generation_settings(db)
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    def get_draft_generation_runtime(self, db: Session) -> DraftGenerationRuntimeConfig:
        row = self.get_draft_generation_settings(db)
        return DraftGenerationRuntimeConfig(
            object_chunk_concurrency=row.object_chunk_concurrency,
            relation_chunk_concurrency=row.relation_chunk_concurrency,
        )

    def get_airflow_settings(self, db: Session) -> dict:
        self.ensure_defaults(db)
        return self._deps.get_airflow(db)

    def update_airflow_settings(self, db: Session, data: dict) -> dict:
        self.ensure_defaults(db)
        return self._deps.save_airflow(db, data)

    def get_airflow_runtime(self, db: Session) -> AirflowRuntimeConfig:
        self.ensure_defaults(db)
        a = self._deps.get_airflow(db)
        return AirflowRuntimeConfig(
            endpoint=a.get("endpoint", ""),
            username=a.get("username"),
            password=a.get("password"),
            token=a.get("token"),
            api_version=a.get("api_version", "v1"),
            # 投递目录空时退回默认（与既有行为一致）
            dags_dir=a.get("dags_dir") or _default_dags_dir(),
            jobs_dir=a.get("jobs_dir") or _default_jobs_dir(),
            # 投递方式与 git-sync 参数：纯数据库读取（法则：配置只在设置页，不读环境变量）。
            dag_delivery_method=a.get("dag_delivery_method") or "local",
            git_remote=a.get("git_remote") or "origin",
            git_branch=a.get("git_branch") or "main",
            git_auto_init=bool(a.get("git_auto_init")),
            git_author=a.get("git_author") or "",
            git_email=a.get("git_email") or "",
            docker_network=a.get("docker_network") or "bridge",
            drivers_dir=a.get("drivers_dir") or "",
            sync_tool_images=parse_tool_images(a.get("sync_tool_images")),
            sync_tool=(a.get("sync_tool") or "").strip().lower(),
            sync_channel=a.get("sync_channel") or "runner",
            sync_runner_endpoint=a.get("sync_runner_endpoint") or "",
            sync_runner_token=a.get("sync_runner_token") or None,
            max_tasks_per_dag=a.get("max_tasks_per_dag") or 50,
            max_active_tasks_per_dag=a.get("max_active_tasks_per_dag") or 16,
            dag_parse_timeout=a.get("dag_parse_timeout") or 60.0,
            preflight_sentinel_timeout=a.get("preflight_sentinel_timeout") or 20.0,
            staging_swap=a.get("staging_swap") if a.get("staging_swap") is not None else True,
            enabled=a.get("enabled", False),
        )

    def get_cube_settings(self, db: Session) -> dict:
        self.ensure_defaults(db)
        return self._deps.get_cube(db)

    def update_cube_settings(self, db: Session, data: dict) -> dict:
        self.ensure_defaults(db)
        return self._deps.save_cube(db, data)

    def get_cube_runtime(self, db: Session) -> CubeRuntimeConfig:
        self.ensure_defaults(db)
        c = self._deps.get_cube(db)
        return CubeRuntimeConfig(
            api_url=c.get("api_url", ""),
            api_secret=c.get("api_secret"),
            preagg_refresh=c.get("preagg_refresh", "1 hour"),
            tenant_dimension=(c.get("tenant_dimension") or None),
            timeout_seconds=int(c.get("timeout_seconds", 30) or 30),
        )

    def get_llm_runtime(self, db: Session) -> LlmRuntimeConfig:
        self.ensure_defaults(db)
        svc = self._deps.get_default_llm(db)
        if svc:
            provider = (svc.get("provider") or "").lower()
            keyless_ok = provider in OPENAI_COMPATIBLE_PROVIDERS
            # 自建 OpenAI 兼容端点允许无 Key 直连（用占位符满足 SDK 非空要求）。
            api_key = svc.get("api_key") or (
                OPENAI_COMPATIBLE_PLACEHOLDER_KEY if keyless_ok else None
            )
            return LlmRuntimeConfig(
                api_base_url=svc.get("api_base_url", ""),
                api_key=api_key,
                model=svc.get("model", ""),
            )
        return LlmRuntimeConfig(
            api_base_url="https://api.deepseek.com",
            api_key=env_settings.openai_api_key,
            model=env_settings.openai_model,
        )

    def test_llm_connection(self, db: Session, data: dict) -> dict:
        """真实拨测一个 LLM 配置：发一次最小 chat 请求，返回连通性与耗时。

        编辑态表单常留空 api_key(表示保持原值)，此时用 service_id 取回已存密钥；
        自建 OpenAI 兼容端点无鉴权时用占位符 Key 直连。
        """
        provider = (data.get("provider") or "").lower()
        keyless_ok = provider in OPENAI_COMPATIBLE_PROVIDERS

        api_key = (data.get("api_key") or "").strip() or None
        if not api_key and data.get("service_id"):
            existing = self._deps.get_llm(db, data["service_id"])
            if existing and existing.get("api_key"):
                api_key = existing["api_key"]
        if not api_key:
            if keyless_ok:
                api_key = OPENAI_COMPATIBLE_PLACEHOLDER_KEY
            else:
                return {"ok": False, "message": "未配置 API Key，无法测试真实连接"}

        base_url = (data.get("api_base_url") or "").strip()
        model = (data.get("model") or "").strip()
        if not base_url or not model:
            return {"ok": False, "message": "缺少模型或 API 地址"}

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=min(env_settings.llm_timeout_seconds, 30),
            max_retries=0,
            http_client=make_http_client(),
        )
        start = perf_counter()
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 —— 任何异常都视为拨测失败并回显简要原因
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:300]}
        latency_ms = int((perf_counter() - start) * 1000)
        return {"ok": True, "message": "连接成功", "latency_ms": latency_ms, "model": model}

    def ensure_defaults(self, db: Session) -> None:
        # 进程级缓存命中时直接跳过，避免每个请求都跑两个探针查询。
        if SettingsService._defaults_initialized:
            return

        if not db.get(DatahubSetting, "default"):
            db.add(
                DatahubSetting(
                    id="default",
                    gms_url=env_settings.datahub_gms_url,
                    frontend_url=env_settings.datahub_frontend_url,
                    token=env_settings.datahub_token,
                )
            )
            db.commit()

        if db.query(LlmServiceConfig).count() == 0:
            db.add(
                LlmServiceConfig(
                    name="DeepSeek 默认",
                    provider="deepseek",
                    api_base_url="https://api.deepseek.com",
                    api_key=env_settings.openai_api_key,
                    model="deepseek-v4-flash",
                    is_default=True,
                    enabled=True,
                )
            )
            db.commit()

        if not db.get(DraftGenerationSetting, "default"):
            db.add(
                DraftGenerationSetting(
                    id="default",
                    object_chunk_concurrency=env_settings.draft_chunk_max_concurrency,
                    relation_chunk_concurrency=env_settings.draft_relation_chunk_max_concurrency,
                )
            )
            db.commit()

        if not db.get(CubeSetting, "default"):
            # 首次从环境变量播种一次；此后以 DB（设置页）为权威
            db.add(
                CubeSetting(
                    id="default",
                    api_url=env_settings.cube_api_url,
                    api_secret=env_settings.cube_api_secret,
                    preagg_refresh=env_settings.cube_preagg_refresh,
                    tenant_dimension=env_settings.cube_tenant_dimension,
                    timeout_seconds=int(env_settings.cube_timeout_seconds),
                )
            )
            db.commit()

        if not db.get(AirflowSetting, "default"):
            # 默认不启用：需在设置页填 endpoint 并启用后才能物化（物化一律走 Airflow 编排）。
            # **环境变量只在这里播一次种**：编排配置此后以这一行为准，改 .env 不再生效。
            # 这样已有部署升级时行为不变，又不会留下两个互相打架的事实源。
            db.add(
                AirflowSetting(
                    id="default",
                    dags_dir=_default_dags_dir(),
                    jobs_dir=_default_jobs_dir(),
                    sync_channel=env_settings.sync_channel or "runner",
                    sync_runner_endpoint=env_settings.sync_runner_endpoint,
                    docker_network=env_settings.airflow_docker_network,
                    drivers_dir=env_settings.airflow_sync_drivers_dir,
                    sync_tool_images=env_settings.sync_tool_images,
                    max_tasks_per_dag=env_settings.ontometa_max_tasks_per_dag,
                    max_active_tasks_per_dag=env_settings.ontometa_max_active_tasks_per_dag,
                    dag_parse_timeout=env_settings.ontometa_dag_parse_timeout,
                    preflight_sentinel_timeout=env_settings.ontometa_preflight_sentinel_timeout,
                    staging_swap=env_settings.ontometa_staging_swap,
                )
            )
            db.commit()

        SettingsService._defaults_initialized = True

        # Phase 1：把旧表（DatahubSetting/CubeSetting/LlmServiceConfig）搬进统一注册表。
        # 幂等：已存在对应行则跳过。此后 DataHub/Cube/LLM 读取侧只认注册表。
        self._deps.migrate_from_legacy(db)

    def _clear_default_llm(self, db: Session) -> None:
        for item in db.query(LlmServiceConfig).filter(LlmServiceConfig.is_default.is_(True)).all():
            item.is_default = False
