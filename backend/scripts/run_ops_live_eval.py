#!/usr/bin/env python3
"""运行 Data Agent V6 P3 的真实 LLM 运营问题回放。

默认是安全 dry-run，不发网络请求。真实调用必须显式设置 ``DATA_AGENT_LIVE=1``：

    DATA_AGENT_LIVE=1 python scripts/run_ops_live_eval.py --domain-id <id>

脚本读取设置页中的 LLM 配置和当前已发布本体，逐题创建临时会话，调用完成后删除会话。
输出 JSON 只含路由/接地/拒答/时延/调用次数，不输出 API Key、DSN 或完整回答。
原始 JSON 默认写到 stdout；用 ``--output`` 可另存文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# 从 backend/ 直接执行脚本时确保 app 与 tests 可导入。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings as env_settings
from app.database import SessionLocal
from app.models import DomainContext, Ontology, OntologyStatus
from app.services.chat_bi import ChatBiService
from app.services.ops_live_eval import (
    OpsLiveCaseResult,
    evaluate_ops_payload,
    summarize_ops_results,
)
from app.services.settings_service import SettingsService
from tests.fixtures.ops_questions import OPS_QUESTIONS, OpsQuestion


def _published_scopes() -> list[tuple[DomainContext, Ontology]]:
    with SessionLocal() as db:
        return (
            db.query(DomainContext, Ontology)
            .join(Ontology, Ontology.domain_context_id == DomainContext.id)
            .filter(Ontology.status == OntologyStatus.PUBLISHED.value)
            .order_by(DomainContext.name.asc())
            .all()
        )


def _trace_records(trace_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(trace_dir.glob("agent-trace-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def _select_cases(args: argparse.Namespace) -> list[OpsQuestion]:
    selected = OPS_QUESTIONS
    if args.question_id:
        wanted = set(args.question_id)
        selected = [case for case in selected if case.id in wanted]
        missing = sorted(wanted - {case.id for case in selected})
        if missing:
            raise SystemExit(f"未知问题 ID：{', '.join(missing)}")
    if args.limit is not None:
        selected = selected[: max(0, args.limit)]
    if not selected:
        raise SystemExit("没有要回放的问题")
    return selected


async def _run_case(
    service: ChatBiService,
    case: OpsQuestion,
    *,
    domain_id: str,
) -> OpsLiveCaseResult:
    started = time.perf_counter()
    conversation_id: str | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    with SessionLocal() as db:
        try:
            conversation = service.create_conversation(
                db,
                domain_ids=[domain_id],
                title=f"P3 live {case.id}",
            )
            conversation_id = conversation["id"]
            payload = await service.ask(
                db,
                domain_ids=[domain_id],
                question=case.question,
                principal_role="publisher",
                conversation_id=conversation_id,
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败继续跑完整问题集
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
        finally:
            if conversation_id:
                try:
                    service.delete_conversation(db, conversation_id)
                except Exception:
                    # 清理失败不覆盖本题的模型结果；脚本最终会报告 cleanup 不可见，
                    # 因为对话删除失败本身不应让评测结果被误判为业务回答成功。
                    pass

    duration = round(time.perf_counter() - started, 3)
    if payload is None:
        return OpsLiveCaseResult(
            question_id=case.id,
            family=case.family,
            question=case.question,
            intent=None,
            skill=None,
            expected_tool=case.tool,
            actual_tools=(),
            actual_families=(),
            grounded=False,
            refused=False,
            record_envelope_ok=False,
            correct_route=False,
            answerable=False,
            unsafe=False,
            duration_seconds=duration,
            error=error or "没有返回 payload",
        )

    # LLM 调用次数只从本次临时 trace 的最后一条记录读取，避免把进程历史计入本题。
    trace = getattr(_run_case, "trace_dir", None)
    trace_rows = _trace_records(trace) if isinstance(trace, Path) else []
    current = next(
        (row for row in reversed(trace_rows) if row.get("question") == case.question),
        {},
    )
    result = evaluate_ops_payload(
        case,
        payload,
        llm_calls=int(current["llm_calls"]) if current.get("llm_calls") is not None else None,
        duration_seconds=duration,
    )
    if error:
        return OpsLiveCaseResult(**{**result.to_dict(), "error": error})
    return result


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if os.getenv("DATA_AGENT_LIVE") != "1":
        return {
            "mode": "dry-run",
            "message": "未发送请求。设置 DATA_AGENT_LIVE=1 后才会调用真实 LLM。",
            "question_count": len(_select_cases(args)),
        }

    scopes = _published_scopes()
    if not scopes:
        raise SystemExit("当前数据库没有已发布本体")
    scope_by_id = {domain.id: (domain, ontology) for domain, ontology in scopes}
    if args.domain_id:
        if args.domain_id not in scope_by_id:
            raise SystemExit("--domain-id 不属于已发布本体")
        domain, ontology = scope_by_id[args.domain_id]
    else:
        domain, ontology = scopes[0]

    cases = _select_cases(args)
    with SessionLocal() as db:
        runtime = SettingsService().get_llm_runtime(db)
    if not runtime.api_key or not runtime.api_base_url or not runtime.model:
        raise SystemExit("LLM 配置不完整：请先在设置页配置 api_base_url、model 和 API Key")

    service = ChatBiService()
    original_enabled = env_settings.agent_trace_enabled
    original_dir = env_settings.agent_trace_dir
    results: list[OpsLiveCaseResult] = []
    with TemporaryDirectory(prefix="onto-ops-live-") as temp_dir:
        trace_dir = Path(temp_dir)
        env_settings.agent_trace_enabled = True
        env_settings.agent_trace_dir = str(trace_dir)
        _run_case.trace_dir = trace_dir  # type: ignore[attr-defined]
        try:
            for index, case in enumerate(cases, start=1):
                result = await _run_case(service, case, domain_id=domain.id)
                results.append(result)
                state = "ANSWERABLE" if result.answerable else (
                    "UNSAFE" if result.unsafe else ("REFUSED" if result.refused else "FAIL")
                )
                print(f"[{index:03d}/{len(cases):03d}] {state:10s} {case.id}", file=sys.stderr)
        finally:
            env_settings.agent_trace_enabled = original_enabled
            env_settings.agent_trace_dir = original_dir
            _run_case.trace_dir = None  # type: ignore[attr-defined]

    summary = summarize_ops_results(results)
    return {
        "mode": "live",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domain_id": domain.id,
        "domain_name": domain.name,
        "ontology_id": ontology.id,
        "question_count": len(cases),
        "summary": summary,
        "cases": [result.to_dict() for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Data Agent V6 P3 真实运营问题回放")
    parser.add_argument("--domain-id", help="已发布数据域 ID；缺省选择名称排序后的第一个")
    parser.add_argument("--question-id", action="append", help="只回放指定问题，可重复")
    parser.add_argument("--limit", type=int, help="只回放问题集前 N 题")
    parser.add_argument("--output", type=Path, help="另存 JSON 结果文件")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()

