# ontometa-sync-runner 镜像：Python + DB 驱动构建期装齐，运行期零挂载。
#
# 驱动烘进镜像是 §3.1 的要点之一——DockerOperator 通道要部署方逐个 bind mount 驱动 jar，
# 缺一个就是运行三分钟后的 ClassNotFoundException；这里在 build 期一次装齐，runner 起来即可用，
# GET /capabilities 如实声明装到了哪些。
#
# 构建（仓库根目录）：
#   docker build -f docker/sync-runner/Dockerfile -t ontometa/sync-runner:3 .
# 直连 Docker Hub 慢/不通时走镜像源（与 docker/orchestration/.env.example 的 IMG_* 同一套路）：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
#     -f docker/sync-runner/Dockerfile -t ontometa/sync-runner:3 .
# 运行（凭据按 alias 从环境变量注入，请求体里只有 alias）：
#   docker run -p 8098:8098 \
#     -e SYNC_CONN_ERP_READONLY_URL='mysql+pymysql://ro:pw@erp-db:3306/erp' \
#     -e SYNC_CONN_WAREHOUSE_DEFAULT_URL='mysql+pymysql://root:pw@doris-fe:9030/dw' \
#     ontometa/sync-runner:3
# 写 Hive 时还要指到 Zeta 集群（见 sync_runner/backends/seatunnel.py 的部署前提）：
#   -e SEATUNNEL_REST_ENDPOINT=http://<zeta>:8080
#   -e SYNC_CONN_<目标别名>_METASTORE_URI=thrift://<不带下划线的主机>:9083

# 基础镜像做成 ARG：本机实测直连 Docker Hub 会以
# "failed to resolve source metadata for docker.io/library/python:3.12-slim" 告败，
# 而这类失败读起来像 Dockerfile 写错了，其实只是拉不到镜像。
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# psycopg2-binary 自带 libpq，无需额外系统包；留一层便于以后加 unixodbc 等。
WORKDIR /app

COPY sync_runner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY sync_runner /app/sync_runner

# 8088 是 YARN ResourceManager 的默认 Web 端口，在 Hadoop 环境里必撞（本机实测：
# 起在 8088 上会被 YARN 的 Jetty 抢先应答，客户端收到一个 404 HTML 页而不是 JSON）。
EXPOSE 8098
# 单进程即可（作业在进程内后台线程跑）；水平扩展留待作业量真正需要时再议。
CMD ["uvicorn", "sync_runner.app:app", "--host", "0.0.0.0", "--port", "8098"]
