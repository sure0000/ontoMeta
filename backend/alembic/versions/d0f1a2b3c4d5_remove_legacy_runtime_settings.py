"""Remove execution paths replaced by the Flink SQL runtime.

The historical migrations remain replayable, but current databases no longer
need the Cube settings table, old runner/docker columns, or the obsolete
preflight sentinel timeout on ``airflow_settings``.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "cffbc3557094"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AIRFLOW_COLUMNS = (
    "jobs_dir",
    "sync_channel",
    "sync_runner_endpoint",
    "sync_runner_token",
    "sync_tool",
    "docker_network",
    "drivers_dir",
    "sync_tool_images",
    "preflight_sentinel_timeout",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("airflow_settings")}
    with op.batch_alter_table("airflow_settings") as batch:
        for name in _AIRFLOW_COLUMNS:
            if name in columns:
                batch.drop_column(name)
    if "cube_settings" in set(inspector.get_table_names()):
        op.drop_table("cube_settings")


def downgrade() -> None:
    # The removed paths are intentionally not restored. Historical migrations
    # remain available for clean installs; downgrading this cut-over is unsupported.
    raise RuntimeError("The legacy runtime settings migration cannot be downgraded")
