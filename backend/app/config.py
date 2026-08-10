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
    # 单个 DAG 内并发跑的搬运任务上限（Airflow max_active_tasks）。层内不再一次性全放开，
    # 避免 734 表一次拉起几百个并发。可按 worker/池子容量调。
    ontometa_max_active_tasks_per_dag: int = 16
    # 落盘 DAG 后等 Airflow 解析到它再触发的最长秒数（替代「立刻触发、404 被吞」）。
    # ⚠ Airflow dag_dir_list_interval 默认 300s（§8.1），若解析慢需相应放大。
    ontometa_dag_parse_timeout: float = 60.0
    # preflight 写 sentinel DAG 后，等 Airflow 解析到它的最长秒数（专治「dags 目录
    # 两侧不一致」失败模式 #3）。Airflow 的 dag_dir_list_interval 默认 300s（⚠ 见
    # MATERIALIZE_SYNC_STABILITY.md §8.1），故超时不作硬失败、只降级为提醒。
    ontometa_preflight_sentinel_timeout: float = 20.0
    # 全量装载是否走 staging + 原子切换（M15）：搬进 ``<表>__stg_<批次>``，成功后由
    # Dialect Adapter 的切换语句换到正式表——搬到一半失败时正式表原封不动。关掉则退回
    # 直接写正式表（失败即半张表/空表）。留这个开关是因为各引擎切换的原子性与代价需在
    # 真实实例核实（⚠ MATERIALIZE_SYNC_STABILITY.md §8.3），真出问题要能一键退回。
    ontometa_staging_swap: bool = True

    # 搬运执行通道（M14）：
    # - ``runner``：向常驻 sync-runner 发一次 HTTP，凭据由 runner 自解析，无宿主机路径/
    #   docker.sock/驱动挂载（消失败模式 #2/#3/#4/#5）。M14 起的默认。
    # - ``docker``：旧通道，worker 经 docker.sock 起一次性搬运容器。已跑通过的路径保留作
    #   对照（比照 M9 保留 direct），出问题可一键切回。
    sync_channel: str = "runner"
    # runner 通道下 sync-runner 的地址。runner 通道选中但此项为空，物化会在提交前报错
    # 让人去配（而不是产出一个连不上 runner 的 DAG）。
    sync_runner_endpoint: str = ""

    # Flink on YARN 计算任务（P1-3）的部署参数。属部署基础设施，不进设置页。
    # flink_sql_runner_jar 是预置的通用 SqlRunner JAR（读 SQL 文件、用环境变量替换占位符
    # 后逐条 executeSql），runner_class 是其 main class，flink_bin 是 flink 命令路径。
    # 缺 runner_jar 时 transform/metric 不执行、只产 SQL（回退「仅产出」模式）。
    flink_sql_runner_jar: str = ""
    flink_sql_runner_class: str = "com.ontometa.flink.SqlRunner"
    flink_bin: str = "flink"
    flink_deploy_target: str = "yarn-per-job"
    flink_parallelism: int = 1
    flink_yarn_queue: str = ""
    # 增量/CDC 搬运是常驻流式作业，读位点靠 checkpoint 续存（重启从最近 checkpoint 恢复，
    # 不重搬不漏）。这是它落哪儿的根目录（``file://…`` 本地，或 ``hdfs://…`` 集群），
    # 属部署基础设施。空 = 未配；有增量/CDC 表时编译会报错要求配上，全量搬运不需要它。
    flink_checkpoint_dir: str = ""

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
    # Data Agent 形式化可靠性闸门（FORMAL_VALIDATION_IMPL.md）：
    #   off  — 完全关闭，回到现状（零风险回滚）
    #   warn — 只记录「本应拒答」到回执，不真拒（观测泄漏基线/误杀率）
    #   on   — 正式生效：SQL 语义证明不过则不执行、断言不可证则拒答
    agent_soundness: str = "on"
    # Data Agent 的 run_sql 工具最低角色（P1.1）。
    # 手动执行端点 `POST /chat-bi/messages/{id}/execute` 要求 publisher，而 `/chat-bi/ask`
    # 兜底只要 editor——工具化会绕过权限模型：editor 自己跑 SQL 被 403，让 Agent 代跑却放行。
    # 故 run_sql 在**工具粒度**上与 /execute 同价。降为 "editor" 可回到改造前行为。
    agent_run_sql_min_role: str = "publisher"
    # 字段取值画像（P1.3）：缓存秒数（0=禁用缓存）与 TopN 条数。
    # 取值分布变化慢，缓存主要是防「每问一次就打一次库」。
    agent_profile_cache_seconds: int = 900
    agent_profile_top_n: int = 20
    # 语义检索（P1.5）。**留空即关闭**——检索退回纯 ILIKE，功能不受影响，
    # 只是同义词召回不到。填入嵌入模型名（走已配置的 OpenAI 兼容服务）即启用。
    agent_embedding_model: str = ""
    # 向量截断维度：现代嵌入模型前若干维已承载主要语义，截短能让纯 Python 暴力检索
    # 快数倍而召回基本无损。设 0 表示不截断。
    agent_embedding_dim: int = 256
    # 余弦相似度下限：低于此分不作为召回结果（0 表示不过滤）
    agent_embedding_min_score: float = 0.0
    # V4 O1 上下文 compaction：对齐 pi 的结构化摘要 + 近轮保留。
    #   on  — 历史超预算时把旧轮抽取为结构化摘要，仅保留近轮原文
    #   off — 回到 history[-6:] 硬截断（零风险回滚）
    agent_compaction: str = "on"
    # 近轮保留的字符预算（约等于 token×2，CJK 场景取字符更稳）。超此预算的更早轮被摘要。
    agent_history_char_budget: int = 6000
    # V4 O2 大结果离场存储：run_sql 结果表辇大时，回给模型的只是「列名 + 样例 N 行 + 总行数 + 句柄」，
    # 全量行存在进程内 per-run store，模型需要更多行时用 read_result(handle, offset, limit) 分页取。
    #   上下文只看到样例，不再被整张表污染、也不被字符截断丢列。前端/渲染/analyze 仍拿全量。
    agent_result_offload: str = "on"  # on/off（off 回到直接回灰全量行的旧行为）
    # 回给模型的样例行数。**V5 T2 实测过 5 vs 20，维持 5**（详见 DATA_AGENT_V5_PLAN §P0.9）。
    #
    # 调大的理由曾经很硬：ERP 域实测显示结果一超过 5 行，模型下一步必定
    # `read_result(offset=5, limit=剩余)` 把余下的**全部**翻回来（20 行取走 15、8 行取走 3），
    # 那次离场等于白花一次 LLM 往返；而多带 15 行只要 ~1000 字符，多一次往返要重付
    # 一整轮 prefill ≈30800 字符——账面上差 30 倍。
    #
    # **同一问句序列跑 5 与 20 的对照，把这个预测推翻了**：往返确实省掉了
    # （read_result 2→0、离场字符→0），但 `avg_llm_calls` 反而 6.2→8.0、步数 7.0→9.5，
    # 逐轮配对是 5 涨 1 平 0 降——不是个别轮次的偏差。样例行变多之后模型在结果上
    # 兜圈子的轮次也变多，省下的那一次往返被这个盖过去了。
    # 「不回涨 avg_llm_calls」是 V5 的验收护栏，故按实测维持 5，不按算术预测改。
    agent_result_sample_rows: int = 5
    # V4 O6 运行轨迹落地（pi JSONL session 风格，非 DB 表，改造后可整目录摘除）。
    #   关闭时不写文件；开启后每问追加一行 JSON 到 agent_trace_dir。
    agent_trace_enabled: bool = False
    agent_trace_dir: str = ".logs/agent_traces"
    # 发布时形式化不变式校验（F2）：
    #   off  — 不检查
    #   warn — 检查并返回报告，不阻断发布（默认，迁移期安全）
    #   error— 存在 error 级不变式违反则阻断发布
    formal_enforcement: str = "warn"
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
