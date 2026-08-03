"""产出一组有代表性的 DAG 产物，供真 Airflow 用 DagBag 解析一遍（配套 scripts/dag_parse_check.py）。

**为什么需要它**：单元测试里的 `ast.parse` 只看语法，桩模块建图只看我们自己写的那部分逻辑，
两者都验不了「Airflow 到底能不能把这个文件导进去」——Operator 的关键字合不合法、provider
在不在、层间连线在真实 `BaseOperator` 上成不成立，只有真 Airflow 说了算。`list >> list`
那个 bug 语法合法、桩也放过，真解析当场炸；它从 M10 活到 M16，就是因为中间没人真解析过。

用法（仓库根目录）：``make dag-parse``；或直接
``python backend/scripts/emit_dag_fixtures.py <输出目录>``。

覆盖面刻意包含：两条执行通道 × 多层（层间串联是出过事的地方）× staging 开关 ×
只建表无搬运作业（M16 的孤儿 DDL 批）。
"""

from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.airflow_dag_builder import AirflowDagBuilder  # noqa: E402
from app.warehouse.jobs import ColumnMapping, JobEndpoint, JobPlan, JobSpec  # noqa: E402

_ONTOLOGY = "11112222-3333-4444-5555-666677778888"
_URN = "urn:li:dataset:(urn:li:dataPlatform:mariadb,erp_ods.tab_{t},PROD)"


def _job(name: str, layer: str, table: str, mode: str = "full") -> JobSpec:
    return JobSpec(
        name=name,
        source=JobEndpoint(
            alias="erp_readonly",
            platform="mariadb",
            database="erp_ods",
            table=f"tab_{table}",
        ),
        target=JobEndpoint(
            alias="warehouse_default", platform="hive", database=layer, table=table
        ),
        columns=(
            ColumnMapping(source=f"{table}_id", target=f"{table}_id"),
            ColumnMapping(source="created_at", target="created_at"),
        ),
        mode=mode,
        partition_key="created_at" if mode == "incremental" else None,
        layer=layer,
        entity_name=table,
        source_urn=_URN.format(t=table),
    )


def _ddl(*targets: str) -> dict[str, str]:
    return {
        t: f"CREATE TABLE IF NOT EXISTS `{t.split('.')[0]}`.`{t.split('.')[1]}` "
        f"(`id` BIGINT, `created_at` TIMESTAMP);"
        for t in targets
    }


# 每个用例一个 (后缀, 说明, build 关键字)。后缀进 dag_id，解析报错时一眼看出是哪种组合。
_CASES = [
    (
        "runner_multilayer_staging",
        "runner 通道 + 三层 + staging：层间是 list >> list，真解析才验得出",
        dict(
            plan=JobPlan(
                jobs=(
                    _job("sync_dim_customer", "dim", "customer"),
                    _job("sync_dim_product", "dim", "product"),
                    _job("sync_dwd_order", "dwd", "order", mode="incremental"),
                    _job("sync_dws_sales", "dws", "sales"),
                )
            ),
            ddl_statements=_ddl("dim.customer", "dim.product", "dwd.order", "dws.sales"),
            channel="runner",
            runner_endpoint="http://sync-runner:8088",
            staging=True,
        ),
    ),
    (
        "runner_nostaging",
        "runner 通道 + 关掉 staging：不产 create_staging/swap 任务的那条分支",
        dict(
            plan=JobPlan(jobs=(_job("sync_dim_customer", "dim", "customer"),)),
            ddl_statements=_ddl("dim.customer"),
            channel="runner",
            runner_endpoint="http://sync-runner:8088",
            staging=False,
        ),
    ),
    (
        "docker_multilayer",
        "docker 通道 + 两层：DockerOperator 的关键字合不合法，只有真 provider 说了算",
        dict(
            plan=JobPlan(
                jobs=(
                    _job("sync_dim_customer", "dim", "customer"),
                    _job("sync_dwd_order", "dwd", "order"),
                )
            ),
            ddl_statements=_ddl("dim.customer", "dwd.order"),
            channel="docker",
            jobs_host_dir="/tmp/ontometa-jobs",
        ),
    ),
    (
        "create_tables_only",
        "只建表、无搬运作业（M16 的孤儿 DDL 批）：没有下游任务时连线不能炸",
        dict(
            plan=JobPlan(jobs=()),
            ddl_statements=_ddl("ads.gmv_daily"),
            channel="runner",
            runner_endpoint="http://sync-runner:8088",
        ),
    ),
]


def emit(out_dir: str) -> list[str]:
    """产出全部用例，返回 dag_id 列表。输出目录**整个重建**，避免上一次的残留混淆解析结果。"""
    dags_dir = os.path.join(out_dir, "dags")
    jobs_dir = os.path.join(out_dir, "jobs")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(dags_dir, exist_ok=True)
    os.makedirs(jobs_dir, exist_ok=True)

    builder = AirflowDagBuilder()
    dag_ids: list[str] = []
    for suffix, note, kwargs in _CASES:
        bundle = builder.build(
            ontology_id=_ONTOLOGY,
            dag_id_suffix=suffix,
            engine="hive",
            **kwargs,
        )
        bundle.write(dags_dir, jobs_dir)
        dag_ids.append(bundle.dag_id)
        print(f"  {bundle.dag_id}  — {note}")
    return dag_ids


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ".dagcheck"
    print(f"产出 DAG 用例到 {target}/dags：")
    ids = emit(target)
    print(f"共 {len(ids)} 个 DAG。")
