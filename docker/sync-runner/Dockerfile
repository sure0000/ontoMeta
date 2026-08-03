# ontometa-sync-runner 镜像：Python + DB 驱动构建期装齐，运行期零挂载。
#
# 驱动烘进镜像是 §3.1 的要点之一——DockerOperator 通道要部署方逐个 bind mount 驱动 jar，
# 缺一个就是运行三分钟后的 ClassNotFoundException；这里在 build 期一次装齐，runner 起来即可用，
# GET /capabilities 如实声明装到了哪些。
#
# 构建（仓库根目录）：
#   docker build -f docker/sync-runner/Dockerfile -t ontometa/sync-runner:1 .
# 运行（凭据按 alias 从环境变量注入，请求体里只有 alias）：
#   docker run -p 8088:8088 \
#     -e SYNC_CONN_ERP_READONLY_URL='mysql+pymysql://ro:pw@erp-db:3306/erp' \
#     -e SYNC_CONN_WAREHOUSE_DEFAULT_URL='mysql+pymysql://root:pw@doris-fe:9030/dw' \
#     ontometa/sync-runner:1

FROM python:3.12-slim

# psycopg2-binary 自带 libpq，无需额外系统包；留一层便于以后加 unixodbc 等。
WORKDIR /app

COPY sync_runner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY sync_runner /app/sync_runner

EXPOSE 8088
# 单进程即可（作业在进程内后台线程跑）；水平扩展留待作业量真正需要时再议。
CMD ["uvicorn", "sync_runner.app:app", "--host", "0.0.0.0", "--port", "8088"]
