# Airflow 镜像 = 官方镜像 + DataHub 血缘插件 + Flink 客户端 + SqlRunner jar
#
# 为什么 Airflow 里要有完整 Flink 客户端：DAG 用 BashOperator 跑 `flink run`，
# 而 `flink run` 默认在**客户端**执行用户 main()——SqlRunner 的 Flink планner
# 是在这个容器里跑的，不是在集群里。所以客户端的 Java 版本也必须合规。
#
# ⚠ Java 必须是 8/11。Flink 1.13 在 Java 17 上会因模块访问限制直接崩，
#   而 apache/airflow:2.10.5 的 bookworm 基础镜像只带 openjdk-17。
#   故用多阶段从 temurin 拷一份 JRE 11 进来。
#
# JDBC 驱动**不放这里**——它们要在集群侧（见 flink.Dockerfile）。
# 放错边的症状是运行期 ClassNotFoundException，且报错指不到是哪边缺。

ARG IMG_AIRFLOW=apache/airflow:2.10.5

FROM eclipse-temurin:11-jre AS jre

FROM ${IMG_AIRFLOW}

ARG FLINK_VERSION=1.13.6
ARG SCALA_VERSION=2.12

USER root

COPY --from=jre /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Flink 发行版（客户端只用 bin/ 与 lib/，但整包解压最省事）。
# 网络慢时改为先下到本目录再 COPY：
#   COPY flink-${FLINK_VERSION}-bin-scala_${SCALA_VERSION}.tgz /tmp/
RUN set -eux; \
    curl -fsSL -o /tmp/flink.tgz \
      "https://archive.apache.org/dist/flink/flink-${FLINK_VERSION}/flink-${FLINK_VERSION}-bin-scala_${SCALA_VERSION}.tgz"; \
    mkdir -p /opt/flink; \
    tar -xzf /tmp/flink.tgz -C /opt/flink --strip-components=1; \
    rm /tmp/flink.tgz

ENV FLINK_HOME=/opt/flink
ENV PATH="${FLINK_HOME}/bin:${PATH}"

# `flink run -t remote` 靠这份 conf 找 JobManager；不写则报 "no cluster specified"。
RUN printf '%s\n' \
    'jobmanager.rpc.address: flink-jobmanager' \
    'rest.address: flink-jobmanager' \
    'rest.port: 8081' \
    > ${FLINK_HOME}/conf/flink-conf.yaml

# SqlRunner jar（客户端侧，由 flink run 上传给集群）。
# 构建前先执行：cp tools/flink-sql-runner/sql-runner.jar deploy/benchmark/
RUN mkdir -p /opt/ontometa
COPY sql-runner.jar /opt/ontometa/sql-runner.jar

RUN chown -R airflow: /opt/ontometa /opt/flink

USER airflow

# DataHub 血缘插件：Airflow 侧按 AIRFLOW__DATAHUB__CONN_ID 找 GMS，
# 自动把任务的 inlets/outlets 上报成血缘。
RUN pip install --no-cache-dir \
      "acryl-datahub-airflow-plugin==1.6.0" \
      "apache-airflow-providers-mysql" \
      "apache-airflow-providers-postgres"
