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

# 产物投递目录是部署路径（config-web-only 法则允许读环境变量的少数 bootstrap 例外）：
# 只在 ensure_defaults 给新库播种 Airflow 设置行时读一次 env，缺省落到仓库自带的
# docker/orchestration 本地验证栈目录（与 compose 挂载点对齐）。运行期一律纯数据库读取。
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _default_dags_dir() -> str:
    return env_settings.airflow_dags_dir or str(
        _REPO_ROOT / "docker" / "orchestration" / "dags"
    )



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
    request_timeout: float = 90.0


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

    # ---- 连接一：调度 API（触发 DagRun、回读状态）----
    # REST 版本不入配置：客户端按 v1 起步、404 时自协商（见 connectors/airflow.py）。
    endpoint: str
    username: str | None
    password: str | None
    # ---- 连接二：DAG 投递（SSH）----
    # 投递目录在设置页配（纯数据库读取，空则视为未配置）。**这是 Airflow 主机上的
    # 路径**——产物经 SSH 推到那台机器，ontoMeta 本地不留副本。
    dags_dir: str
    # 填了密码就用密码（需 sshpass）；留空则用 ontoMeta 主机的默认 SSH 身份/agent
    # （要指定私钥就写进该主机的 ~/.ssh/config——路径不是 Web 设置页管得了的东西）。
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_password: str | None
    # DAG 形状与时序。dag_parse_timeout 要大于 Airflow 的 dag_dir_list_interval，
    # 否则首次提交必然报「尚未解析到」。
    max_tasks_per_dag: int
    max_active_tasks_per_dag: int
    dag_parse_timeout: float
    preflight_sentinel_timeout: float
    staging_swap: bool
    enabled: bool
    # Flink 执行引擎参数（设置页配）：搬运/计算任务经 Airflow BashOperator 提交 flink run。
    # runner_jar 空 → 退「仅产出 SQL 不执行」。flink_bin 缺 PATH 时填绝对路径。
    flink_sql_runner_jar: str = ""
    flink_sql_runner_class: str = "com.ontometa.flink.SqlRunner"
    flink_bin: str = "flink"
    flink_deploy_target: str = "yarn-per-job"
    flink_parallelism: int = 1
    flink_yarn_queue: str = ""
    flink_checkpoint_dir: str = ""
    flink_rest_endpoint: str = ""

    @property
    def available(self) -> bool:
        # 没有投递目录/投递主机就没法把 DAG 交出去；未启用或无 endpoint 也不可用。
        return bool(self.enabled and self.endpoint and self.dags_dir and self.ssh_host)

    def build_delivery(self):
        """构造 SSH 投递器。

        放在运行期配置上，调用侧（materialization_runner / flink_job_runner /
        pipeline_compiler）拿到后直接 ``deliver(...)``。

        只有 SSH 一条通道：ontoMeta / Airflow / Flink 常分处三台机器，"写本地文件系统"
        在那种拓扑下只会把产物写进一台没人看的机器。本机验证把 ssh_host 指向 localhost。
        """
        from app.services.dag_delivery import get_delivery

        return get_delivery(
            self.ssh_host,
            user=self.ssh_user or None,
            port=self.ssh_port,
            password=self.ssh_password or None,
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
            request_timeout=float(c.get("request_timeout") or 90),
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
            # 纯数据库读取（法则：配置只在设置页，不读环境变量）：空 = 未配置，
            # available 为假、物化报错——环境变量只在建行播种时读一次（bootstrap 例外）。
            dags_dir=a.get("dags_dir") or "",
            # SSH 投递参数：同上，纯数据库读取。
            ssh_host=a.get("ssh_host") or "",
            ssh_port=int(a.get("ssh_port") or 22),
            ssh_user=a.get("ssh_user") or "",
            ssh_password=a.get("ssh_password") or None,
            max_tasks_per_dag=a.get("max_tasks_per_dag") or 50,
            max_active_tasks_per_dag=a.get("max_active_tasks_per_dag") or 16,
            dag_parse_timeout=a.get("dag_parse_timeout") or 60.0,
            preflight_sentinel_timeout=a.get("preflight_sentinel_timeout") or 20.0,
            staging_swap=a.get("staging_swap") if a.get("staging_swap") is not None else True,
            enabled=a.get("enabled", False),
            # Flink 执行参数（设置页配，空则用默认；flink_bin 缺省 PATH 上的 `flink`）。
            flink_sql_runner_jar=a.get("flink_sql_runner_jar") or "",
            flink_sql_runner_class=a.get("flink_sql_runner_class")
            or "com.ontometa.flink.SqlRunner",
            flink_bin=a.get("flink_bin") or "flink",
            flink_deploy_target=a.get("flink_deploy_target") or "yarn-per-job",
            flink_parallelism=int(a.get("flink_parallelism") or 1),
            flink_yarn_queue=a.get("flink_yarn_queue") or "",
            flink_checkpoint_dir=a.get("flink_checkpoint_dir") or "",
            flink_rest_endpoint=a.get("flink_rest_endpoint") or "",
        )

    # Cube（保留用于向后兼容，但不再作为可部署组件）
    def get_cube_settings(self, db: Session) -> dict:
        """已废弃：Cube 不再作为可部署的基础设施组件。保留此方法用于向后兼容。"""
        from datetime import datetime, timezone
        return {"updated_at": datetime.now(timezone.utc)}

    def update_cube_settings(self, db: Session, data: dict) -> dict:
        """已废弃：Cube 不再作为可部署的基础设施组件。保留此方法用于向后兼容。"""
        from datetime import datetime, timezone
        return {"updated_at": datetime.now(timezone.utc)}

    def get_cube_runtime(self, db: Session) -> CubeRuntimeConfig:
        """已废弃：Cube 不再作为可部署的基础设施组件。返回空配置用于向后兼容。"""
        return CubeRuntimeConfig(
            api_url="",
            api_secret=None,
            preagg_refresh="1 hour",
            tenant_dimension=None,
            timeout_seconds=30,
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
                    # 遗留表仍有 jobs_dir 列（读取侧真源已是 dependency_components，
                    # 且投递不再用它）——留空即可，不再播种一个指向已删组件的路径。
                    jobs_dir="",
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
