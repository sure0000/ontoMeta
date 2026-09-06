"""P1 修复的回归钉子（承接 test_p0_production_hardening.py）。

  P1-2  取运行记录不再对全表回读 Airflow    app/services/ops_records.py
  P1-3  鉴权热路径不再每请求写库            app/auth.py

P1-1（零行/行数不符不再判绿）在 test_sync_reconciliation.py，
P1-4（ErrorBoundary + 前端用例）与 P1-5（token 检查进 CI）在 frontend/。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ------------------------------------------------- P1-2 取运行记录的对账成本


def test_read_task_run_pushes_limit_into_the_query(db, monkeypatch):
    """``limit`` 必须进查询，不能只在拿到结果后切片。

    ``list_artifacts`` 默认逐条回读 Airflow DagRun 做对账，也就是**一条制品一次远程
    HTTP**。不截断就是「为了回 5 条，先对全表几十条各发一次请求」——审计时库里有 10 条
    orchestrated 制品，AirflowClient 超时 30 秒，Airflow 不通时最坏 300 秒挂在一次问答上。

    这个坑 ``list_artifacts`` 的 docstring 已经写明并在 MCP 侧改过，ops_records 漏了。
    """
    from app.services import ops_records
    from app.services.agent_pipeline import AgentPipelineService

    seen: dict = {}

    def _spy(self, db_, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(AgentPipelineService, "list_artifacts", _spy)

    ops_records.read_task_run(db, {"limit": 5})

    assert seen.get("limit") == 5, f"limit 没进查询：{seen}"


def test_read_task_run_limit_is_clamped_not_trusted(db, monkeypatch):
    """调用方给的 limit 要被夹到上限——否则「对全表对账」换个入口又回来了。"""
    from app.services import ops_records
    from app.services.agent_pipeline import AgentPipelineService

    seen: dict = {}
    monkeypatch.setattr(
        AgentPipelineService,
        "list_artifacts",
        lambda self, db_, **kwargs: (seen.update(kwargs), [])[1],
    )

    ops_records.read_task_run(db, {"limit": 10_000})

    assert 1 <= seen["limit"] <= 20, seen


# ------------------------------------------------- P1-3 鉴权热路径的写库


def _principal(db, **overrides):
    from app.auth import hash_api_key
    from app.models.principal import Principal

    token = overrides.pop("token", "p1-token-for-last-used-test")
    fields = {
        "name": "test-principal",
        "role": "reader",
        "token_hash": hash_api_key(token, None),
        "token_prefix": token[:12],
        "active": True,
        **overrides,
    }
    principal = Principal(**fields)
    db.add(principal)
    db.commit()
    return principal, token


def test_first_use_records_the_timestamp(db):
    from app.auth import _touch_last_used

    principal, _ = _principal(db)
    assert principal.last_used_at is None

    _touch_last_used(db, principal)

    assert principal.last_used_at is not None
    db.delete(principal)
    db.commit()


def test_repeated_use_within_the_window_does_not_write(db):
    """连续请求不再各写一次库。

    改之前鉴权中间件对**每个**带主体令牌的请求都 UPDATE + COMMIT 一次——所有 GET 都
    在热路径上带一次写事务，在 Postgres 上还会给同一行反复制造死元组。而这个字段只
    用来显示「最近使用：X 分钟前」，秒级精度没人看。
    """
    from app.auth import _touch_last_used

    principal, _ = _principal(db)
    _touch_last_used(db, principal)
    first = principal.last_used_at

    for _ in range(20):
        _touch_last_used(db, principal)

    assert principal.last_used_at == first, "窗口内的重复使用不该再写库"
    db.delete(principal)
    db.commit()


def test_use_after_the_window_records_again(db):
    """节流不是不记：过了记录精度就该更新，否则「最近使用」永远停在第一次。"""
    from app.auth import _LAST_USED_RESOLUTION, _touch_last_used

    principal, _ = _principal(db)
    stale = datetime.now(UTC) - _LAST_USED_RESOLUTION - timedelta(seconds=5)
    principal.last_used_at = stale.replace(tzinfo=None)
    db.commit()

    _touch_last_used(db, principal)

    assert principal.last_used_at.replace(tzinfo=None) > stale.replace(tzinfo=None)
    db.delete(principal)
    db.commit()


def test_naive_stored_timestamp_does_not_explode(db):
    """列是不带时区的 DateTime：读回来是 naive，直接与 aware 的 now 相减会 TypeError。

    这条不是假设——写进去的是 aware，读出来的是 naive，两者相减在 Python 里直接抛。
    """
    from app.auth import _touch_last_used

    principal, _ = _principal(db)
    principal.last_used_at = datetime.utcnow()  # noqa: DTZ003 —— 刻意造 naive 值
    db.commit()

    _touch_last_used(db, principal)  # 不抛即通过

    db.delete(principal)
    db.commit()


def test_auth_resolution_still_returns_role_and_id(db):
    """节流不能顺手改坏鉴权本身的返回值。"""
    from app.auth import resolve_principal_token

    principal, token = _principal(db)
    role, principal_id = resolve_principal_token(token)

    assert role == "reader"
    assert principal_id == principal.id

    db.delete(principal)
    db.commit()


def test_inactive_principal_is_rejected(db):
    from app.auth import resolve_principal_token

    principal, token = _principal(db, active=False, token="p1-inactive-token-xyz")
    assert resolve_principal_token(token) == (None, None)

    db.delete(principal)
    db.commit()
