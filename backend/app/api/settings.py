from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import settings_service
from app.database import get_db
from app.schemas import (
    AirflowSettingsOut,
    AirflowSettingsUpdate,
    CubeSettingsOut,
    CubeSettingsUpdate,
    DatahubSettingsOut,
    DatahubSettingsUpdate,
    DraftGenerationSettingsOut,
    DraftGenerationSettingsUpdate,
    LlmConnectionTestRequest,
    LlmConnectionTestResult,
    LlmModelOption,
    LlmServiceConfigCreate,
    LlmServiceConfigDetail,
    SyncRunnerSecretOut,
    SyncRunnerSecretUpdate,
    LlmServiceConfigOut,
    LlmServiceConfigUpdate,
)
from app.services.settings_service import mask_secret

router = APIRouter()

def _llm_service_out(service) -> LlmServiceConfigOut:
    return LlmServiceConfigOut(
        id=service["id"],
        name=service["name"],
        provider=service["provider"],
        api_base_url=service["api_base_url"],
        model=service["model"],
        is_default=service["is_default"],
        enabled=service["enabled"],
        api_key_set=bool(service.get("api_key")),
        api_key_hint=mask_secret(service.get("api_key")),
        created_at=service["created_at"],
        updated_at=service["updated_at"],
    )


def _llm_service_detail(service) -> LlmServiceConfigDetail:
    # 不再回显明文 API Key；列表/详情仅提供 hint，编辑时留空表示保持不变
    return LlmServiceConfigDetail(
        **_llm_service_out(service).model_dump(),
        api_key=None,
    )


def _datahub_settings_out(row) -> DatahubSettingsOut:
    return DatahubSettingsOut(
        gms_url=row.get("gms_url", ""),
        frontend_url=row.get("frontend_url", ""),
        token_set=bool(row.get("token")),
        token_hint=mask_secret(row.get("token")),
        fabric=row.get("fabric") or "PROD",
        updated_at=row.get("updated_at"),
    )

@router.get("/config")
def get_app_config(db: Session = Depends(get_db)):
    datahub = settings_service.get_datahub_runtime(db)
    return {
        "datahub_gms_url": datahub.gms_url,
        "datahub_frontend_url": datahub.frontend_url,
    }


@router.get("/settings/llm-models", response_model=list[LlmModelOption])
def list_llm_models():
    return settings_service.list_llm_models()


@router.get("/settings/llm-services", response_model=list[LlmServiceConfigOut])
def list_llm_services(db: Session = Depends(get_db)):
    return [_llm_service_out(item) for item in settings_service.list_llm_services(db)]


@router.post("/settings/llm-services", response_model=LlmServiceConfigDetail)
def create_llm_service(data: LlmServiceConfigCreate, db: Session = Depends(get_db)):
    service = settings_service.create_llm_service(db, data.model_dump())
    return _llm_service_detail(service)


@router.post("/settings/llm-services/test", response_model=LlmConnectionTestResult)
def test_llm_connection(data: LlmConnectionTestRequest, db: Session = Depends(get_db)):
    return settings_service.test_llm_connection(db, data.model_dump())


