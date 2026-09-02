"""ontology_segments.kind：板块划分改为全覆盖分区

聚类只覆盖得了一小部分对象（erpnext 实测 1035 个里只有 134 个进了业务模块），
其余曾经堆在一个隐式的「未接入」桶里。kind 把它们按处置方式拆开：
business / shared / pending / technical / system（见 services/segment_kinds）。

Revision ID: 8c8a5b82add9
Revises: 37274306e992
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa

revision = "8c8a5b82add9"
down_revision = "37274306e992"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # 存量库可能已被 repair 迁移补过列，这里保持幂等。
    if _has_column("ontology_segments", "kind"):
        return
    op.add_column(
        "ontology_segments",
        sa.Column(
            "kind",
            sa.String(length=30),
            nullable=False,
            server_default="business",
        ),
    )
    op.create_index(
        "ix_ontology_segments_kind", "ontology_segments", ["kind"], unique=False
    )


def downgrade() -> None:
    if not _has_column("ontology_segments", "kind"):
        return
    op.drop_index("ix_ontology_segments_kind", table_name="ontology_segments")
    op.drop_column("ontology_segments", "kind")
