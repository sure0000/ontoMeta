"""SyncTool Adapter：作业配置渲染的结构、幂等、以及「凭据绝不进产物」。

不连任何外部系统——渲染是纯函数。真实提交到 SeaTunnel 属 M10 的集成验证。
"""

from __future__ import annotations

import json
import re

import pytest

from app.warehouse.jobs import (
    ColumnMapping,
    JobEndpoint,
    JobSpec,
    UnknownSyncToolError,
    get_job_adapter,
    list_sync_tools,
)


def _job(mode: str = "full", *, platform: str = "mariadb", partition_key=None) -> JobSpec:
    return JobSpec(
        name=f"sync_dim_customer_{mode}",
        source=JobEndpoint(
            alias="erp_readonly", platform=platform, database="erp_ods", table="tab_customer"
        ),
        target=JobEndpoint(
            alias="warehouse_default", platform="hive", database="dim", table="customer"
        ),
        columns=(
            ColumnMapping(source="cust_id", target="customer_id"),
            ColumnMapping(source="cust_name", target="customer_name"),
        ),
        mode=mode,
        partition_key=partition_key,
    )


def test_registry_defaults_to_seatunnel():
    assert get_job_adapter().name == "seatunnel"
    assert get_job_adapter("seatunnel").name == "seatunnel"
    assert "seatunnel" in list_sync_tools()
    with pytest.raises(UnknownSyncToolError):
        get_job_adapter("no-such-tool")


def test_render_has_four_sections_and_column_mapping():
    conf = get_job_adapter().render(_job())
    assert set(conf) == {"env", "source", "transform", "sink"}
    assert conf["env"]["job.mode"] == "BATCH"
    query = conf["source"][0]["query"]
    # 列映射体现为 SELECT 源列 AS 目标列
    assert "`cust_id` AS `customer_id`" in query
    assert "FROM erp_ods.tab_customer" in query
    assert conf["sink"][0]["database"] == "dim"
    assert conf["sink"][0]["table"] == "customer"


def test_no_credentials_in_rendered_config():
    """产物里只能出现别名派生的占位符，绝不能有真实连接信息。"""
    conf = get_job_adapter().render(_job())
    blob = json.dumps(conf, ensure_ascii=False)
    # 所有 user/password/url 位置都必须是 ${...} 占位符
    for key in ("user", "password", "url"):
        for value in re.findall(rf'"{key}"\s*:\s*"([^"]*)"', blob):
            assert value.startswith("${") and value.endswith("}"), (key, value)
    assert "${ERP_READONLY_URL}" in blob
    assert "${WAREHOUSE_DEFAULT_URL}" not in blob  # hive sink 用 metastore_uri
    assert "${WAREHOUSE_DEFAULT_METASTORE_URI}" in blob


def test_render_is_idempotent():
    """同一 JobSpec 重复渲染逐字节一致（沿用 M3 的幂等要求）。"""
    adapter = get_job_adapter()
    spec = _job()
    a = json.dumps(adapter.render(spec), sort_keys=True, ensure_ascii=False)
    b = json.dumps(adapter.render(spec), sort_keys=True, ensure_ascii=False)
    assert a == b


def test_sink_never_auto_creates_table():
    """建表只能走 M3 的 DDL——sink 自动建表会绕过本体反补的注释/分区/主键声明。"""
    conf = get_job_adapter().render(_job())
    assert conf["sink"][0]["save_mode_create_template"] == "NONE"


def test_incremental_adds_watermark_predicate():
    conf = get_job_adapter().render(_job("incremental", partition_key="created_at"))
    source = conf["source"][0]
    assert source["partition_column"] == "created_at"
    assert "${watermark}" in source["query"]


def test_incremental_without_partition_key_has_no_predicate():
    """没有分区键就不能凭空造谓词——planner 已就此记 unsupported 提示。"""
    conf = get_job_adapter().render(_job("incremental"))
    assert "${watermark}" not in conf["source"][0]["query"]
    assert "partition_column" not in conf["source"][0]


