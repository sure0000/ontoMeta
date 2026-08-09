# Flink SqlRunner

ontoMeta 的通用 Flink SQL 执行器。`transform`（清洗）与 `metric`（聚合）两类任务生成的
Flink SQL 脚本，由 Airflow 的 BashOperator 这样提交：

```
flink run -t <target> -p <并行度> -c com.ontometa.flink.SqlRunner sql-runner.jar --file <脚本.sql>
```

命令行由 `app/services/airflow_dag_builder.py::_flink_run_command` 拼装，jar 路径与
main class 来自 `FLINK_SQL_RUNNER_JAR` / `FLINK_SQL_RUNNER_CLASS`。

## 它做三件事

1. **读脚本**：ontoMeta 生成的 `SET` + `CREATE TABLE`（源/目标各一）+ `INSERT`。
2. **替换凭据占位符**：脚本里只有 `${ERP_READONLY_URL}` 这类占位符（凭据不进产物），
   真值从**同名环境变量**取。Airflow 侧由 BashOperator 的 `env` 注入，值是
   `{{ conn.<别名>.… }}`，运行期才解析 Connection。缺变量即报错并点名——不带着空串
   往下跑（那只会得到一句指不到原因的 "Connection refused"）。
3. **逐条执行**：`SET` 由 runner 自己解释（Flink 1.13 的 `executeSql()` 不收 SET，而
   `execution.runtime-mode` 必须在建 TableEnvironment 时就定下）；`INSERT` 会 `await()`
   到作业结束，否则 Airflow 会把「已提交」当成「已完成」。

## 构建

需要 JDK 8/11 与一份 Flink 发行版（只用它的 `lib/` 编译，不联网）：

```bash
cd tools/flink-sql-runner
FLINK_LIB=~/local/flink/current/lib
mkdir -p build
javac -encoding UTF-8 -cp "$FLINK_LIB/*" -d build src/com/ontometa/flink/SqlRunner.java
jar cf sql-runner.jar -C build .
```

产物 `sql-runner.jar` 不进版本库（`build/` 同）；部署时把路径配进 `FLINK_SQL_RUNNER_JAR`。

## 目标库/源库的 JDBC 驱动

驱动**不随本 jar 分发**，需放进 Flink 的 `lib/`（因授权原因官方镜像也不带）：

| 平台 | 驱动 |
| --- | --- |
| PostgreSQL | `postgresql-*.jar` |
| MySQL / MariaDB | `mysql-connector-j-*.jar` 或 `mariadb-java-client-*.jar` |
| Doris / StarRocks | 各自的专用 connector |

缺驱动时的报错是 `ClassNotFoundException: …jdbc.Driver`，出现在 Flink 作业日志里。