@router.get("/settings/llm-services/{service_id}", response_model=LlmServiceConfigDetail)
def get_llm_service(service_id: str, db: Session = Depends(get_db)):
    service = settings_service.get_llm_service(db, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return _llm_service_detail(service)


@router.put("/settings/llm-services/{service_id}", response_model=LlmServiceConfigDetail)
def update_llm_service(
    service_id: str, data: LlmServiceConfigUpdate, db: Session = Depends(get_db)
):
    payload = data.model_dump(exclude_unset=True)
    service = settings_service.update_llm_service(db, service_id, payload)
    if not service:
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return _llm_service_detail(service)


@router.delete("/settings/llm-services/{service_id}")
def delete_llm_service(service_id: str, db: Session = Depends(get_db)):
    if not settings_service.delete_llm_service(db, service_id):
        raise HTTPException(status_code=404, detail="LLM 服务配置不存在")
    return {"id": service_id, "deleted": True}


@router.get("/settings/datahub", response_model=DatahubSettingsOut)
def get_datahub_settings(db: Session = Depends(get_db)):
    return _datahub_settings_out(settings_service.get_datahub_settings(db))


@router.put("/settings/datahub", response_model=DatahubSettingsOut)
def update_datahub_settings(data: DatahubSettingsUpdate, db: Session = Depends(get_db)):
    row = settings_service.update_datahub_settings(db, data.model_dump())
    return _datahub_settings_out(row)


@router.get("/settings/draft-generation", response_model=DraftGenerationSettingsOut)
def get_draft_generation_settings(db: Session = Depends(get_db)):
    return settings_service.get_draft_generation_settings(db)


@router.put("/settings/draft-generation", response_model=DraftGenerationSettingsOut)
def update_draft_generation_settings(
    data: DraftGenerationSettingsUpdate, db: Session = Depends(get_db)
):
    return settings_service.update_draft_generation_settings(db, data.model_dump())


def _airflow_settings_out(row) -> AirflowSettingsOut:
    return AirflowSettingsOut(
        endpoint=row.get("endpoint", ""),
        username=row.get("username"),
        password_set=bool(row.get("password")),
        password_hint=mask_secret(row.get("password")),
        token_set=bool(row.get("token")),
        api_version=row.get("api_version", "v1"),
        enabled=row.get("enabled", False),
        available=bool(row.get("enabled") and row.get("endpoint")),
        dags_dir=row.get("dags_dir") or "",
        jobs_dir=row.get("jobs_dir") or "",
        dag_delivery_method=row.get("dag_delivery_method") or "local",
        git_remote=row.get("git_remote") or "origin",
        git_branch=row.get("git_branch") or "main",
        git_auto_init=bool(row.get("git_auto_init")),
        git_author=row.get("git_author") or "",
        git_email=row.get("git_email") or "",
        sync_channel=row.get("sync_channel") or "runner",
        sync_runner_endpoint=row.get("sync_runner_endpoint") or "",
        sync_runner_token_set=bool(row.get("sync_runner_token")),
        docker_network=row.get("docker_network") or "bridge",
        drivers_dir=row.get("drivers_dir") or "",
        sync_tool_images=row.get("sync_tool_images") or "",
        sync_tool=row.get("sync_tool") or "",
        max_tasks_per_dag=row.get("max_tasks_per_dag") or 50,
        max_active_tasks_per_dag=row.get("max_active_tasks_per_dag") or 16,
        dag_parse_timeout=row.get("dag_parse_timeout") or 60.0,
        preflight_sentinel_timeout=row.get("preflight_sentinel_timeout") or 20.0,
        staging_swap=row.get("staging_swap") if row.get("staging_swap") is not None else True,
        updated_at=row.get("updated_at"),
    )


@router.get("/settings/airflow", response_model=AirflowSettingsOut)
def get_airflow_settings(db: Session = Depends(get_db)):
    return _airflow_settings_out(settings_service.get_airflow_settings(db))


@router.put("/settings/airflow", response_model=AirflowSettingsOut)
def update_airflow_settings(data: AirflowSettingsUpdate, db: Session = Depends(get_db)):
    return _airflow_settings_out(
        settings_service.update_airflow_settings(db, data.model_dump())
    )


# ---------- sync-runner 的连接配置（凭据代填，ontoMeta 不留副本） ----------
#
# 为什么是代填而不是存在 ontoMeta：凭据只有一个归属地——值落在 runner 自己的存储里，
# 这样 runner 的 /probe 才有意义，DAG 产物里也才只有别名。设置页在这里只是个输入框，
# 请求穿过去就没了，**不落库、不缓存、不回读明文**。


def _runner_client(db: Session):
    from app.connectors.sync_runner import SyncRunnerClient

    airflow = settings_service.get_airflow_runtime(db)
    if not airflow.sync_runner_endpoint:
        raise HTTPException(status_code=400, detail="未配置 sync-runner 地址")
    return SyncRunnerClient(
        airflow.sync_runner_endpoint, token=airflow.sync_runner_token
    )


@router.get("/settings/sync-runner/secrets", response_model=list[SyncRunnerSecretOut])
def list_sync_runner_secrets(db: Session = Depends(get_db)):
    """列出 runner 侧已配的别名。机密键只回「已设置」，明文不出 runner。"""
    from app.connectors.sync_runner import SyncRunnerError

    client = _runner_client(db)
    try:
        return client.list_secrets()
    except SyncRunnerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()


@router.put("/settings/sync-runner/secrets/{alias}")
def put_sync_runner_secret(
    alias: str, data: SyncRunnerSecretUpdate, db: Session = Depends(get_db)
):
    """把一个别名的连接配置写进 runner。**ontoMeta 不保存这些值。**"""
    from app.connectors.sync_runner import SyncRunnerError

    client = _runner_client(db)
    try:
        return client.put_secret(alias, data.values)
    except SyncRunnerError as exc:
        # runner 的 409/403 文本已经说清了原因（别名由环境变量管、runner 没设 token），
        # 原样带出比再包一层更有用。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()


@router.delete("/settings/sync-runner/secrets/{alias}")
def delete_sync_runner_secret(alias: str, db: Session = Depends(get_db)):
    from app.connectors.sync_runner import SyncRunnerError

    client = _runner_client(db)
    try:
        return client.delete_secret(alias)
    except SyncRunnerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()


@router.post("/settings/airflow/test")
def test_airflow_connection(db: Session = Depends(get_db)):
    """连通性测试：先 ``/health`` 探通，再打一次带版本前缀的 REST API 探鉴权。

    只测 ``/health`` 会给假绿灯——它在 Airflow 2.x 默认匿名可读，而下发 DagRun 走的
    ``/api/{version}/*`` 可能因为没开 basic_auth 后端而 401。两步都过才算真的能用。
    """
    from app.connectors.airflow import AirflowClient, AirflowError, explain_ping_failure

    cfg = settings_service.get_airflow_runtime(db)
    client = AirflowClient(
        cfg.endpoint,
        username=cfg.username,
        password=cfg.password,
        token=cfg.token,
        api_version=cfg.api_version,
    )
    try:
        health = client.health()
    except AirflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        client.ping_api()
    except AirflowError as exc:
        # /health 通、REST 不通：按 401/403（鉴权）与 404/405（版本，自动探测应改成哪个）补充下一步。
        detail = explain_ping_failure(client, cfg.api_version, exc)
        raise HTTPException(status_code=400, detail=detail) from exc
    finally:
        client.close()
    return {"ok": True, "health": health}


def _cube_settings_out(row) -> CubeSettingsOut:
    return CubeSettingsOut(
        api_url=row.get("api_url", ""),
        secret_set=bool(row.get("api_secret")),
        secret_hint=mask_secret(row.get("api_secret")),
        preagg_refresh=row.get("preagg_refresh", "1 hour"),
        tenant_dimension=row.get("tenant_dimension"),
        timeout_seconds=int(row.get("timeout_seconds", 30) or 30),
        updated_at=row.get("updated_at"),
    )


@router.get("/settings/cube", response_model=CubeSettingsOut)
def get_cube_settings(db: Session = Depends(get_db)):
    return _cube_settings_out(settings_service.get_cube_settings(db))


@router.put("/settings/cube", response_model=CubeSettingsOut)
def update_cube_settings(data: CubeSettingsUpdate, db: Session = Depends(get_db)):
    row = settings_service.update_cube_settings(db, data.model_dump())
    return _cube_settings_out(row)


