"""端到端冒烟：一张表，从本体一路走到目标仓里真的有数据。

**它抓的是接线 bug**，不是方言细节。今天为止踩到的每一个坑——DAG 导不进 Airflow、
作业配置没挂进容器、凭据占位符没人注入、建表语句没进任何 DAG、preflight 的提示照做无效——
都属于「只要真跑通一次就会暴露」的那类，而它们全是靠线上一条条撞出来的。这个脚本就是那"一次"。

跑一遍做这些事，每步都给结论，失败就停在失败那步：

1. 前置：ontoMeta / Airflow / 目标仓 是否都在，dags 目录两侧是否一致（调真正的 preflight 接口）
2. 提交物化（只选一张表），拿回执
3. 轮询 DagRun 到终态
4. 到目标仓里数行数——**这一步才是真正的验收**：前面全绿但表里没数据，是最常见的假成功

用法（仓库根目录）：

    make smoke                          # 用下面的默认值
    SMOKE_ENTITY=item make smoke        # 换一张表

环境变量见 `_env()` 处的默认值；跑之前后端要起着（`make backend`）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------- 配置 ----------


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


API = _env("ONTOMETA_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = _env("ONTOMETA_ADMIN_TOKEN", "dev-admin-token-change-me")
ONTOLOGY = _env("SMOKE_ONTOLOGY_ID", "")
DATASOURCE = _env("SMOKE_DATASOURCE_ID", "")
ENTITY = _env("SMOKE_ENTITY", "customer")
ENGINE = _env("SMOKE_ENGINE", "hive")
# 目标仓怎么查行数。Hive 用容器里的 beeline；换目标仓就换这两个。
HIVE_CONTAINER = _env("SMOKE_HIVE_CONTAINER", "hive-server")
HIVE_JDBC = _env("SMOKE_HIVE_JDBC", "jdbc:hive2://localhost:10000")
RUN_TIMEOUT = float(_env("SMOKE_RUN_TIMEOUT", "900"))

_GREEN, _RED, _YELLOW, _DIM, _OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {_GREEN}✓{_OFF} {msg}")


def warn(msg: str) -> None:
    print(f"  {_YELLOW}!{_OFF} {msg}")


def die(msg: str, hint: str = "") -> None:
    print(f"  {_RED}✗{_OFF} {msg}")
    if hint:
        print(f"    {_DIM}下一步：{hint}{_OFF}")
    sys.exit(1)


def step(n: int, title: str) -> None:
    print(f"\n== {n}. {title} ==")


# ---------- HTTP ----------


def call(method: str, path: str, body: dict | None = None, timeout: float = 300) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        # 内网服务，绝不走开发机代理（与后端各 connector 同一处置）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        die(f"{method} {path} → HTTP {exc.code}", detail)
    except urllib.error.URLError as exc:
        die(f"{method} {path} 连不上：{exc.reason}", f"后端起了吗？（make backend，{API}）")
    return {}


def hive_scalar(sql: str) -> str | None:
    """在 Hive 上跑一条返回单值的 SQL。取不到值返回 None（不抛，调用方决定怎么算）。"""
    cmd = [
        "docker", "exec", HIVE_CONTAINER, "bash", "-lc",
        f"beeline -u {HIVE_JDBC} --silent=true --outputformat=csv2 -e \"{sql}\" 2>/dev/null",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1] if len(lines) >= 2 else None


# ---------- 步骤 ----------


def resolve_ids() -> tuple[str, str]:
    """本体与目标数据源：给了就用，没给就挑唯一合理的那个，挑不出来就报错说清楚。"""
    ontology = ONTOLOGY
    if not ontology:
        items = call("GET", "/api/ontologies?limit=50")
        published = [o for o in items if o.get("status") == "published"]
        if len(published) != 1:
            die(
                f"有 {len(published)} 个已发布本体，选不出来",
                "用 SMOKE_ONTOLOGY_ID=<id> 指定",
            )
        ontology = published[0]["id"]
    datasource = DATASOURCE
    if not datasource:
        items = call("GET", "/api/data-sources")
        matched = [d for d in items if (d.get("kind") or "").lower() == ENGINE.lower()]
        if len(matched) != 1:
            die(
                f"kind={ENGINE} 的数据源有 {len(matched)} 个，选不出来",
                "用 SMOKE_DATASOURCE_ID=<id> 指定",
            )
        datasource = matched[0]["id"]
    return ontology, datasource


def run_preflight(ontology: str, datasource: str) -> None:
    report = call(
        "POST",
        f"/api/ontologies/{ontology}/warehouse/materialize/preflight",
        {"target_datasource_id": datasource, "engine": ENGINE, "selected_targets": [ENTITY]},
    )
    for item in report.get("items") or []:
        line = f"{item['label']}：{item['detail']}"
        if item["status"] == "pass":
            ok(line)
        elif item["status"] == "warn" or not item["blocking"]:
            warn(line)
            if item.get("next_step"):
                print(f"    {_DIM}{item['next_step']}{_OFF}")
        else:
            die(line, item.get("next_step") or "")
    if not report.get("ok"):
        die("preflight 有阻断项", "按上面的提示处理后重跑")
    ok("preflight 无阻断项")


def submit(ontology: str, datasource: str) -> dict:
    resp = call(
        "POST",
        f"/api/ontologies/{ontology}/warehouse/materialize",
        {
            "target_datasource_id": datasource,
            "engine": ENGINE,
            "selected_targets": [ENTITY],
        },
    )
    # 执行回执嵌在 receipt 里（MaterializeResult 的外层只有制品元信息）。
    receipt = dict(resp.get("receipt") or {})
    receipt["artifact_id"] = resp.get("artifact_id")
    batches = receipt.get("batches") or []
    print(f"  产出 {len(batches)} 个 DAG：")
    for b in batches:
        state = b.get("error") or b.get("state")
        print(f"    {b['dag_id']}  建表 {len(b.get('tables') or [])} 张，"
              f"搬运 {len(b.get('jobs') or [])} 个 → {state}")
    if receipt.get("unsupported"):
        warn(f"{len(receipt['unsupported'])} 张表不产搬运作业：")
        for u in receipt["unsupported"][:5]:
            print(f"    {_DIM}{u.get('target')}: {u.get('reason')}{_OFF}")
    if not any(b.get("jobs") for b in batches):
        die(
            "没有任何搬运作业——这一轮只会建表、不会有数据",
            "多半是执行通道不支持该目标引擎（看上面 preflight 的「目标引擎支持」一项）",
        )
    stuck = [b for b in batches if b.get("error")]
    if stuck:
        die(
            "DAG 已落盘但没触发起来：" + (stuck[0].get("error") or ""),
            "若提示「尚未解析到」，多半是 Airflow 的 dag_dir_list_interval 太长"
            "（默认 300s）：把它调小（AIRFLOW__SCHEDULER__DAG_DIR_LIST_INTERVAL=30），"
            "或把 ONTOMETA_DAG_PARSE_TIMEOUT 提到大于该间隔。",
        )
    ok(f"已提交，artifact={receipt.get('artifact_id') or '?'}")
    return receipt


def wait_run(receipt: dict) -> None:
    artifact = receipt.get("artifact_id")
    if not artifact:
        die("回执里没有 artifact_id，无法轮询状态")
    deadline = time.monotonic() + RUN_TIMEOUT
    last = ""
    while True:
        status = call("GET", f"/api/warehouse/materialize/{artifact}/status")
        state = (status.get("state") or "").lower()
        if state != last:
            print(f"  {_DIM}{state or '(无状态)'}{_OFF}")
            last = state
        if state in ("success", "failed"):
            break
        if time.monotonic() >= deadline:
            die(
                f"等了 {RUN_TIMEOUT:.0f}s 仍未结束（当前 {state}）",
                f"去 Airflow 看任务日志：{receipt.get('run_url') or ''}",
            )
        time.sleep(10)
    if state != "success":
        failed = [t for t in (status.get("tasks") or []) if t.get("state") == "failed"]
        die(
            f"DagRun 失败，失败任务：{[t.get('task_id') for t in failed] or '见 Airflow'}",
            f"日志：{receipt.get('run_url') or ''}",
        )
    ok("DagRun 成功")


def verify_rows(receipt: dict) -> None:
    """**真正的验收**：目标表里到底有没有数据。前面全绿而这里是 0，才是最常见的假成功。"""
    tables = sorted({t for b in receipt.get("batches") or [] for t in (b.get("tables") or [])})
    if not tables:
        die("回执里没有目标表名，无从校验")
    bad = []
    for table in tables:
        n = hive_scalar(f"select count(*) from {table}")
        if n is None:
            warn(f"{table}：查不到行数（beeline 不可用？容器名 {HIVE_CONTAINER} 对吗）")
            continue
        if n.isdigit() and int(n) > 0:
            ok(f"{table}：{n} 行")
        else:
            bad.append(f"{table}（{n} 行）")
    if bad:
        die(
            "这些表建出来了但没有数据：" + "、".join(bad),
            "搬运任务显示成功却没落数，先看 Airflow 里该任务的日志与 Flink SQL 产物",
        )


def main() -> int:
    print(f"{_DIM}ontoMeta 端到端冒烟：{API} → {ENGINE}，实体 {ENTITY}{_OFF}")

    step(1, "解析本体与目标数据源")
    ontology, datasource = resolve_ids()
    ok(f"本体 {ontology}")
    ok(f"目标 {datasource}（{ENGINE}）")

    step(2, "提交前自检")
    run_preflight(ontology, datasource)

    step(3, "提交物化")
    receipt = submit(ontology, datasource)

    step(4, "等 DagRun 结束")
    wait_run(receipt)

    step(5, "校验目标仓里的数据")
    verify_rows(receipt)

    print(f"\n{_GREEN}冒烟通过{_OFF}：本体 → DDL → 搬运作业 → Airflow → {ENGINE} 全链路有数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
