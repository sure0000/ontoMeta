"""seatunnel 档：能力声明、挑档、渲染、版本门禁。

Zeta 集群用假的 HTTP 替身，不需要真集群——真集群上的验证是 `make smoke`。
"""

from __future__ import annotations

import json

import pytest

from sync_runner.backends import capabilities, pick_backend, seatunnel
from sync_runner.contract import WireColumn, WireEndpoint, WireJobSpec


def _spec(target_platform="hive", mode="full", target_alias="warehouse_default"):
    return WireJobSpec(
        name="sync_dim_customer",
        source=WireEndpoint(
            alias="erp_readonly", platform="mariadb", database="erp", table="tabCustomer"
        ),
        target=WireEndpoint(
            alias=target_alias, platform=target_platform, database="dim", table="customer"
        ),
        columns=[WireColumn(source="name", target="customer_name")],
        mode=mode,
        partition_key="modified" if mode == "incremental" else None,
    )


@pytest.fixture
def zeta(monkeypatch):
    """把 Zeta 换成可编程的替身，返回记录下来的请求。"""
    monkeypatch.setenv("SEATUNNEL_REST_ENDPOINT", "http://zeta:8080")
    calls: list[tuple[str, str, dict | None]] = []
    state = {"version": "2.3.11", "status": "FINISHED", "error": None}

    def fake_http(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path == "/overview":
            return {"projectVersion": state["version"]}
        if path.startswith("/submit-job"):
            return {"jobId": "42", "jobName": "x"}
        if path.startswith("/job-info/"):
            return {
                "jobStatus": state["status"],
                "errorMsg": state["error"],
                # 两种形状都要认：按表的 map 与标量
                "metrics": {"SourceReceivedCount": {"t": "7"}, "SinkWriteCount": "7"},
            }
        raise AssertionError(f"没预期的调用 {path}")

    monkeypatch.setattr(seatunnel, "_http", fake_http)
    return {"calls": calls, "state": state}


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("SYNC_CONN_ERP_READONLY_URL", "mysql+pymysql://ro:pw@erp-db:3306/erp")
    monkeypatch.setenv("SYNC_CONN_WAREHOUSE_DEFAULT_METASTORE_URI", "thrift://hms:9083")


# ---------- 能力与挑档 ----------


def test_hive_is_available_only_when_zeta_is(monkeypatch, zeta):
    caps = capabilities()
    assert "seatunnel" in caps.backends
    assert "hive" in caps.sinks
    # Hive 只做全量：增量要回读目标水位，runner 读不了 Hive（见 SINK_MODES 说明）
    assert caps.sink_modes["hive"] == ["full"]

    monkeypatch.delenv("SEATUNNEL_REST_ENDPOINT")
    caps = capabilities()
    assert "seatunnel" not in caps.backends
    assert "hive" not in caps.sinks


def test_unsupported_zeta_version_disables_the_tier(zeta):
    zeta["state"]["version"] = "2.4.0"
    assert seatunnel.available() is False
    assert "hive" not in capabilities().sinks


def test_native_is_preferred_and_seatunnel_picks_up_hive(zeta):
    # sqlite→sqlite 归 native（不依赖外部集群、增量能回读水位）
    native_job = _spec(target_platform="sqlite")
    native_job.source.platform = "sqlite"
    assert pick_backend(native_job) == "native"
    # hive 只有 seatunnel 能接
    assert pick_backend(_spec()) == "seatunnel"


def test_hive_incremental_has_no_backend(zeta):
    """不静默降级：Hive 增量两档都不支持，挑不出档而不是退回全量。"""
    assert pick_backend(_spec(mode="incremental")) is None


# ---------- 渲染 ----------


def test_hive_render_shape(zeta):
    config = seatunnel.render(_spec())
    source, sink = config["source"][0], config["sink"][0]

    assert source["plugin_name"] == "Jdbc"
    # JDBC url 不能照搬 SQLAlchemy 的 driver 段（mysql+pymysql 是 Python 侧的事）
    assert source["url"] == "jdbc:mariadb://erp-db:3306/erp"
    assert source["driver"] == "org.mariadb.jdbc.Driver"
    assert source["user"] == "ro" and source["password"] == "pw"
    assert "`name` AS `customer_name`" in source["query"]

    # Hive sink 只认一个 table_name（2.3.11 实测），拆成 database/table 会报缺参数
    assert sink["plugin_name"] == "Hive"
    assert sink["table_name"] == "dim.customer"
    assert "database" not in sink and "table" not in sink
    assert sink["metastore_uri"] == "thrift://hms:9083"
    # source 与 sink 必须靠 plugin_output/plugin_input 接上，否则 sink 收不到数据
    assert source["plugin_output"] == sink["plugin_input"]


def test_missing_metastore_uri_says_exactly_what_to_set(zeta, monkeypatch):
    monkeypatch.delenv("SYNC_CONN_WAREHOUSE_DEFAULT_METASTORE_URI")
    with pytest.raises(seatunnel.SeaTunnelError) as exc:
        seatunnel.render(_spec())
    assert "METASTORE_URI" in str(exc.value)


def test_render_is_deterministic(zeta):
    """同一 spec 反复渲染逐字节一致（沿用生成侧的幂等要求）。"""
    a = json.dumps(seatunnel.render(_spec()), sort_keys=True)
    b = json.dumps(seatunnel.render(_spec()), sort_keys=True)
    assert a == b


# ---------- 执行 ----------


def test_run_submits_polls_and_reports_rows(zeta):
    result = seatunnel.run(_spec(), poll_seconds=0)
    assert (result.rows_read, result.rows_written) == (7, 7)
    assert result.job_id == "42"
    paths = [p for _, p, _ in zeta["calls"]]
    assert any(p.startswith("/submit-job?jobName=sync_dim_customer") for p in paths)
    assert "/job-info/42" in paths


def test_failed_job_surfaces_zeta_error_message(zeta):
    zeta["state"]["status"] = "FAILED"
    zeta["state"]["error"] = "table not found"
    with pytest.raises(seatunnel.SeaTunnelError) as exc:
        seatunnel.run(_spec(), poll_seconds=0)
    assert "table not found" in str(exc.value)
