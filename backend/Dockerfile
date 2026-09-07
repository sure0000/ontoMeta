FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.1"

COPY alembic.ini .
COPY alembic ./alembic
COPY scripts ./scripts
COPY app ./app

# 非 root 运行：容器里的进程一旦被打穿，root 意味着能改 /app 下的代码、装包、看遍
# 整个文件系统。代码归 root 所有（运行用户读得到、写不了），只把应用**确实要写**的
# 三个目录交给它——写盘路径写死在这里，改 settings 里的默认值时要跟着改：
#   data/lineage_packages  血缘代码包归档（重扫要用原包，必须落盘）
#   .cache/draft_evidence  证据包缓存（settings.draft_evidence_cache_dir）
RUN useradd --system --create-home --uid 10001 ontometa \
    && chown -R root:root /app \
    && mkdir -p /app/data/lineage_packages /app/.cache/draft_evidence /app/.logs/agent_traces \
    && chown -R ontometa:ontometa /app/data /app/.cache /app/.logs
USER ontometa

ENV PYTHONUNBUFFERED=1
# uvicorn worker 数。单 worker 只用一个 CPU 核，本体查询、证据构建这类 CPU 密集调用
# 会互相排队。⚠ 改这个值要连着改 DB_POOL_SIZE / DB_MAX_OVERFLOW：数据库连接数是
# workers × (pool_size + max_overflow)，默认 4 × (5 + 10) = 60，Postgres 默认上限 100。
ENV UVICORN_WORKERS=4

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://127.0.0.1:8000/health || exit 1

# exec 形式启动，让 uvicorn 主管进程直接收到 SIGTERM，容器停止时 worker 能优雅退出。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS}"]
