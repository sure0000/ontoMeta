"""airflow 编排配置全部入库（设置页唯一入口，不再需要配置文件）

原来这些只能靠环境变量/.env 配：投递目录、执行通道与 runner 地址、docker 通道的网络与
驱动目录、镜像覆盖、分批上限、并发闸门、等解析超时、staging 开关。改成设置行的列后，
环境变量只在**建行时播一次种**（见 settings_service.ensure_defaults），此后以库为准——
避免「改了 .env 却不生效」这种两个事实源打架。

已有部署升级：新列带 server_default，随后 _carry_over_env_values() 把**升级前真正生效的**
环境变量值一次性写进那一行——不搬的话 runner 地址、docker 网络、驱动目录会静默回到出厂值，
物化随即失败，而现场看起来像「什么都没改却坏了」。

Revision ID: b5c6d7e8f9a0
Revises: d9e0f1a2b3c4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None

# (列名, 类型, server_default 的 SQL 字面量)。默认值与 config.Settings 的字段默认一致，
# 保证「什么都不配」时行为与本迁移之前完全相同。
_COLUMNS = [
    ("dags_dir", sa.String(512), "''"),
    ("jobs_dir", sa.String(512), "''"),
    ("sync_channel", sa.String(16), "'runner'"),
    ("sync_runner_endpoint", sa.String(512), "''"),

    ("docker_network", sa.String(128), "'bridge'"),
    ("drivers_dir", sa.String(512), "''"),
    ("sync_tool_images", sa.String(1024), "''"),
    ("max_tasks_per_dag", sa.Integer(), "50"),
    ("max_active_tasks_per_dag", sa.Integer(), "16"),
    ("dag_parse_timeout", sa.Float(), "60.0"),
    ("preflight_sentinel_timeout", sa.Float(), "20.0"),
    # SQLite 没有布尔类型，1/0 是它与 Postgres 都认的写法。
    ("staging_swap", sa.Boolean(), "1"),
]


def upgrade() -> None:
    for name, type_, default_sql in _COLUMNS:
        op.add_column(
            "airflow_settings",
            sa.Column(name, type_, nullable=False, server_default=sa.text(default_sql)),
        )
    # 调 runner 的 token 单独建：**可空**（runner 未设 token 时就是没有），
    # 与其余「总有一个出厂值」的配置项不是一回事。
    op.add_column(
        "airflow_settings", sa.Column("sync_runner_token", sa.String(512), nullable=True)
    )
    _carry_over_env_values()


def _carry_over_env_values() -> None:
    """把**原来的配置来源**（环境变量/.env）一次性搬进已有的那一行。

    新列只有 server_default，已有行拿到的是「出厂值」，而升级前真正生效的是 .env 里的值：
    不搬的话，升级瞬间 runner 地址、docker 网络、驱动目录会静默回到出厂值，物化随即失败，
    而现场看起来像「什么都没改却坏了」。这是一次性的迁移动作，**只对已存在的行做**——
    新库建行时由 settings_service 播种，不走这里。
    """
    from pathlib import Path

    from app.config import settings as env  # 迁移期读一次旧来源，运行期不再读

    # 目录默认值**内联**而非从 settings_service 导入：迁移是历史快照，不能依赖
    # 会演进的活代码——_default_jobs_dir 后来随 seatunnel 组件一起下线，那次删除
    # 曾让本迁移 ImportError、整套 alembic upgrade head 直接炸掉。
    _repo_root = Path(__file__).resolve().parents[3]
    dags_dir = getattr(env, "airflow_dags_dir", "") or str(
        _repo_root / "docker" / "orchestration" / "dags"
    )
    jobs_dir = getattr(env, "airflow_jobs_dir", "") or str(
        _repo_root / "docker" / "orchestration" / "seatunnel" / "jobs"
    )

    op.execute(
        sa.text(
            """
            UPDATE airflow_settings SET
                dags_dir = :dags_dir,
                jobs_dir = :jobs_dir,
                sync_channel = :sync_channel,
                sync_runner_endpoint = :sync_runner_endpoint,
                docker_network = :docker_network,
                drivers_dir = :drivers_dir,
                sync_tool_images = :sync_tool_images,
                max_tasks_per_dag = :max_tasks_per_dag,
                max_active_tasks_per_dag = :max_active_tasks_per_dag,
                dag_parse_timeout = :dag_parse_timeout,
                preflight_sentinel_timeout = :preflight_sentinel_timeout,
                staging_swap = :staging_swap
            """
        ).bindparams(
            dags_dir=dags_dir,
            jobs_dir=jobs_dir,
            sync_channel=getattr(env, "sync_channel", "runner") or "runner",
            sync_runner_endpoint=getattr(env, "sync_runner_endpoint", "") or "",
            docker_network=getattr(env, "airflow_docker_network", "bridge") or "bridge",
            drivers_dir=getattr(env, "airflow_sync_drivers_dir", "") or "",
            sync_tool_images=getattr(env, "sync_tool_images", "") or "",
            max_tasks_per_dag=env.ontometa_max_tasks_per_dag,
            max_active_tasks_per_dag=env.ontometa_max_active_tasks_per_dag,
            dag_parse_timeout=env.ontometa_dag_parse_timeout,
            # This setting was removed from the runtime before the migration was
            # retired; keep historical upgrades replayable with its old default.
            preflight_sentinel_timeout=getattr(
                env, "ontometa_preflight_sentinel_timeout", 20.0
            ),
            staging_swap=bool(env.ontometa_staging_swap),
        )
    )


def downgrade() -> None:
    op.drop_column("airflow_settings", "sync_runner_token")
    for name, _type, _default in reversed(_COLUMNS):
        op.drop_column("airflow_settings", name)
