"""Airflow 的两条连接：表单不重复、无效项不出现、拨测分开。

Airflow 一个组件握着两条互不相干的连接——**调度 API**（触发 DagRun，走 8080）与
**DAG 投递**（rsync 产物，走 22，甚至不是同一台机器）。它们常常一通一断，故：

1. 连接字段分组自描述，前端据此分节渲染；``ssh_password`` 在整个表单面上只出现一次
   （此前既在通用「连接信息」里按字段名裸渲染一遍、又在「DAG 投递」里渲染一遍）；
2. 填了不生效的东西不占表单（token / api_version / 私钥路径）；
3. 拨测可只测其中一条，行状态由**各条的最新记账**聚合，不被单条结果覆盖。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.database import SessionLocal
from app.services import dependency_service as ds
from app.services.dependency_service import DependencyComponentService


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def svc():
    return DependencyComponentService()


@pytest.fixture
def airflow_row(svc, db):
    """一行配好两条连接的 airflow 组件；用例改完由本 fixture 还原。"""
    row = svc._get_singleton(db, "airflow")
    created = row is None
    before = (
        (row.connection_json, row.deploy_spec_json, row.deploy_status,
         row.enabled, row.deploy_mode, row.name)
        if row
        else None
    )
    svc.save_airflow(db, {
        "endpoint": "http://airflow:8080", "username": "admin", "password": "pw",
        "ssh_password": "sshpw", "ssh_host": "airflow-host", "ssh_user": "deploy",
        "ssh_port": 22, "dags_dir": "/opt/airflow/dags", "enabled": True,
    })
    row = svc._get_singleton(db, "airflow")
    yield row
    db.expire_all()
    row = svc._get_singleton(db, "airflow")
    if created:
        db.delete(row)
    elif before:
        # 每一项都要还原：airflow 是全库单例，落下一项就会把「默认未启用」之类的
        # 相邻用例带塌（这行状态是跨用例共享的）。
        (row.connection_json, row.deploy_spec_json, row.deploy_status,
         row.enabled, row.deploy_mode, row.name) = before
    db.commit()


# --------------------------------------------------------------- 表单不重复


def test_ssh_password_appears_exactly_once_on_the_form(svc):
    """SSH 密码只在「DAG 投递」这一组出现——通用连接段与投递段各渲染一遍就成了重复填写。"""
    schema = svc.schema()
    groups = schema["connection_groups"]["airflow"]
    where = [g["id"] for g in groups if "ssh_password" in g["fields"]]
    assert where == ["ssh"]
    # 分组必须覆盖全部连接字段，否则被漏掉的字段在分节渲染下没人显示
    covered = [f for g in groups for f in g["fields"]]
    assert sorted(covered) == sorted(f[0] for f in ds.CONNECTION_SCHEMAS["airflow"])
    assert len(covered) == len(set(covered))  # 同一字段不属于两个分组


def test_ineffective_fields_are_not_configurable(svc):
    """token/api_version/私钥路径都已不是配置项（理由见 CONNECTION_SCHEMAS 注释）。"""
    conn_fields = {f["name"] for f in svc.schema()["connection_schemas"]["airflow"]}
    assert "token" not in conn_fields
    assert "api_version" not in conn_fields
    assert "ssh_key_path" not in ds.DependencyComponentService._AIRFLOW_EXTRA_FIELDS


def test_single_connection_components_get_a_default_group(svc):
    """没分组的组件回一条 default，前端一套渲染逻辑通吃。"""
    groups = svc.schema()["connection_groups"]["llm"]
    assert [g["id"] for g in groups] == ["default"]
    assert "api_key" in groups[0]["fields"]


# ----------------------------------------------------------------- 分开拨测


def _stub(monkeypatch, *, api=None, ssh=None):
    """把两个探针换成可控替身，记录各自被调了几次。"""
    calls: dict[str, int] = {"api": 0, "ssh": 0}

    def _make(kind, result):
        def _fn(conn, extra):
            calls[kind] += 1
            return result

        return _fn

    probes = dict(ds._PROBES)
    if api is not None:
        probes[("airflow", "api")] = _make("api", api)
    if ssh is not None:
        probes[("airflow", "ssh")] = _make("ssh", ssh)
    monkeypatch.setattr(ds, "_PROBES", probes)
    return calls


def test_target_probes_only_that_connection(svc, db, airflow_row, monkeypatch):
    calls = _stub(
        monkeypatch,
        api=ds.ProbeResult(True, "连接成功", 5),
        ssh=ds.ProbeResult(True, "可写", 9),
    )
    result = svc.probe(db, airflow_row.id, target="ssh")
    assert result.ok and calls == {"api": 0, "ssh": 1}
    assert [p["group"] for p in result.parts] == ["ssh"]


def test_probing_one_connection_does_not_mark_the_whole_row_connected(
    svc, db, airflow_row, monkeypatch
):
    """只测通了 SSH 不等于 Airflow 能用——调度 API 还没测过，行状态得如实停在未拨测。"""
    _stub(monkeypatch, api=ds.ProbeResult(False, "boom"), ssh=ds.ProbeResult(True, "可写", 9))
    svc.probe(db, airflow_row.id, target="ssh")
    db.refresh(airflow_row)
    assert airflow_row.deploy_status == "not_deployed"

    # 两条都测过且都通，才算已连接
    _stub(monkeypatch, api=ds.ProbeResult(True, "连接成功", 5))
    svc.probe(db, airflow_row.id, target="api")
    db.refresh(airflow_row)
    assert airflow_row.deploy_status == "connected"


def test_failed_connection_is_named_not_just_reddened(svc, db, airflow_row, monkeypatch):
    """一通一断时错误里要指名是哪条断了，否则用户只看到「Airflow 有问题」。"""
    _stub(
        monkeypatch,
        api=ds.ProbeResult(True, "连接成功", 5),
        ssh=ds.ProbeResult(False, "Permission denied"),
    )
    result = svc.probe(db, airflow_row.id)  # 不给 target = 全测
    assert result.ok is False
    assert "DAG 投递" in result.message and "Permission denied" in result.message
    db.refresh(airflow_row)
    assert airflow_row.deploy_status == "failed"
    assert "DAG 投递" in (airflow_row.deploy_error or "")
    # 明细逐条回传，前端据此显示「调度 API ✓ / DAG 投递 ✗」
    assert [(p["group"], p["ok"]) for p in result.parts] == [("api", True), ("ssh", False)]


def test_recovered_connection_clears_the_row_error(svc, db, airflow_row, monkeypatch):
    """断的那条修好后单独重测，行状态要跟着恢复——记账是逐条覆盖的。"""
    _stub(
        monkeypatch,
        api=ds.ProbeResult(True, "连接成功", 5),
        ssh=ds.ProbeResult(False, "Permission denied"),
    )
    svc.probe(db, airflow_row.id)
    _stub(monkeypatch, ssh=ds.ProbeResult(True, "可写", 9))
    svc.probe(db, airflow_row.id, target="ssh")
    db.refresh(airflow_row)
    assert airflow_row.deploy_status == "connected"
    assert airflow_row.deploy_error is None


def test_unknown_target_is_rejected_with_the_available_ones(svc, db, airflow_row):
    result = svc.probe(db, airflow_row.id, target="nope")
    assert result.ok is False
    assert "api" in result.message and "ssh" in result.message


def _ledger(row) -> dict:
    return ds._loads(row.deploy_spec_json).get("_probe", {})


def test_saving_config_invalidates_the_probe_ledger(svc, db, airflow_row, monkeypatch):
    """改了配置，上次拨测的结论就不再属于这份配置，逐条记账必须一起作废。

    只退行状态、不清记账的话：把填错的地址改对之后，那条连接仍挂着旧的红叉——
    「保存即绿灯」是假绿灯，「改对了还是红叉」是同一个毛病的镜像版。
    """
    _stub(monkeypatch, api=ds.ProbeResult(False, "解析不了这个主机名"), ssh=ds.ProbeResult(True, "可写", 9))
    svc.probe(db, airflow_row.id)
    db.refresh(airflow_row)
    assert set(_ledger(airflow_row)) == {"api", "ssh"}

    svc.save_airflow(db, {"endpoint": "http://airflow-fixed:8080"})
    db.refresh(airflow_row)
    assert _ledger(airflow_row) == {}
    assert airflow_row.deploy_status == "not_deployed"  # 保存≠可用，重测才有结论


def test_updating_connection_through_the_panel_also_invalidates(
    svc, db, airflow_row, monkeypatch
):
    """面板走的是 update_component 这条路，同样要作废（SSH 主机在 deploy_spec.extra 里）。"""
    _stub(monkeypatch, api=ds.ProbeResult(True, "连接成功", 5), ssh=ds.ProbeResult(False, "连不上"))
    svc.probe(db, airflow_row.id)
    svc.update_component(db, airflow_row.id, {
        "deploy_spec": {"extra": {"ssh_host": "另一台", "dags_dir": "/opt/airflow/dags"}},
    })
    db.refresh(airflow_row)
    assert _ledger(airflow_row) == {}


def test_renaming_keeps_the_probe_ledger(svc, db, airflow_row, monkeypatch):
    """只改展示名没碰任何连接参数，不该把拨测结果一起抹掉。"""
    _stub(monkeypatch, api=ds.ProbeResult(True, "连接成功", 5), ssh=ds.ProbeResult(True, "可写", 9))
    svc.probe(db, airflow_row.id)
    svc.update_component(db, airflow_row.id, {"name": "Airflow 生产"})
    db.refresh(airflow_row)
    assert set(_ledger(airflow_row)) == {"api", "ssh"}
    assert airflow_row.deploy_status == "connected"


def test_deploy_verifies_only_the_component_itself(svc, db, airflow_row, monkeypatch):
    """装完只验组件自身那条连接。

    DAG 投递是 ontoMeta 侧另配的一条连接，装机时通常还没配——若部署后按「全部连接」
    拨测，一次成功的安装会被那条没配的连接判成失败。
    """
    calls = _stub(
        monkeypatch,
        api=ds.ProbeResult(True, "连接成功", 5),
        ssh=ds.ProbeResult(False, "缺少 SSH 主机（DAG 产物没有地方可投）"),
    )
    airflow_row.deploy_mode = "bare_metal"
    airflow_row.deploy_spec_json = ds._dumps(
        {"ssh_host": "1.2.3.4", "ssh_user": "root", "port": 8081}
    )
    db.commit()
    installed = {"endpoint": "http://1.2.3.4:8081", "username": "admin", "password": "pw"}
    with patch("app.services.install_recipes.run_install", return_value=installed):
        result = svc.deploy(db, airflow_row.id)

    assert result["ok"] is True
    assert calls == {"api": 1, "ssh": 0}
    db.refresh(airflow_row)
    # 没测过的 DAG 投递不给 connected（那是假绿灯），但也不能报成未部署（那是假红叉）
    assert airflow_row.deploy_status == "deployed"
