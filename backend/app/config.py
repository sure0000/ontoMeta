from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 用**绝对路径**定位（基于本文件位置 → backend/.env），让服务在任意 cwd 下拉起都能
# 读到 DATABASE_URL 等 bootstrap 配置。相对 ".env" 只在 cwd==backend 时才命中；MCP server
# 被外部（dsh / 远程 uvicorn）以别的 cwd 拉起时会静默退回默认 sqlite 空库、连错库起不来——
# 这是 dsh 接入时踩到的真实根因。环境变量仍优先于本文件（测试与容器化据此覆盖）。
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8")

    app_name: str = "ontoMeta"
    debug: bool = True
    database_url: str = "sqlite:///./ontometa.db"

    # 管理端共享 Token；未配置时受保护的 /api 返回 503
    ontometa_admin_token: str | None = None
    # 外部 API Key 哈希 pepper（可选，变更后须重新生成全部 App Key）
    api_key_hash_pepper: str | None = None

    # ---- MCP 服务（stdio）鉴权 ----
    # stdio 传输没有逐请求 HTTP 头：整条会话（一个子进程）用一个身份。身份来自启动 MCP
    # 服务器的客户端（Claude Desktop / Cursor）在其配置的 env 块里传入的 Token——与
    # ONTOMETA_ADMIN_TOKEN / Principal Token 同价，由 app.auth.resolve_principal_token 解析。
    ontometa_mcp_token: str | None = None
    # 未提供 Token 时授予的角色。本地 stdio 是用户自己拉起的子进程，给到 reader 级别
    # 让只读查询开箱即用，但同步/物化提案与代跑 SQL 仍按各工具的 required_role 拦住。
    # 置空字符串 = 无匿名身份（所有需要角色的工具一律 403），供锁定部署使用。
    mcp_default_role: str = "reader"
    # MCP 限流（进程内滑动窗口，stdio 单进程即全局）：每个工具每分钟调用上限。
    # 防 agent 失控循环打爆下游（数仓 / DB）。0 = 关闭限流。与 agent_run_sql_min_role /
    # agent_soundness 一样是**行为参数**，随部署走 env（不属于「连接配置进 Web 设置页」）。
    mcp_rate_limit_per_minute: int = 120
    # execute_sql 直打数仓、代价最重，单独更严。0 = 跟随 mcp_rate_limit_per_minute。
    mcp_execute_sql_rate_limit_per_minute: int = 30
    # ---- MCP 远程 HTTP 传输（Phase 5）----
    # 默认关闭：把 MCP 暴露到网络是安全敏感操作，须显式开启。开启后 MCP 挂在后端的
    # /mcp 路由（Streamable HTTP，JSON 响应模式），异地 agent 用「服务地址 + 令牌」连接，
    # 不碰任何本地路径。身份逐请求解析（Authorization: Bearer <令牌>），复用与 REST 同一份
    # resolve_principal_token；无本地 stdio 的「一进程一 env Token」。
    mcp_http_enabled: bool = False
    # 远程是否允许匿名（无令牌）。默认否——公网暴露不该匿名可读；本地 stdio 的匿名 reader
    # 便利不适用于网络。为真时无令牌回落 mcp_default_role（仍受各工具 required_role 约束）。
    mcp_http_allow_anonymous: bool = False

    datahub_gms_url: str = "http://localhost:8080"
    datahub_frontend_url: str = "http://localhost:9002"
    datahub_token: str | None = None

    # Airflow 编排的产物投递目录（部署路径，config-web-only 法则的 bootstrap 例外）：
    # 只在给新库播种 Airflow 设置行时读一次，缺省落到 docker/orchestration 下的本地
    # 验证栈目录；此后以设置页（DB）为权威，运行期不再读本变量。
    airflow_dags_dir: str = ""

    # 血缘补录的代码包归档目录（部署路径，config-web-only 法则的 bootstrap 例外）：
    # 重扫要用原包，所以上传的包必须落盘；缺省放在 backend/data/lineage_packages。
    lineage_package_dir: str = ""

    # 单个物化 DAG 的任务上限。M16 据此把大本体拆成多个 DAG；M13 只用于 preflight
    # 预警「本次表数超限、当前仍会塞进一个 DAG」。默认 50。
    ontometa_max_tasks_per_dag: int = 50
    # 单个 DAG 内并发跑的搬运任务上限（Airflow max_active_tasks）。层内不再一次性全放开，
    # 避免 734 表一次拉起几百个并发。可按 worker/池子容量调。
    ontometa_max_active_tasks_per_dag: int = 16
    # 落盘 DAG 后等 Airflow 解析到它再触发的最长秒数（替代「立刻触发、404 被吞」）。
    # ⚠ Airflow dag_dir_list_interval 默认 300s（§8.1），若解析慢需相应放大。
    ontometa_dag_parse_timeout: float = 60.0
    # 全量装载是否走 staging + 原子切换（M15）：搬进 ``<表>__stg_<批次>``，成功后由
    # Dialect Adapter 的切换语句换到正式表——搬到一半失败时正式表原封不动。关掉则退回
    # 直接写正式表（失败即半张表/空表）。留这个开关是因为各引擎切换的原子性与代价需在
    # 真实实例核实（⚠ MATERIALIZE_SYNC_STABILITY.md §8.3），真出问题要能一键退回。
    ontometa_staging_swap: bool = True

    # Flink 执行引擎参数已迁到【设置页 → Airflow/Flink】（落库，见 AirflowRuntimeConfig）。
    # 不再从 env 读——遵循 docs/DEVELOPMENT_PRINCIPLES.md P1（配置只在 Web 端）。

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 300.0
    # LLM 客户端可靠性（草稿预生成对自建 GLM 端点大扇出时易触发连接抖动）：
    # - max_retries：OpenAI SDK 自带的连接错重试次数（默认 2 偏低，端点抖动时不够）。
    # - connect/keepalive：给 httpx 客户端显式分档超时与 keepalive 上限，主动在服务器
    #   关闭空闲连接前丢弃它，避免复用死连接触发 ReadError/RemoteProtocolError。
    llm_max_retries: int = 5
    llm_connect_timeout_seconds: float = 10.0
    llm_http_keepalive_expiry_seconds: float = 15.0
    llm_http_max_keepalive: int = 8

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
    # 单个命名分块的 app 级重试次数（叠加在 SDK max_retries 之上）：分块流水线用
    # return_exceptions 收集失败块，仍失败才让任务失败（不做确定性命名降级），配合
    # checkpoint 续跑只补缺失块。退避为指数+jitter。
    draft_chunk_retry_attempts: int = 3
    # 预生成的 evidence 证据包磁盘缓存 TTL（秒）：续跑/重试时命中则跳过 DataHub 的
    # 分钟级抓取，直接进入分块生成。0 = 禁用缓存。默认 6 小时。
    draft_evidence_cache_ttl_seconds: int = 21600
    # evidence 缓存落盘目录（相对 backend 工作目录，跟随 agent_trace_dir 的约定）。
    draft_evidence_cache_dir: str = ".cache/draft_evidence"
    # 连接类瞬时失败时自动续跑的最大次数（0 = 禁用，退回人工重试）：自动入队的续跑
    # 任务复用 checkpoint + evidence 缓存，只补缺失块，绝不重跑抓取。
    draft_auto_resume_max: int = 2

    max_concurrent_draft_generations: int = 2
    # 草稿生成是否在分离子进程执行（C）：默认 True，reload/异常退出杀 API worker
    # 时子进程不受影响，任务能跑到底。测试置 false 走进程内 inline，保持确定/快速。
    draft_worker_subprocess: bool = True
    # 进程启动时回收僵尸草稿任务的陈旧宽限窗口（秒）：仅回收 updated_at 早于
    # now-窗口 的 queued/running 任务，避免热重载/孤儿进程并存时误杀另一存活进程里
    # 正在推进的任务。须大于单个生成分块的最坏 LLM 延迟。
    draft_task_stale_grace_seconds: int = 180
    # 并发过高会加重不稳定隧道(如 ngrok)的断连概率；配合 _graphql 重试，取较稳的 3。
    datahub_max_concurrency: int = 3

    cors_origins: list[str] = [
        "http://localhost:5180",
        "http://localhost:5173",
        "http://localhost:3000",
    ]


settings = Settings()
