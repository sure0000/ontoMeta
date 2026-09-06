#!/usr/bin/env python3
"""端到端驱动：四类任务各走一遍完整流程。

覆盖四类独立任务（sync / transform / metric / materialize）各自走
draft → validate → confirm → execute，源库/目标库/Flink/Airflow 真实触达。

原先还有 B 段「任务链」（create → advance → schedule → compile → lineage）。
**手工任务链已随六环确认一起删除**，那 5 个 ``/agents/pipelines*`` 端点后端不再存在，
B 段跑起来只会得到一串 404，故整段移除。跨任务的依赖现在由 lineage_scheduler 推。

环境要求：后端 :8000、Airflow :8081 可达、Flink bin + SqlRunner JAR 已配、源库可用。
所有断言失败即整体失败；每步打印回执关键信号（dag_run_id / run_url / execute_mode）。

**环境相关的 id 走环境变量**，不写死——写死的 id 换台机器就全错，而报出来的症状
是「源库不可用」之类完全指错方向的话：

    E2E_ONTOLOGY_ID=... E2E_DATASOURCE_ID=... E2E_LOGIC_ID=... python scripts/e2e_task_full_flow.py

不给则自动挑：唯一的已发布本体 / 唯一状态 ok 的源 / 第一条业务口径；挑不出来就报错说清楚。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("ONTOMETA_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ONTOMETA_ADMIN_TOKEN", "dev-admin-token-change-me")
HEADERS = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}

# 环境相关的 id：给了就用，没给就在 resolve_ids() 里挑。
ONTOLOGY_ID = os.environ.get("E2E_ONTOLOGY_ID", "")
PG_DATASOURCE_ID = os.environ.get("E2E_DATASOURCE_ID", "")
BRAND_COUNT_LOGIC_ID = os.environ.get("E2E_LOGIC_ID", "")

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
results: list[tuple[str, bool, str]] = []


def _req(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{API}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            err = json.loads(err)
        except Exception:
            pass
        raise RuntimeError(f"{method} {path} -> {e.code}: {err}") from e


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        print(f"      !!! {detail}")


def show_receipt(kind: str, receipt: dict) -> None:
    keys = ("execute_mode", "dag_id", "dag_run_id", "run_url", "state", "handoff", "note", "ok")
    picked = {k: receipt.get(k) for k in keys if k in receipt}
    print(f"      [{kind}] 回执: {json.dumps(picked, ensure_ascii=False)[:400]}")


def full_flow(label: str, kind: str, intent: str, context: dict, *, expect_execute: str) -> dict:
    """单制品完整流程：draft → validate → confirm → execute。返回最终制品。"""
    print(f"\n▶ {label} ({kind})")
    art = _req("POST", "/agents/draft",
               {"kind": kind, "intent": intent,
                "context": {**context, "ontology_id": ONTOLOGY_ID},
                "ontology_id": ONTOLOGY_ID})
    check(f"{label}: draft 状态=drafted", art["status"] == "drafted", art["status"])
    aid = art["id"]

    art = _req("POST", f"/agents/artifacts/{aid}/validate", {"context": {}})
    vr = art.get("validation_report") or {}
    check(f"{label}: validate 通过(无阻断)", vr.get("blocking_count") == 0,
          f"blocking={vr.get('blocking_count')}")

    art = _req("POST", f"/agents/artifacts/{aid}/confirm", {"operator": "e2e-tester"})
    check(f"{label}: confirm 状态=confirmed", art["status"] == "confirmed", art["status"])

    art = _req("POST", f"/agents/artifacts/{aid}/execute", {"context": {}})
    receipt = art.get("execution_receipt") or {}
    show_receipt(label, receipt)
    if expect_execute == "flink":
        check(f"{label}: execute 走 Flink", receipt.get("execute_mode") == "flink_on_yarn",
              str(receipt.get("execute_mode")))
        check(f"{label}: 有 dag_run_id", bool(receipt.get("dag_run_id")))
        check(f"{label}: 有 run_url", bool(receipt.get("run_url")))
    elif expect_execute == "airflow":
        check(f"{label}: execute 触达 Airflow", bool(receipt.get("dag_run_id")),
              str(receipt.get("execute_mode")))
        check(f"{label}: 有 run_url", bool(receipt.get("run_url")))
    elif expect_execute == "handoff":
        check(f"{label}: 退回仅产出(显式说明)", bool(receipt.get("handoff") or receipt.get("note")),
              str(receipt.get("handoff")))
    check(f"{label}: execute 状态=succeeded", art["status"] == "succeeded", art["status"])
    return art


def resolve_ids() -> tuple[str, str, str]:
    """本体 / 目标数据源 / 业务口径：给了就用，没给就挑，挑不出来就说清为什么。

    写死 id 的老问题是换台机器全错，而报出来的是「源库不可用」这类指错方向的话。
    """
    ontology = ONTOLOGY_ID
    if not ontology:
        published = [o for o in _req("GET", "/ontologies?limit=50") or [] if o.get("status") == "published"]
        if len(published) != 1:
            raise SystemExit(
                f"已发布本体有 {len(published)} 个，选不出来；请设 E2E_ONTOLOGY_ID"
            )
        ontology = published[0]["id"]

    datasource = PG_DATASOURCE_ID
    if not datasource:
        usable = [d for d in _req("GET", "/data-sources") or [] if (d.get("status") or "") == "ok"]
        if len(usable) != 1:
            raise SystemExit(
                f"状态 ok 的数据源有 {len(usable)} 个，选不出来；请设 E2E_DATASOURCE_ID"
            )
        datasource = usable[0]["id"]

    logic = BRAND_COUNT_LOGIC_ID
    if not logic:
        logics = _req("GET", f"/business-logics?ontology_id={ontology}") or []
        if not logics:
            raise SystemExit("这个本体下没有业务口径，metric 任务跑不了；请设 E2E_LOGIC_ID")
        logic = logics[0]["id"]

    return ontology, datasource, logic


def main() -> int:
    # 0. 环境探活
    print("=== 0. 环境探活 ===")
    h = json.loads(urllib.request.urlopen(f"{API}/health").read())
    check("后端存活", h.get("status") == "ok", str(h))
    kinds = _req("GET", "/agents/kinds")
    check("四类任务全注册", set(kinds["registered"]) >= {"sync", "transform", "metric", "materialize"},
          str(kinds["registered"]))

    ontology_id, datasource_id, logic_id = resolve_ids()
    print(f"      本体={ontology_id} 数据源={datasource_id} 口径={logic_id}")

    ds = _req("GET", "/data-sources")
    target = next((d for d in ds if d["id"] == datasource_id), None)
    check("目标数据源可用", bool(target) and target["status"] == "ok", str(target and target["status"]))
    af = _req("GET", "/settings/airflow")
    check("Airflow available", af.get("available") is True, str(af.get("available")))

    ctx_base = {"target_datasource_id": datasource_id, "target_database": "dw"}

    # A. 四类独立任务完整流程
    print("\n=== A. 四类独立任务（draft→validate→confirm→execute）===")

    # A1. materialize — 落 pg 仓，物化 brand/country 两张维表，触达 Airflow
    full_flow(
        "A1.materialize", "materialize", "物化 brand 与 country 到数仓",
        {**ctx_base, "selected_targets": ["brand", "country"], "load_strategy": "full"},
        expect_execute="airflow",
    )

    # A2. sync — 搬运 brand，目标 pg → 触达 Airflow 搬运通道
    full_flow(
        "A2.sync", "sync", "把 brand 源表同步到数仓",
        {**ctx_base, "object_type": "brand", "mode": "full"},
        expect_execute="airflow",
    )

    # A3. transform — 清洗 brand（去重+空值），目标 pg → 走 Flink on YARN（经 Airflow 触发）
    full_flow(
        "A3.transform", "transform", "对 brand 去重并过滤空值",
        {**ctx_base, "target_table": "brand", "cleansing_rules": ["deduplicate", "drop_null"],
         "execution_mode": "batch"},
        expect_execute="flink",
    )

    # A4. metric — brand_count 口径聚合，目标 pg → 走 Flink on YARN
    full_flow(
        "A4.metric", "metric", "按 brand_count 口径聚合",
        {**ctx_base, "business_logic_id": logic_id, "execution_mode": "batch"},
        expect_execute="flink",
    )

    # 汇总
    print("\n=== 汇总 ===")
    ok = sum(1 for _, c, _ in results if c)
    total = len(results)
    for name, c, detail in results:
        if not c:
            print(f"  {FAIL} {name} — {detail}")
    print(f"\n通过 {ok}/{total} 项断言")
    return 0 if ok == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n{FAIL} 致命错误: {e}")
        import traceback; traceback.print_exc()
        sys.exit(2)
