"""物化编排 materialization_runner.run：总是交 Airflow 编排（不再有直连落库）。

验证：产出 DAG + 搬运作业并触发一次运行、按勾选/覆盖裁剪与重命名、触发失败不丢产物、
前置校验（无数据源/无 dsn/未配 Airflow）报错。生成器按 hive 引擎产出真实 DDL。
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
    故每个用例结束后复位成「未配置」。
    """
    yield client
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db, {"enabled": False}
        )


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
        receipt = materialization_runner.run(
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
        receipt = materialization_runner.run(
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
    # 建表 DDL 落盘到 DAG spec，重命名后的表名在其中
    import json as _json

    spec_path = next((tmp_path / "dags").glob("*.json"))
    spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    assert any("warehouse_prod" in s and "dim_customer" in s for s in spec["ddl"])


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

        materialization_runner.run(
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
        materialization_runner.run(
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


def _enable_airflow(tmp_path, monkeypatch, *, triggered: dict, channel: str = "docker", capabilities: dict | None = None):
    """把 Airflow 配成可用，并把 REST 调用换成记录器（不需要真实 Airflow）。

    **编排配置全在设置行里**（不再有环境变量），故这里直接写设置行——测试因此走的是
    生产同一条读取路径，而不是绕过它去 patch 一个已经不参与决策的 env 对象。
    ``channel`` 选执行通道：``docker`` 走旧兄弟容器通道；``runner`` 额外把 SyncRunnerClient
    换成返回给定 capabilities 的替身（不需要真实 runner）。
    """
    from app.services.settings_service import SettingsService

    with SessionLocal() as db:
        SettingsService().update_airflow_settings(
            db,
            {
                "endpoint": "http://airflow:8080",
                "enabled": True,
                "dags_dir": str(tmp_path / "dags"),
                "jobs_dir": str(tmp_path / "jobs"),
                "sync_channel": channel,
                "sync_runner_endpoint": "http://sync-runner:8088",
            },
        )

    if channel == "runner":
        from app.connectors.sync_runner import EXPECTED_CONTRACT_VERSION

        caps = capabilities or {
            # 跟着常量走：契约 bump 时这里不该变成又一处要手改的地方。
            "contract_version": EXPECTED_CONTRACT_VERSION,
            "backends": ["native", "seatunnel"],
            "sources": ["mysql", "mariadb"],
            "sinks": ["hive", "doris"],
            "modes": ["full", "incremental"],
            # Hive 只做全量（seatunnel 档回读不了 Hive 水位），与真 runner 一致。
            "sink_modes": {"hive": ["full"], "doris": ["full", "incremental"]},
        }

        class _FakeRunner:
            def __init__(self, *a, **kw):
                pass

            def capabilities(self):
                return caps

            def close(self):
                pass

        monkeypatch.setattr(materialization_runner, "SyncRunnerClient", _FakeRunner)

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
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-1",
        )

    # 关键：没有任何直连写库
    assert rec.calls == []
    assert receipt["execute_mode"] == "orchestrated"
    assert receipt["ok"] is True
    assert receipt["state"] == "queued"
    # run_id = 制品 id + 批次后缀（无 cron → manual）：每个 DAG 一个确定性 run_id，重复提交幂等
    assert receipt["dag_run_id"] == "ontometa__artifact-1__manual"
    assert triggered["run_id"] == receipt["dag_run_id"]
    assert triggered["unpaused"] == receipt["dag_id"]
    # M16：无 cron 的表进单个 __manual DAG（本例 2 表未超上限，不分批）
    assert len(receipt["batches"]) == 1
    assert receipt["batches"][0]["dag_id"].endswith("__manual")

    # 产物真的落盘了：DAG + 边车 JSON + 每个作业一个配置
    dags = sorted(p.name for p in (tmp_path / "dags").iterdir())
    jobs = sorted(p.name for p in (tmp_path / "jobs").iterdir())
    assert any(n.endswith(".py") for n in dags) and any(n.endswith(".json") for n in dags)
    assert len(jobs) == len(receipt["jobs"]) >= 1


def test_runner_channel_writes_pythonoperator_dag_and_records_channel(tmp_path, monkeypatch):
    """runner 通道：产出 PythonOperator DAG（无作业配置文件），回执记 sync_channel=runner。"""
    ids = _seed("runnerok")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    triggered: dict = {}
    _enable_airflow(tmp_path, monkeypatch, triggered=triggered, channel="runner")

    with SessionLocal() as db:
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="artifact-r1",
        )

    assert receipt["ok"] is True
    assert receipt["sync_channel"] == "runner"
    assert receipt["state"] == "queued"
    # DAG 落盘且是 runner 通道：PythonOperator、无凭据、无作业配置文件
    dag_py = next(p for p in (tmp_path / "dags").iterdir() if p.name.endswith(".py"))
    source = dag_py.read_text(encoding="utf-8")
    assert "PythonOperator" in source and "DockerOperator" not in source
    assert "password" not in source and "jdbc:" not in source
    jobs_dir = tmp_path / "jobs"
    assert not jobs_dir.exists() or list(jobs_dir.iterdir()) == []


def test_runner_channel_requires_endpoint(tmp_path, monkeypatch):
    """runner 通道但没配 runner 地址 → 提交前报错，不产出连不上 runner 的 DAG。"""

    ids = _seed("runnernoep")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={}, channel="runner")
    _set_airflow(sync_runner_endpoint="")

    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="sync-runner"):
            materialization_runner.run(
                db, ids["ontology_id"], target_datasource_id=ids["datasource_id"], engine="hive"
            )


