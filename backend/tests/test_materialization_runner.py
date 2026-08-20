"""物化 / 数据同步编排：``run_materialize``（只建结构）与 ``run_sync``（只搬数据）。

两者总是交 Airflow 编排（不再有直连落库）。验证：各自只产该产的东西（物化无搬运任务、
同步无建表且回执 tables 为空）、按勾选/覆盖裁剪与重命名、按 cron 分组分批、触发失败不丢
产物、前置校验（无数据源/无 dsn/未配 Airflow）报错、dag_id 按制品隔离。
生成器按 hive 引擎产出真实 DDL。
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import (
    DomainContext,
    ObjectType,
    Ontology,
    OntologyStatus,
    Property,
    RelationType,
)
from app.models.data_app import DataSource
from app.services import data_app_executor, materialization_runner
from app.services.materialization_contract import MaterializationContractService

_mc = MaterializationContractService()

_URN = "urn:li:dataset:(urn:li:dataPlatform:mysql,erp_ods.{table},PROD)"


@pytest.fixture(autouse=True)
def _init_db(client):
    """拉起 session 级 client 以建表（runner 测试直接用 SessionLocal，不走 API）。

    ``airflow_settings`` 是单例行，用例改了会漏给后续用例（默认执行方式随之改变），
    故每个用例结束后复位成「未配置」。同时清掉本模块 seed 的 DataSource 行——
    留在共享会话库会污染后续用例的 warehouse-first 选源（如 test_column_profiler）。
    """
    yield client
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db, {"enabled": False}
        )
        db.query(DataSource).filter(DataSource.name.like("target-%")).delete()
        db.commit()


def _seed(tag: str, *, with_dsn: bool = True, orphan: bool = False) -> dict:
    with SessionLocal() as db:
        domain = DomainContext(
            datahub_domain_id=f"urn:li:domain:mr-{tag}", name=f"mr-{tag}"
        )
        db.add(domain)
        db.flush()
        ontology = Ontology(
            domain_context_id=domain.id,
            status=OntologyStatus.PUBLISHED.value,
            version=1,
        )
        db.add(ontology)
        db.flush()

        customer = ObjectType(
            ontology_id=ontology.id,
            name="customer",
            display_name="客户",
            table_role="business_object",
            source_ref=_URN.format(table="tab_customer"),
        )
        order = ObjectType(
            ontology_id=ontology.id,
            name="sales_order",
            display_name="销售订单",
            table_role="business_object",
            source_ref=_URN.format(table="tab_order"),
        )
        db.add_all([customer, order])
        db.flush()
        if orphan:
            # 无 source_ref → planner 产不出搬运作业，但表仍要建（回归用例的被测对象）。
            manual_dim = ObjectType(
                ontology_id=ontology.id,
                name="manual_dim",
                display_name="人工维度",
                table_role="business_object",
            )
            db.add(manual_dim)
            db.flush()
            db.add(
                Property(
                    object_type_id=manual_dim.id,
                    name="code",
                    display_name="编码",
                    data_type="string",
                    semantic_type="identifier",
                    required=True,
                )
            )
        # 有列才有可生成的 ETL SELECT；created_at 同时作为增量装载的分区键候选。
        db.add_all(
            [
                Property(
                    object_type_id=customer.id,
                    name="customer_id",
                    display_name="客户ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                ),
                Property(
                    object_type_id=customer.id,
                    name="created_at",
                    display_name="创建时间",
                    data_type="timestamp",
                    semantic_type="datetime",
                ),
                Property(
                    object_type_id=order.id,
                    name="sales_order_id",
                    display_name="订单ID",
                    data_type="bigint",
                    semantic_type="identifier",
                    required=True,
                ),
            ]
        )
        db.add(
            RelationType(
                ontology_id=ontology.id,
                name="places",
                display_name="下单",
                source_object_type_id=customer.id,
                target_object_type_id=order.id,
                structure_type="fact_table",
            )
        )
        ds = DataSource(
            name=f"target-{tag}",
            kind="doris",
            dsn_secret_ref="sqlite:///unused-stubbed" if with_dsn else None,
        )
        db.add(ds)
        db.commit()
        return {"ontology_id": ontology.id, "datasource_id": ds.id}


class _Recorder:
    """替身 execute_write：记录每次调用，按需模拟成功/失败。"""

    def __init__(self, fail_first: bool = False):
        self.calls: list[list[str]] = []
        self.fail_first = fail_first

    def __call__(self, *, dsn, statements, mapping=None, timeout_seconds=60):
        self.calls.append(list(statements))
        failed = self.fail_first and len(self.calls) == 1
        per = [
            {
                "index": i,
                "ok": not failed,
                **({"error": "boom"} if failed else {}),
            }
            for i in range(len(statements))
        ]
        return {
            "total": len(statements),
            "executed": 0 if failed else len(statements),
            "failed": len(statements) if failed else 0,
            "error": "boom" if failed else None,
            "per_statement": per,
        }


def test_selected_targets_filters_tables(tmp_path, monkeypatch):
    ids = _seed("filter")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            selected_targets=["customer"],
            artifact_id="art-filter",
        )
    # 只物化 customer 一张表
    assert receipt["tables"] == [t for t in receipt["tables"] if t.endswith(".customer")]
    assert len(receipt["tables"]) == 1


def test_database_and_table_overrides_rename_targets(tmp_path, monkeypatch):
    """人工指定的目标库/表名要贯穿到产物（建表 DDL 与搬运作业同一目标）。"""
    ids = _seed("rename")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        service = materialization_runner._contract_service
        service.sync(db, ids["ontology_id"])
        contracts = service.list_contracts(db, ids["ontology_id"])
        names = service.resolve_target_names(db, contracts)
        customer_contract = next(
            c for c in contracts if names.get(c.target_id, (None,))[0] == "customer"
        )
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            database_overrides={"dim": "warehouse_prod"},
            table_overrides={customer_contract.id: "dim_customer"},
            selected_targets=["customer"],
            artifact_id="art-rename",
        )

    # 勾选按实体名，改名后仍要命中（不能因为改名把自己裁掉）
    assert receipt["tables"] == ["warehouse_prod.dim_customer"]
    # 建表 DDL 落盘到 Flink DAG spec 的 warehouse_ddl，重命名后的表名在其中
    import json as _json

    spec_path = next(
        p for p in (tmp_path / "dags").rglob("*.json") if p.parent.name != "jobs"
    )
    spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    assert any(
        "warehouse_prod" in s and "dim_customer" in s for s in spec["warehouse_ddl"]
    )


def test_batch_refresh_cron_expands_to_selected_contracts(tmp_path, monkeypatch):
    """整批调度写回本次选中的每个契约；逐实体显式给的 cron 不被整批默认盖掉。

    Data Agent 那条路只能说「整批每天凌晨跑」——它拿不到、也不该要求先拿到契约 id。
    """
    ids = _seed("batchcron")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        service = materialization_runner._contract_service
        service.sync(db, ids["ontology_id"])
        contracts = service.list_contracts(db, ids["ontology_id"], materialized_only=True)
        names = service.resolve_target_names(db, contracts)
        customer = next(c for c in contracts if names.get(c.target_id, (None,))[0] == "customer")

        materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            refresh_cron="0 2 * * *",
            overrides={customer.id: {"refresh_cron": "0 5 * * *"}},
            artifact_id="art-batchcron",
        )
        after = service.list_contracts(db, ids["ontology_id"], materialized_only=True)
        by_id = {c.id: c for c in after}

    assert by_id[customer.id].refresh_cron == "0 5 * * *"  # 细粒度优先
    others = [c for cid, c in by_id.items() if cid != customer.id]
    assert others and all(c.refresh_cron == "0 2 * * *" for c in others)


def test_no_refresh_cron_leaves_contracts_untouched(tmp_path, monkeypatch):
    """不传整批调度就不动契约的 refresh_cron——不能凭空塞一个默认调度。"""
    ids = _seed("nocron")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        service = materialization_runner._contract_service
        service.sync(db, ids["ontology_id"])
        before = {
            c.id: c.refresh_cron
            for c in service.list_contracts(db, ids["ontology_id"], materialized_only=True)
        }
        materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="art-nocron",
        )
        after = {
            c.id: c.refresh_cron
            for c in service.list_contracts(db, ids["ontology_id"], materialized_only=True)
        }
    assert after == before


def _set_airflow(**fields):
    """改设置行上的编排旋钮（原来是环境变量，现已全部入库）。"""
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(db, fields)


def _enable_airflow(tmp_path, monkeypatch, *, triggered: dict):
    """把 Airflow 配成可用，把 REST 调用换成记录器，并配上 Flink SqlRunner JAR。

    统一执行架构：搬运一律走 Flink SQL on YARN（无 seatunnel/docker/runner 多通道）。
    Flink 参数现在也在 Airflow 设置行里（DB，见 DEVELOPMENT_PRINCIPLES P1）：给个非空 JAR
    路径走真实 DAG 生成而非 handoff（不落地执行，只产 DAG + .sql + 触发替身）。
    """
    from app.services.settings_service import AirflowRuntimeConfig, SettingsService

    from tests.support.delivery import LocalTransportDelivery, make_runner_jar

    # 投递器给真的（SSH 逻辑完整跑，传输落到本地 tmp）：runner 经 build_delivery()
    # 拿实例，patch 类方法覆盖全部用例，避免真 ssh 到不存在的 test 主机。
    monkeypatch.setattr(
        AirflowRuntimeConfig,
        "build_delivery",
        lambda self: LocalTransportDelivery(),
    )

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db,
            {
                "endpoint": "http://airflow:8080",
                "enabled": True,
                "dags_dir": str(tmp_path / "dags"),
                # SSH 投递参数：available 判定需要主机；jar 是 ontoMeta 侧真实路径
                # （随包分发），不再是 Airflow 机器上的占位绝对路径。
                "ssh_host": "test-airflow-host",
                # Flink 提交参数（DB）：非空 JAR 走真实 DAG 生成；checkpoint 供含 timestamp 分区键
                # 的表默认 incremental 的 CDC 流式作业（缺它仅在专门用例里清回空验证）。
                "flink_sql_runner_jar": make_runner_jar(tmp_path),
                "flink_checkpoint_dir": "file:///tmp/ontometa-ckpt",
            },
        )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def dag_exists(self, dag_id):
            return True  # 假设已解析（等解析那步立即通过）

        def unpause_dag(self, dag_id):
            triggered["unpaused"] = dag_id

        def trigger_dag(self, dag_id, *, dag_run_id, conf=None):
            triggered["dag_id"] = dag_id
            triggered["run_id"] = dag_run_id
            return {"dag_run_id": dag_run_id, "state": "queued"}

        def run_url(self, dag_id, run_id):
            return f"http://airflow:8080/dags/{dag_id}/grid?dag_run_id={run_id}"

        def close(self):
            triggered["closed"] = True

    monkeypatch.setattr(materialization_runner, "AirflowClient", _FakeClient)


def test_orchestrated_mode_writes_dag_and_triggers(tmp_path, monkeypatch):
    """默认路径：产出 DAG + 作业配置并触发一次运行，**不在本进程里落库**。"""
    ids = _seed("orch")
    rec = _Recorder()
    monkeypatch.setattr(data_app_executor, "execute_write", rec)
    triggered: dict = {}
    _enable_airflow(tmp_path, monkeypatch, triggered=triggered)

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-1",
        )

    # 关键：没有任何直连写库
    assert rec.calls == []
    # execute_mode 是**对账的开关**：agent_pipeline._reconcile_orchestrated_status 只认
    # "orchestrated"，此前这里产 "flink_on_yarn"，于是制品从提交那刻起就是 SUCCEEDED、
    # 从不与真实 DagRun 对账。搬运通道另记在 sync_tool。
    assert receipt["execute_mode"] == "orchestrated"
    assert receipt["emit"] == "dml"
    assert receipt["ok"] is True
    assert receipt["state"] == "queued"
    # run_id = 制品 id + 批次后缀（无 cron → manual）：每个 DAG 一个确定性 run_id，重复提交幂等
    assert receipt["dag_run_id"] == "ontometa__artifact-1__manual"
    assert triggered["run_id"] == receipt["dag_run_id"]
    assert triggered["unpaused"] == receipt["dag_id"]
    # M16：无 cron 的表进单个 __manual DAG（本例 2 表未超上限，不分批）
    assert len(receipt["batches"]) == 1
    assert receipt["batches"][0]["dag_id"].endswith("__manual")

    # 产物真的落盘了：DAG + 边车 JSON + 每个作业一个配置。
    # 按 <dags>/ontometa/<artifact_id>/ 子目录聚合，jobs 落该子目录下的 jobs/。
    dags = sorted(p.name for p in (tmp_path / "dags").rglob("*") if p.is_file())
    jobs = sorted(p.name for p in (tmp_path / "dags").rglob("jobs/*") if p.is_file())
    assert any(n.endswith(".py") for n in dags) and any(n.endswith(".json") for n in dags)
    assert len(jobs) == len(receipt["jobs"]) >= 1


def test_flink_channel_writes_bashoperator_dag_with_sql_files(tmp_path, monkeypatch):
    """统一执行：产出 Flink SQL DAG（BashOperator flink run），每个搬运作业一个 .sql 文件，
    产物无凭据明文。"""
    ids = _seed("flinkok")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    triggered: dict = {}
    _enable_airflow(tmp_path, monkeypatch, triggered=triggered)

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-r1",
        )

    assert receipt["ok"] is True
    assert receipt["execute_mode"] == "orchestrated"
    assert receipt["sync_tool"] == "flink"
    assert "sync_channel" not in receipt  # 多通道概念已废除
    assert receipt["state"] == "queued"
    # DAG 是 Flink 通道：BashOperator + flink run，无 Docker 兄弟容器、无凭据明文。
    # （read_spec 用一个 PythonOperator 读边车 JSON 暴露 sql_dir，属正常编排，不是搬运容器。）
    dag_py = next(p for p in (tmp_path / "dags").rglob("*.py"))
    source = dag_py.read_text(encoding="utf-8")
    assert "BashOperator" in source and "DockerOperator" not in source
    assert "password" not in source and "jdbc:" not in source
    # 每个搬运作业一个 .sql 文件（落 <dags>/ontometa/<artifact>/jobs/）
    sql_files = [p for p in (tmp_path / "dags").rglob("jobs/*.sql") if p.is_file()]
    assert len(sql_files) == len(receipt["jobs"]) >= 1
    # .sql 里是恒等搬运（INSERT + flink 占位符凭据，无明文）
    body = sql_files[0].read_text(encoding="utf-8")
    assert "INSERT INTO" in body and "${" in body


def test_incremental_without_checkpoint_dir_errors(tmp_path, monkeypatch):
    """增量/CDC 是流式作业需 checkpoint_dir；未配 → 用户可读的物化错误，而非 500。"""
    ids = _seed("incrnockpt")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={})
    # 清空 checkpoint（DB 设置行）：增量作业缺 checkpoint 应报可读错误
    _set_airflow(flink_checkpoint_dir="")
    # customer 源平台 mysql 有 CDC 连接器；全局 load_strategy=incremental 触发 CDC 路径
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="checkpoint"):
            materialization_runner.run_sync(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="doris",  # doris 支持 incremental（hive 只全量）
                load_strategy="incremental",
                selected_targets=["customer"],
            )


def test_orchestrated_reports_trigger_failure_without_losing_artifacts(tmp_path, monkeypatch):
    """触发失败时产物已落盘：回执要如实说「产物已出、触发失败」，不能让人以为什么都没发生。"""
    ids = _seed("orchfail")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    triggered: dict = {}
    _enable_airflow(tmp_path, monkeypatch, triggered=triggered)

    def _boom(self, dag_id, *, dag_run_id, conf=None):
        raise materialization_runner.AirflowError("trigger_dag", "HTTP 404 dag not found")

    monkeypatch.setattr(materialization_runner.AirflowClient, "trigger_dag", _boom)

    with SessionLocal() as db:
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-2",
        )

    assert receipt["ok"] is False
    assert "404" in receipt["error"]
    assert receipt["state"] == "failed"
    assert list((tmp_path / "dags").iterdir())  # 产物仍在，可人工排查后重试


def _set_cron_m16(ontology_id: str, mapping: dict[str, str]) -> None:
    """按实体名给契约设 refresh_cron 并钉住（sync 不覆盖），供 M16 分组测试用。"""
    with SessionLocal() as db:
        _mc.sync(db, ontology_id)
        cs = _mc.list_contracts(db, ontology_id)
        names = _mc.resolve_target_names(db, cs)
        for c in cs:
            entity = names.get(c.target_id, (None,))[0]
            if entity in mapping:
                _mc.update(db, c.id, {"refresh_cron": mapping[entity]})


def test_cron_grouping_one_dag_per_cron(tmp_path, monkeypatch):
    """M16：一个 cron 一个 DAG，少数派 cron 不再被众数吞掉。"""
    ids = _seed("crongroup")
    _set_cron_m16(ids["ontology_id"], {"customer": "0 2 * * *"})  # sales_order 无 cron
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="a-cron",
        )

    batches = receipt["batches"]
    assert {b["schedule"] for b in batches} == {"0 2 * * *", None}
    assert len({b["dag_id"] for b in batches}) == 2  # 两个 cron 组 → 两个 DAG
    by_sched = {b["schedule"]: b for b in batches}
    assert any("customer" in j for j in by_sched["0 2 * * *"]["jobs"])
    assert by_sched[None]["dag_id"].endswith("__manual")


def test_batching_splits_group_over_max_tasks(tmp_path, monkeypatch):
    """M16：单组超上限 → 拆成 _b0/_b1 多个 DAG，每个 ≤ 上限。"""

    ids = _seed("batchsplit")  # customer + sales_order，均无 cron → 同一 manual 组
    _set_airflow(max_tasks_per_dag=1)
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="a-split",
        )

    batches = receipt["batches"]
    assert len(batches) == 2
    assert all(len(b["jobs"]) == 1 for b in batches)
    assert sorted(b["suffix"] for b in batches) == ["manual_b0", "manual_b1"]
    assert len({b["dag_run_id"] for b in batches}) == 2  # 单批可单独重跑


def test_tables_without_sync_jobs_still_get_ddl(tmp_path, monkeypatch):
    """回归：无搬运作业的表（ADS/缺 source_ref/装载方式不支持）也必须有 DAG 建它。

    分批曾按「该批有作业的目标表」筛 DDL，于是这些表的 CREATE 不进任何 DAG，却仍出现在
    回执的 tables 里——用户以为建了，实际一张也没有。
    """
    import json as _json

    ids = _seed("orphanddl", orphan=True)
    _set_cron_m16(ids["ontology_id"], {"customer": "0 2 * * *"})  # 制造多批，孤儿不能被吞
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="a-orphan",
        )

    # 汇总所有 DAG 的建表 DDL 文本（Flink spec 用 warehouse_ddl，一批一段列表）。
    all_ddl: list[str] = []
    for spec_path in sorted((tmp_path / "dags").rglob("*.json")):
        if spec_path.parent.name == "jobs":  # 跳过作业配置 JSON，只读 DAG 边车 spec
            continue
        all_ddl.extend(_json.loads(spec_path.read_text(encoding="utf-8"))["warehouse_ddl"])
    # 去方言引用：hive/doris 用反引号 `dim`.`customer`，postgres 用双引号——都归一成
    # 裸的 dim.customer 再比对，否则 qualified 名永远命中不了带引号的 DDL。
    blob = "\n".join(all_ddl).replace("`", "").replace('"', "")

    # 回执说要物化的每张表，都真的有某条建表 DDL 会建它（含无搬运作业的孤儿表）
    for table in receipt["tables"]:
        assert table in blob, f"{table} 未出现在任何 DAG 的建表 DDL 中"
    assert "manual_dim" in blob
    # 孤儿归 manual 批（无 cron），不挂到定时 DAG 上重复建
    manual = next(b for b in receipt["batches"] if b["schedule"] is None)
    assert any(t.endswith(".manual_dim") for t in manual["tables"])
    assert not any(t.endswith(".manual_dim") for t in manual["jobs"])


def test_unparsed_dag_recorded_not_triggered(tmp_path, monkeypatch):
    """落盘后等解析：Airflow 未解析到 → 不触发，回执记「尚未解析到」，产物仍在。"""

    ids = _seed("noparse")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={})
    _set_airflow(dag_parse_timeout=0.2)
    monkeypatch.setattr(materialization_runner.time, "sleep", lambda s: None)

    class _NoParse:
        def __init__(self, *a, **kw):
            pass

        def dag_exists(self, dag_id):
            return False  # 永远解析不到

        def unpause_dag(self, dag_id):
            raise AssertionError("未解析到就不该触发")

        def trigger_dag(self, *a, **kw):
            raise AssertionError("未解析到就不该触发")

        def run_url(self, dag_id, run_id):
            return f"url/{dag_id}/{run_id}"

        def close(self):
            pass

    monkeypatch.setattr(materialization_runner, "AirflowClient", _NoParse)

    with SessionLocal() as db:
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="a-noparse",
        )

    assert receipt["ok"] is False
    assert all("尚未解析" in (b["error"] or "") for b in receipt["batches"])
    assert list((tmp_path / "dags").iterdir())  # 产物已落盘，可排查后重试


def test_parse_wait_is_bounded_by_one_timeout_across_batches(tmp_path, monkeypatch):
    """多批都没被解析到时，**总**等待受一个 timeout 约束，不随批数放大。

    目录两侧不一致（失败模式 #3）正是这条路径要抓的场景。逐批各等一遍会把
    「超时 × 批数」拖成十几分钟阻塞在一个 HTTP 请求里（734 张表约 15 批），
    网关先超时，用户连「产物已落盘、去查目录」这句提示都拿不到。
    """

    ids = _seed("waitbound")
    _set_airflow(max_tasks_per_dag=1)  # → 2 批
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={})
    _set_airflow(dag_parse_timeout=10.0)

    # 假时钟：sleep 不真睡，只推进时间——总耗时因而可断言且用例是秒级的。
    clock = {"now": 0.0}
    monkeypatch.setattr(materialization_runner.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        materialization_runner.time,
        "sleep",
        lambda s: clock.__setitem__("now", clock["now"] + s),
    )

    class _NoParse:
        def __init__(self, *a, **kw):
            pass

        def dag_exists(self, dag_id):
            return False

        def run_url(self, dag_id, run_id):
            return f"url/{dag_id}/{run_id}"

        def close(self):
            pass

    monkeypatch.setattr(materialization_runner, "AirflowClient", _NoParse)

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="a-waitbound",
        )

    assert len(receipt["batches"]) == 2
    assert all("尚未解析" in (b["error"] or "") for b in receipt["batches"])
    # 关键：等了一个超时，不是两个（轮询间隔 2s，故留一轮的余量）
    assert clock["now"] <= 10.0 + 2


@pytest.mark.parametrize("entry", ["run_materialize", "run_sync"])
def test_errors_when_airflow_unavailable(entry, monkeypatch):
    """未配可用 Airflow 时直接报错——不再静默回退到直连落库。两个入口共用同一套前置校验。"""
    ids = _seed(f"unset-{entry}")
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="Airflow"):
            getattr(materialization_runner, entry)(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
            )


@pytest.mark.parametrize("entry", ["run_materialize", "run_sync"])
def test_missing_datasource_raises(entry, monkeypatch):
    ids = _seed(f"nods-{entry}")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError):
            getattr(materialization_runner, entry)(
                db,
                ids["ontology_id"],
                target_datasource_id="does-not-exist",
                engine="hive",
            )


@pytest.mark.parametrize("entry", ["run_materialize", "run_sync"])
def test_datasource_without_dsn_raises(entry, monkeypatch):
    ids = _seed(f"nodsn-{entry}", with_dsn=False)
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError):
            getattr(materialization_runner, entry)(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
            )


def test_run_is_gone_no_alias_left():
    """``run()`` 必须整个消失，不留别名。

    只有两个调用方且都要改口径，留个别名只会让「这到底是建表还是搬数」重新变成
    调用点的猜谜——那正是本次拆分要消除的歧义。
    """
    assert not hasattr(materialization_runner, "run")


# ---------- 拆分：物化只出 DDL，同步只出 DML ----------


def _dag_specs(tmp_path) -> list[dict]:
    """落盘的所有 DAG 边车 spec（跳过 jobs/ 下的作业 JSON）。"""
    import json as _json

    return [
        _json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((tmp_path / "dags").rglob("*.json"))
        if p.parent.name != "jobs"
    ]


def test_materialize_emits_ddl_only(tmp_path, monkeypatch):
    """物化只建结构：产出的 DAG 有建表语句、**没有任何搬运任务与 staging/swap**。

    此前物化与同步是同一个函数，物化会顺手把数据搬了（且全量走 staging swap 会整表
    替换，业务直接写进目标表的行在第一次切换后消失）。
    """
    ids = _seed("emitddl")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="art-emitddl",
        )

    assert receipt["emit"] == "ddl"
    assert receipt["jobs"] == []
    assert receipt["tables"]  # 表还是要建的
    specs = _dag_specs(tmp_path)
    assert len(specs) == 1  # 建表是一次性动作，不按 cron 分批
    spec = specs[0]
    assert spec["tasks"] == [], "物化不该产搬运任务"
    assert not spec["swaps"], "物化不该产 staging swap"
    assert spec["schedule"] is None, "建表 DAG 不挂定时，否则每轮重跑一遍 CREATE"
    assert spec["warehouse_ddl"]
    # 没有 .sql 搬运作业文件落盘
    assert not list((tmp_path / "dags").rglob("jobs/*.sql"))


def test_sync_emits_dml_only(tmp_path, monkeypatch):
    """同步只搬数据：不建业务表，回执的 tables 为空。

    ``tables`` 是前端（MaterializationContractPanel）判「这张表已物化」的依据，
    同步填了就会让一次搬运冒充成物化。批里唯一允许出现的 DDL 是 staging 表
    （``CREATE TABLE … LIKE 正式表``，属搬运侧且要求正式表已存在）。
    """
    ids = _seed("emitdml")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            load_strategy="full",
            artifact_id="art-emitdml",
        )

    assert receipt["emit"] == "dml"
    assert receipt["tables"] == [], "同步回执不得报告建了表"
    assert receipt["jobs"]
    assert all(b["tables"] == [] for b in receipt["batches"])
    ddl = [s for spec in _dag_specs(tmp_path) for s in spec["warehouse_ddl"]]
    # 只有 staging 建表（LIKE 正式表），没有一条业务表的列定义
    assert ddl and all(" LIKE " in s.upper() for s in ddl), ddl
    assert not any("customer_id" in s for s in ddl), ddl


def test_sync_without_movable_objects_raises(tmp_path, monkeypatch):
    """一个作业都编不出来 = 没有任何数据会被搬，必须大声拒绝而不是回 ok: True。

    拆分后这是最可能的静默失败：选中的对象全是人工建模的（没有物理源表，只能物化）。
    """
    ids = _seed("nomovable", orphan=True)
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        with pytest.raises(
            materialization_runner.MaterializationError, match="没有任何对象可以同步"
        ) as exc:
            materialization_runner.run_sync(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
                selected_targets=["manual_dim"],  # 无 source_ref，搬不了
                artifact_id="art-nomovable",
            )
    # 报错要说清是哪个对象、为什么，而不是笼统一句「无可搬对象」
    assert "manual_dim" in str(exc.value)


def test_materialize_does_not_require_flink_jar(tmp_path, monkeypatch):
    """物化不校验 Flink SqlRunner JAR：建表是 SQLExecuteQueryOperator，与 Flink 无关。

    此前 ``_run_orchestrated`` 在建 bundle 之前就因缺 JAR 退 handoff，于是没装 SqlRunner
    的部署上，人工建模的本体拿到 ok: True 却一张表都没建。
    """
    ids = _seed("nojarddl")
    _enable_airflow(tmp_path, monkeypatch, triggered={})
    _set_airflow(flink_sql_runner_jar="")

    with SessionLocal() as db:
        receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="art-nojarddl",
        )

    assert receipt["execute_mode"] == "orchestrated", "缺 JAR 不该让建表退成 handoff"
    assert receipt["tables"]
    assert any(spec["warehouse_ddl"] for spec in _dag_specs(tmp_path))


def test_sync_without_flink_jar_handoffs(tmp_path, monkeypatch):
    """同步缺 JAR 则如实退「仅产出」——数据搬运真的执行不了。"""
    ids = _seed("nojardml")
    _enable_airflow(tmp_path, monkeypatch, triggered={})
    _set_airflow(flink_sql_runner_jar="")

    with SessionLocal() as db:
        receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="art-nojardml",
        )

    assert receipt["execute_mode"] == "handoff"
    assert receipt["emit"] == "dml"
    assert receipt["tables"] == []


def test_dag_id_is_scoped_to_artifact_not_ontology(tmp_path, monkeypatch):
    """同一本体上的物化与同步必须落在不同 dag_id 上。

    dag_id 的 base 曾取本体 id，而两条制品的 cron 后缀相同 → 同一个 dag_id，
    谁后投递谁覆盖谁（产物目录早已按 artifact_id 隔离，dag_id 没跟上）。
    """
    ids = _seed("dagid")
    _enable_airflow(tmp_path, monkeypatch, triggered={})

    with SessionLocal() as db:
        ddl_receipt = materialization_runner.run_materialize(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="art-dagid-materialize",
        )
        dml_receipt = materialization_runner.run_sync(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="art-dagid-sync",
        )

    ddl_dags = {b["dag_id"] for b in ddl_receipt["batches"]}
    dml_dags = {b["dag_id"] for b in dml_receipt["batches"]}
    assert ddl_dags and dml_dags
    assert not (ddl_dags & dml_dags)


# ---------- 两段式 DDL：外键裁剪与分批 ----------


def test_select_constraints_drops_refs_outside_selection():
    """只物化一部分时，指向未勾选表的外键必须丢掉。

    这是「选了 account_category 没选 account」当场整批红的那个 bug：postgres 加约束时
    校验被引用表存在，不裁掉的话 add_constraints 任务必失败。
    """
    from app.services.materialization_runner import _select_constraints

    constraints = {
        "dim.account_category": [
            {"ref": "dim.account", "sql": "ALTER ... account"},
            {"ref": "dim.brand", "sql": "ALTER ... brand"},
        ],
    }
    # 只建 account_category 和 brand，没建 account
    ddl_items = [("dim.account_category", "CREATE ..."), ("dim.brand", "CREATE ...")]
    out = _select_constraints(constraints, ddl_items)
    assert out == {"dim.account_category": [{"ref": "dim.brand", "sql": "ALTER ... brand"}]}


def test_select_constraints_keeps_all_when_both_ends_selected():
    from app.services.materialization_runner import _select_constraints

    constraints = {"dim.a": [{"ref": "dim.b", "sql": "S"}]}
    ddl_items = [("dim.a", "CREATE a"), ("dim.b", "CREATE b")]
    assert _select_constraints(constraints, ddl_items) == {"dim.a": [{"ref": "dim.b", "sql": "S"}]}


def test_assign_ddl_defers_cross_batch_foreign_keys():
    """跨批的外键不能下发：各批是独立 DAG、同时触发、彼此无先后。

    下发了就可能在被引用表建出来之前执行。丢掉的要记进 constraints_deferred，
    由回执报出去——不是静默降级。
    """
    from app.services.materialization_runner import _assign_ddl

    batches = [
        {"suffix": "c1", "schedule": "0 1 * * *", "jobs": ()},
        {"suffix": "manual", "schedule": None, "jobs": ()},
    ]
    ddl_items = [("dim.a", "CREATE a"), ("dim.b", "CREATE b")]
    # a 在 manual 批（孤儿 DDL 全归 manual），故 a→b 与 b→a 都是同批
    constraints = {
        "dim.a": [{"ref": "dim.b", "sql": "FK a->b"}],
    }
    out = _assign_ddl(batches, ddl_items, constraints)
    manual = next(b for b in out if b["suffix"] == "manual")
    assert manual["constraints"] == {"dim.a": ["FK a->b"]}
    assert manual["constraints_deferred"] == []


def test_assign_ddl_records_deferred_when_ref_in_other_batch(monkeypatch):
    """被引用表落在别的批 → 该约束这次不建，并如实记进 constraints_deferred。"""
    from types import SimpleNamespace

    from app.services.materialization_runner import _assign_ddl

    def _job(target):
        return SimpleNamespace(target=SimpleNamespace(qualified=target))

    batches = [
        {"suffix": "c1", "schedule": "0 1 * * *", "jobs": (_job("dim.a"),)},
        {"suffix": "c2", "schedule": "0 2 * * *", "jobs": (_job("dim.b"),)},
    ]
    ddl_items = [("dim.a", "CREATE a"), ("dim.b", "CREATE b")]
    constraints = {"dim.a": [{"ref": "dim.b", "sql": "FK a->b"}]}
    out = _assign_ddl(batches, ddl_items, constraints)
    first = next(b for b in out if b["suffix"] == "c1")
    assert first["constraints"] == {}
    assert first["constraints_deferred"] == ["dim.a → dim.b"]
