# Flink 集群镜像 = 官方镜像 + JDBC 连接器与驱动
#
# 驱动必须在**集群侧**（JobManager/TaskManager 的 /opt/flink/lib）：
# SqlRunner 是薄 jar，没 shade 任何连接器，实际建连发生在 TaskManager 上。
# 官方镜像因授权原因不带这些驱动，必须自己加——这正是此前 transform 任务
# 「差 MariaDB 驱动」卡住的地方。
#
# 版本必须与 SqlRunner 编译时的 Flink 发行版一致（当前 1.13.6）。
# 换 Flink 版本要连带重编 tools/flink-sql-runner。

ARG IMG_FLINK=flink:1.13.6-scala_2.12-java8

FROM ${IMG_FLINK}

ARG FLINK_VERSION=1.13.6
ARG SCALA_VERSION=2.12
ARG MYSQL_DRIVER_VERSION=8.0.33
ARG PG_DRIVER_VERSION=42.7.3

USER root

# 网络慢时改为先下到本目录再 COPY *.jar /opt/flink/lib/
RUN set -eux; \
    M2=https://repo1.maven.org/maven2; \
    curl -fsSL -o /opt/flink/lib/flink-connector-jdbc_${SCALA_VERSION}-${FLINK_VERSION}.jar \
      "$M2/org/apache/flink/flink-connector-jdbc_${SCALA_VERSION}/${FLINK_VERSION}/flink-connector-jdbc_${SCALA_VERSION}-${FLINK_VERSION}.jar"; \
    curl -fsSL -o /opt/flink/lib/mysql-connector-j-${MYSQL_DRIVER_VERSION}.jar \
      "$M2/com/mysql/mysql-connector-j/${MYSQL_DRIVER_VERSION}/mysql-connector-j-${MYSQL_DRIVER_VERSION}.jar"; \
    curl -fsSL -o /opt/flink/lib/postgresql-${PG_DRIVER_VERSION}.jar \
      "$M2/org/postgresql/postgresql/${PG_DRIVER_VERSION}/postgresql-${PG_DRIVER_VERSION}.jar"; \
    chown -R flink:flink /opt/flink/lib

USER flink
