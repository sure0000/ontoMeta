from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "ontoMeta"
    debug: bool = True
    database_url: str = "sqlite:///./ontometa.db"

    # 管理端共享 Token；未配置时受保护的 /api 返回 503
    ontometa_admin_token: str | None = None
    # 外部 API Key 哈希 pepper（可选，变更后须重新生成全部 App Key）
    api_key_hash_pepper: str | None = None

    datahub_gms_url: str = "http://localhost:8080"
    datahub_frontend_url: str = "http://localhost:9002"
    datahub_token: str | None = None

    # Airflow 编排的产物投递目录（方案 A：生成 DAG/作业配置文件 → Airflow 读共享卷）。
    # 属部署基础设施（与 Airflow 容器的挂载点对齐），不在设置页配；缺省由
    # settings_service 解析为 docker/orchestration 下的本地验证栈目录，可用环境变量覆盖。
    airflow_dags_dir: str = ""
    airflow_jobs_dir: str = ""
    # 搬运任务容器接哪张 Docker 网络。默认 bridge 保持原行为；真实部署里源库与目标仓
    # 多以容器名互访（如 hive-metastore / hadoop-namenode），默认 bridge 解析不了，
    # 需指到与它们同一张网络上。同属部署基础设施，不进设置页。
    airflow_docker_network: str = "bridge"
    # JDBC 驱动 jar 目录（宿主机路径）。搬运镜像因授权不带驱动，缺了就是
    # ClassNotFoundException: …jdbc.Driver。空 = 不挂。
    airflow_sync_drivers_dir: str = ""
    # 搬运工具的执行镜像覆盖：``工具名=镜像[,工具名=镜像…]``，如
    # ``datax=registry.internal/datax:3.0``。适配器只给得出「官方镜像叫什么」，
    # 而**镜像在这套部署里叫什么、拉不拉得到**是部署事实——DataX 无官方镜像
    # （见 warehouse/jobs/datax.py），不在这里指一个自建镜像，任务只会在 Airflow
    # 侧因 pull 404 失败。同属部署基础设施，不进设置页。
    sync_tool_images: str = ""

    # 单个物化 DAG 的任务上限。M16 据此把大本体拆成多个 DAG；M13 只用于 preflight
    # 预警「本次表数超限、当前仍会塞进一个 DAG」。默认 50。
    ontometa_max_tasks_per_dag: int = 50
    # preflight 写 sentinel DAG 后，等 Airflow 解析到它的最长秒数（专治「dags 目录
    # 两侧不一致」失败模式 #3）。Airflow 的 dag_dir_list_interval 默认 300s（⚠ 见
    # MATERIALIZE_SYNC_STABILITY.md §8.1），故超时不作硬失败、只降级为提醒。
    ontometa_preflight_sentinel_timeout: float = 20.0

    @property
    def sync_tool_image_map(self) -> dict[str, str]:
        """``sync_tool_images`` → ``{工具名: 镜像}``。格式不对的项直接跳过，不猜。"""
        mapping: dict[str, str] = {}
        for item in (self.sync_tool_images or "").split(","):
            name, sep, image = item.partition("=")
            if not sep:
                continue
            name, image = name.strip().lower(), image.strip()
            if name and image:
                mapping[name] = image
        return mapping

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 300.0

    # Cube 语义层（可选外挂）：ontoMeta 生成 Cube data model 并调用其 Load API。
    cube_api_url: str = "http://localhost:4000"
    cube_api_secret: str | None = None
    cube_timeout_seconds: float = 30.0
    # 预聚合定时刷新间隔（交给 Cube Refresh Worker）
    cube_preagg_refresh: str = "1 hour"
    # 行级权限：租户隔离列名（各对象若含此属性则自动生成 RLS 过滤）；为空则不启用
    cube_tenant_dimension: str | None = None

    # 草稿生成时单次 LLM 证据 payload 的字符预算：超过则自动分块 Map-Reduce。
    # 用字符长度做保守估计（宁可多切一块也不冒超长风险），按模型上下文调优：
    # DeepSeek 64K token 上下文，此处默认 ~48000 字符（约 16-20K token），
    # 为 system prompt 与输出预留充足余量。
    llm_context_budget_chars: int = 48000
    # 对象命名(含属性中文名)分块流水线：并发调用 LLM 的子块数上限。
    draft_chunk_max_concurrency: int = 4
    # 分块生成时每批最多打包的表(对象)数：优先按表数切块，字符预算作为兜底细分。
    draft_chunk_table_batch_size: int = 10
    # 关系业务命名分块流水线：每批最多打包的关系数(独立于对象分块，覆盖跨对象块关系)。
    draft_chunk_relation_batch_size: int = 40
    # 关系命名分块流水线：并发调用 LLM 的子块数上限(与对象流水线各自独立的信号量)。
    draft_relation_chunk_max_concurrency: int = 4

    max_concurrent_draft_generations: int = 2
    # 并发过高会加重不稳定隧道(如 ngrok)的断连概率；配合 _graphql 重试，取较稳的 3。
    datahub_max_concurrency: int = 3

    # 外部 API / MCP：每应用每分钟默认请求上限（进程内固定窗口；<=0 关闭）
    external_api_rate_limit_per_minute: int = 60

    cors_origins: list[str] = [
        "http://localhost:5180",
        "http://localhost:5173",
        "http://localhost:3000",
    ]


settings = Settings()
