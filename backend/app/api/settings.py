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
        id=service.id,
        name=service.name,
        provider=service.provider,
        api_base_url=service.api_base_url,
        model=service.model,
        is_default=service.is_default,
        enabled=service.enabled,
        api_key_set=bool(service.api_key),
        api_key_hint=mask_secret(service.api_key),
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _llm_service_detail(service) -> LlmServiceConfigDetail:
    # 不再回显明文 API Key；列表/详情仅提供 hint，编辑时留空表示保持不变
    return LlmServiceConfigDetail(
        **_llm_service_out(service).model_dump(),
        api_key=None,
    )


def _datahub_settings_out(row) -> DatahubSettingsOut:
    return DatahubSettingsOut(
        gms_url=row.gms_url,
        frontend_url=row.frontend_url,
        token_set=bool(row.token),
        token_hint=mask_secret(row.token),
        fabric=row.fabric or "PROD",
        updated_at=row.updated_at,
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
        endpoint=row.endpoint,
        username=row.username,
        password_set=bool(row.password),
        password_hint=mask_secret(row.password),
        token_set=bool(row.token),
        api_version=row.api_version,
        enabled=row.enabled,
        # 投递目录空时读取侧会退回默认（见 settings_service），可用与否只看启用 + endpoint。
        available=bool(row.enabled and row.endpoint),
        dags_dir=row.dags_dir,
        jobs_dir=row.jobs_dir,
        sync_channel=row.sync_channel,
        sync_runner_endpoint=row.sync_runner_endpoint,
        sync_runner_token_set=bool(row.sync_runner_token),
        docker_network=row.docker_network,
        drivers_dir=row.drivers_dir,
        sync_tool_images=row.sync_tool_images,
        max_tasks_per_dag=row.max_tasks_per_dag,
        max_active_tasks_per_dag=row.max_active_tasks_per_dag,
        dag_parse_timeout=row.dag_parse_timeout,
        preflight_sentinel_timeout=row.preflight_sentinel_timeout,
        staging_swap=row.staging_swap,
        updated_at=row.updated_at,
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
    from app.connectors.airflow import AirflowClient, AirflowError

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
        detail = str(exc)
        if "401" in detail or "403" in detail:
            detail += (
                f"（{cfg.endpoint}/health 可通，说明网络没问题，是 REST API 鉴权不通。"
                "Airflow 2.x 默认 api.auth_backends 只有 session，仅供 Web UI 用；"
                "请在 Airflow 侧设 AIRFLOW__API__AUTH_BACKENDS="
                "airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session "
                "后重启 webserver，或改用 token 鉴权）"
            )
        raise HTTPException(status_code=400, detail=detail) from exc
    finally:
        client.close()
    return {"ok": True, "health": health}


def _cube_settings_out(row) -> CubeSettingsOut:
    return CubeSettingsOut(
        api_url=row.api_url,
        secret_set=bool(row.api_secret),
        secret_hint=mask_secret(row.api_secret),
        preagg_refresh=row.preagg_refresh,
        tenant_dimension=row.tenant_dimension,
        timeout_seconds=row.timeout_seconds,
        updated_at=row.updated_at,
    )


@router.get("/settings/cube", response_model=CubeSettingsOut)
def get_cube_settings(db: Session = Depends(get_db)):
    return _cube_settings_out(settings_service.get_cube_settings(db))


@router.put("/settings/cube", response_model=CubeSettingsOut)
def update_cube_settings(data: CubeSettingsUpdate, db: Session = Depends(get_db)):
    row = settings_service.update_cube_settings(db, data.model_dump())
    return _cube_settings_out(row)