def test_runner_channel_unreachable_errors(tmp_path, monkeypatch):
    """runner 不可达 → 报错（提交前问清，而不是发个 DAG 过去再炸）。"""
    ids = _seed("runnerdown")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(tmp_path, monkeypatch, triggered={}, channel="runner")

    class _Dead:
        def __init__(self, *a, **kw):
            pass

        def capabilities(self):
            raise materialization_runner.SyncRunnerError("capabilities", "connection refused")

        def close(self):
            pass

    monkeypatch.setattr(materialization_runner, "SyncRunnerClient", _Dead)

    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="不可达"):
            materialization_runner.run(
                db, ids["ontology_id"], target_datasource_id=ids["datasource_id"], engine="hive"
            )


def test_runner_channel_contract_mismatch_errors(tmp_path, monkeypatch):
    """契约版本不匹配即拒绝提交，不发过去再看会不会炸（§3.5）。"""
    ids = _seed("runnerver")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    _enable_airflow(
        tmp_path,
        monkeypatch,
        triggered={},
        channel="runner",
        capabilities={
            "contract_version": "999",
            "modes": ["full"],
            "sources": ["mysql"],
            "sinks": ["hive"],
        },
    )

    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="契约版本"):
            materialization_runner.run(
                db, ids["ontology_id"], target_datasource_id=ids["datasource_id"], engine="hive"
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
        receipt = materialization_runner.run(
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
        receipt = materialization_runner.run(
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
        receipt = materialization_runner.run(
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
        receipt = materialization_runner.run(
            db,
            ids["ontology_id"],
            target_datasource_id=ids["datasource_id"],
            engine="hive",
            artifact_id="a-orphan",
        )

    built: set[str] = set()
    for spec_path in sorted((tmp_path / "dags").glob("*.json")):
        built.update(_json.loads(spec_path.read_text(encoding="utf-8"))["ddl_targets"])

    # 回执说要物化的每张表，都真的有某个 DAG 会建它
    assert set(receipt["tables"]) == built
    assert any(t.endswith(".manual_dim") for t in built)
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
        receipt = materialization_runner.run(
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
        receipt = materialization_runner.run(
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


def test_errors_when_airflow_unavailable(monkeypatch):
    """未配可用 Airflow 时直接报错——不再静默回退到直连落库。"""
    ids = _seed("unset")
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError, match="Airflow"):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
            )


def test_missing_datasource_raises(monkeypatch):
    ids = _seed("nods")
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id="does-not-exist",
                engine="hive",
            )


def test_datasource_without_dsn_raises(monkeypatch):
    ids = _seed("nodsn", with_dsn=False)
    monkeypatch.setattr(data_app_executor, "execute_write", _Recorder())
    with SessionLocal() as db:
        with pytest.raises(materialization_runner.MaterializationError):
            materialization_runner.run(
                db,
                ids["ontology_id"],
                target_datasource_id=ids["datasource_id"],
                engine="hive",
            )
