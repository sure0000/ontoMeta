# 物化编排验证栈（本地）

给 `MATERIALIZE_ORCHESTRATION.md` 方案做本地验证用。**只补齐本机缺的服务**——DataHub、ERPNext、
Bigtop Manager 已各自在跑，这里以 external network 接入，不重起一套。

## 本机基线（2026-08 实测）

| 服务 | 状态 | 地址 | 用途 |
|---|---|---|---|
| DataHub | ✅ v1.6.0 在跑 | GMS `:8080`，前端 `:9002` | 血缘上报目标 |
| ERPNext + MariaDB 11.8 | ✅ 在跑 | MariaDB `:3308` | 真实源库（本体「数据域-ERP-全量」即来自它） |
| Bigtop Manager | ✅ 在跑 | `:18080` | 备选方案里用它部署 Hive |
| Airflow | ❌ 缺 | 本栈提供 `:8081` | 调度 + 血缘自动注册 |
| SeaTunnel | ❌ 缺 | 本栈提供 | 跨源搬运 |
| 目标数仓 | ❌ 缺 | 本栈提供 Doris `:9030` | 物化落库目标 |

> ⚠ **镜像拉取是第一道坎**：本机 Docker Hub 直连可用但极慢——`alpine:3.20`（约 4MB）实测
> **6 分 03 秒**。airflow(~1.5GB)/seatunnel(~1GB)/doris(~3GB) 按此速率拉不下来。
> 起栈前先配镜像源（见 `.env.example`），`make orch-preflight` 会测速并在过慢时直接拦下。

## 用法

```bash
cp docker/orchestration/.env.example docker/orchestration/.env   # 按需改镜像源与源库凭据
make orch-preflight        # 镜像/网络/端口/已有服务检查，全过再往下
make orch-up-airflow       # Airflow + 元数据库（含 DataHub 插件，镜像本地构建）
make orch-up-sync          # SeaTunnel
make orch-up-warehouse     # Doris（可选，见下方目标数仓三案）
make orch-logs             # 跟日志
make orch-down             # 停
```

Airflow Web：http://localhost:8081 （admin/admin，`standalone` 模式，仅本地验证用）。

## 目标数仓三案

| 方案 | 代价 | 保真度 | 何时选 |
|---|---|---|---|
| **Doris all-in-one**（本栈 `warehouse` profile） | 拉 ~3GB 镜像 | 高：仓库已有 Doris Dialect Adapter，MySQL 线协议便于校验建表结果 | 默认首选 |
| Bigtop Manager 部署 Hive | 无需新镜像（bm-1/2/3 已在跑），但部署重、耗时长 | 最高：Hive 是方案里的权威副本 | 镜像拉不动，或要验 Hive 方言 |
| 本机已有 MySQL 8 当假数仓 | 零成本 | **低**：仓库无 MySQL 方言 adapter，不验证 DDL 正确性 | 只验编排/血缘机制（M10 冒烟），不验方言 |

## 目录

| 路径 | 说明 |
|---|---|
| `dags/` | ontoMeta 生成的 DAG 落盘处（方案 A：产物即制品，可 diff 可回滚），挂载进 Airflow |
| `seatunnel/jobs/` | 生成的 SeaTunnel 作业配置，Airflow 任务容器挂载读取 |
| `airflow.Dockerfile` | Airflow + `acryl-datahub-airflow-plugin` + docker/mysql provider |
| `preflight.sh` | 起栈前检查，`make orch-preflight` 调它 |

## 版本对照（已核实）

- 本机 DataHub = **v1.6.0**（`GET :8080/config` 返回）
- `acryl-datahub-airflow-plugin==1.6.0` → `apache-airflow >=2.5.0,<4.0.0`（Airflow 2/3 均可）
- `acryl-datahub-airflow-plugin==1.6.0.17`（最新补丁）→ `apache-airflow >=3.0.0,<4.0.0`（**仅 Airflow 3**）

故默认取 **Airflow 2.10.5 + 插件 1.6.0**：与 DataHub 主版本对齐，REST 为 `/api/v1`。
若改用 Airflow 3，REST 变 `/api/v2`，影响面限于 `app/connectors/airflow.py` 一个模块。

## 起栈后待核实项

镜像拉下来、栈起来之后，这几条要在真实实例上确认（方案文档里标了「⚠ 需实施前验证」的那些）：

1. `curl localhost:8081/openapi.json | grep dagRuns` —— 确认 REST 版本与触发路径。
2. Airflow 里 `datahub_rest_default` 连接可用：跑一个 hello DAG，看 DataHub 是否出现 DataFlow/DataJob。
3. SeaTunnel 版本的配置格式（HOCON/JSON）与 Hive/Doris Sink 对分区覆盖写的支持。
4. DataHub v1.6.0 的字段级血缘（`fineGrainedLineages`）支持程度。