def test_cdc_uses_cdc_plugin_and_streaming_mode():
    conf = get_job_adapter().render(_job("cdc"))
    assert conf["env"]["job.mode"] == "STREAMING"
    assert conf["source"][0]["plugin_name"] == "MySQL-CDC"


def test_cdc_from_unsupported_platform_raises():
    """宁可显式报错，也不悄悄退回全量——那会改变数据语义。"""
    adapter = get_job_adapter()
    assert adapter.supports_cdc_from("mariadb") is True
    assert adapter.supports_cdc_from("oracle") is False
    with pytest.raises(ValueError, match="CDC"):
        adapter.render(_job("cdc", platform="oracle"))


def test_unknown_mode_rejected_at_spec_level():
    with pytest.raises(ValueError, match="装载方式"):
        _job("no-such-mode")


# ---------- 工具可插拔：datax / flink ----------


def test_registry_lists_three_tools():
    tools = list_sync_tools()
    assert set(tools) >= {"seatunnel", "datax", "flink"}


def test_each_adapter_declares_image_and_command():
    """镜像与命令属工具自身（不在设置页）；command 携带水位模板供 Airflow 渲染。"""
    for tool in ("seatunnel", "datax", "flink"):
        a = get_job_adapter(tool)
        assert a.docker_image
        cmd = a.airflow_command("/jobs/x.json")
        assert isinstance(cmd, list) and "/jobs/x.json" in cmd
        assert any("data_interval_start" in part for part in cmd)


def test_datax_renders_reader_writer_and_no_credentials():
    conf = get_job_adapter("datax").render(_job())
    content = conf["job"]["content"][0]
    assert content["reader"]["name"] == "mysqlreader"
    assert content["writer"]["name"] == "hdfswriter"
    # 列映射：reader 取源列、writer 写目标列（靠列序对齐）
    assert content["reader"]["parameter"]["column"] == ["cust_id", "cust_name"]
    assert content["writer"]["parameter"]["column"] == ["customer_id", "customer_name"]
    blob = json.dumps(conf, ensure_ascii=False)
    assert "${ERP_READONLY_PASSWORD}" in blob
    for value in re.findall(r'"password"\s*:\s*"([^"]*)"', blob):
        assert value.startswith("${")


def test_datax_has_no_cdc():
    a = get_job_adapter("datax")
    assert a.supports("full") is True
    assert a.supports("incremental") is True
    assert a.supports("cdc") is False


def test_datax_incremental_uses_watermark_where():
    conf = get_job_adapter("datax").render(_job("incremental", partition_key="created_at"))
    where = conf["job"]["content"][0]["reader"]["parameter"]["where"]
    assert "${watermark}" in where and "created_at" in where


def test_flink_supports_cdc_and_renders_pipeline():
    a = get_job_adapter("flink")
    assert a.supports("cdc") is True
    assert a.supports_cdc_from("mariadb") is True
    assert a.supports_cdc_from("oracle") is False
    conf = a.render(_job("cdc"))
    assert conf["source"]["connector"] == "mysql-cdc"
    assert conf["sink"]["connector"] == "hive"
    # 列改名在 transform 段：源列 → 目标列
    assert {"source": "cust_id", "target": "customer_id"} in conf["transform"]


def test_flink_no_credentials_and_no_auto_create():
    conf = get_job_adapter("flink").render(_job())
    assert conf["sink"]["sink.auto-create"] is False
    blob = json.dumps(conf, ensure_ascii=False)
    for key in ("username", "password", "url"):
        for value in re.findall(rf'"{key}"\s*:\s*"([^"]*)"', blob):
            assert value.startswith("${")


def test_all_adapters_render_idempotently():
    spec = _job()
    for tool in ("seatunnel", "datax", "flink"):
        a = get_job_adapter(tool)
        x = json.dumps(a.render(spec), sort_keys=True, ensure_ascii=False)
        y = json.dumps(a.render(spec), sort_keys=True, ensure_ascii=False)
        assert x == y
