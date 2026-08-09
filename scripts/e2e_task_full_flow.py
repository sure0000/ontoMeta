#!/usr/bin/env python3
"""端到端驱动：任务管理所有任务走完整流程。

覆盖：
  A. 四类独立任务（sync / transform / metric / materialize）各自走
     draft → validate → confirm → execute，源库/目标库/Flink/Airflow 真实触达；
  B. 任务链（materialize → transform → metric）：create → 逐步 advance（每步独立走完
     validate → confirm → execute）→ schedule(cron) → compile(周期 DAG) → lineage 预览。

环境要求：后端 :8000、Airflow :8081 可达、Flink bin + SqlRunner JAR 已配、源库 pg ok。
所有断言失败即整体失败；每步打印回执关键信号（dag_run_id / run_url / execute_mode）。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
TOKEN = "dev-admin-token-change-me"
HEADERS = {"X-Admin-Token": TOKEN, "Content-Type": "application/json"}

# 真实环境常量
ONTOLOGY_ID = "60125f9f-8fab-4f45-b0a0-7464de77cebe"          # 734 对象，已发布
PG_DATASOURCE_ID = "6dc0af33-5b8a-406d-bf46-97e3691aba02"     # pg (postgres, ok)
BRAND_COUNT_LOGIC_ID = "9ad86671-8f47-4e4c-9006-6aca3d03c4f1" # brand_count 口径

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


def main() -> int:
    # 0. 环境探活
    print("=== 0. 环境探活 ===")
    h = json.loads(urllib.request.urlopen(f"{API}/health").read())
    check("后端存活", h.get("status") == "ok", str(h))
    kinds = _req("GET", "/agents/kinds")
    check("四类任务全注册", set(kinds["registered"]) >= {"sync", "transform", "metric", "materialize"},
          str(kinds["registered"]))
    ds = _req("GET", "/data-sources")
    pg = next((d for d in ds if d["id"] == PG_DATASOURCE_ID), None)
    check("源库 pg 可用", pg and pg["status"] == "ok", str(pg and pg["status"]))
    af = _req("GET", "/settings/airflow")
    check("Airflow available", af.get("available") is True, str(af.get("available")))

    ctx_base = {"target_datasource_id": PG_DATASOURCE_ID, "target_database": "dw"}

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
        {**ctx_base, "business_logic_id": BRAND_COUNT_LOGIC_ID, "execution_mode": "batch"},
        expect_execute="flink",
    )

    # B. 任务链：materialize → transform → metric
    print("\n=== B. 任务链（create→advance×3→schedule→compile→lineage）===")
    chain = _req("POST", "/agents/pipelines", {
        "name": "e2e-brand-链",
        "intent": "物化 brand 到数仓后去重清洗，再按 brand_count 聚合",
        "ontology_id": ONTOLOGY_ID,
        "steps": [
            {"kind": "materialize", "intent": "物化 brand 到数仓",
             "context": {**ctx_base, "selected_targets": ["brand"]}},
            {"kind": "transform", "intent": "对 brand 去重清洗",
             "context": {"target_table": "brand", "cleansing_rules": ["deduplicate"]}},
            {"kind": "metric", "intent": "按 brand_count 聚合",
             "context": {"business_logic_id": BRAND_COUNT_LOGIC_ID}},
        ],
    })
    check("B: 建链只落意图(无制品)", all(s["artifact_id"] is None for s in chain["steps"]),
          str(chain["status"]))
    check("B: 链态=drafted", chain["status"] == "drafted", chain["status"])
    pid = chain["id"]

    # 逐步推进：每步独立走完 validate → confirm → execute，再 advance 下一步
    step_expect = ["airflow", "flink", "flink"]
    for i in range(3):
        adv = _req("POST", f"/agents/pipelines/{pid}/advance")
        art = adv["artifact"]
        check(f"B: advance 第{i+1}步 起草({art['kind']})", art["status"] == "drafted", art["status"])
        aid = art["id"]
        a = _req("POST", f"/agents/artifacts/{aid}/validate", {"context": {}})
        vr = a.get("validation_report") or {}
        check(f"B: 第{i+1}步 validate 无阻断", vr.get("blocking_count") == 0,
              f"blocking={vr.get('blocking_count')}")
        a = _req("POST", f"/agents/artifacts/{aid}/confirm", {"operator": "e2e-tester"})
        check(f"B: 第{i+1}步 confirmed", a["status"] == "confirmed", a["status"])
        a = _req("POST", f"/agents/artifacts/{aid}/execute", {"context": {}})
        rc = a.get("execution_receipt") or {}
        show_receipt(f"B.step{i+1}", rc)
        exp = step_expect[i]
        if exp == "flink":
            check(f"B: 第{i+1}步 走 Flink", rc.get("execute_mode") == "flink_on_yarn",
                  str(rc.get("execute_mode")))
        else:
            check(f"B: 第{i+1}步 触达 Airflow", bool(rc.get("dag_run_id")),
                  str(rc.get("execute_mode")))
        check(f"B: 第{i+1}步 succeeded", a["status"] == "succeeded", a["status"])

    chain = _req("GET", f"/agents/pipelines/{pid}")
    check("B: 链全部成功", chain["status"] == "succeeded", chain["status"])
    check("B: 无下一步", chain["next_step_index"] is None, str(chain["next_step_index"]))

    # 周期调度：设 cron → 编译成一条周期 DAG
    chain = _req("PUT", f"/agents/pipelines/{pid}/schedule", {"schedule_cron": "0 2 * * *"})
    check("B: 设 cron=0 2 * * *", chain["schedule_cron"] == "0 2 * * *", str(chain["schedule_cron"]))

    compiled = _req("POST", f"/agents/pipelines/{pid}/compile")
    check("B: 编译出周期 DAG", bool(compiled.get("compiled_dag_id")), str(compiled))
    check("B: DAG 落盘", bool(compiled.get("dag_path")), str(compiled.get("dag_path")))
    print(f"      compiled_dag_id = {compiled.get('compiled_dag_id')}")
    print(f"      dag_path        = {compiled.get('dag_path')}")

    chain = _req("GET", f"/agents/pipelines/{pid}")
    check("B: 链记录 compiled_dag_id", bool(chain["compiled_dag_id"]), str(chain["compiled_dag_id"]))

    # 链级血缘预览
    lineage = _req("GET", f"/agents/pipelines/{pid}/lineage")
    check("B: 血缘预览有返回", isinstance(lineage, (list, dict)) and lineage is not None,
          str(lineage)[:200])
    print(f"      lineage preview = {json.dumps(lineage, ensure_ascii=False)[:300]}")

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
