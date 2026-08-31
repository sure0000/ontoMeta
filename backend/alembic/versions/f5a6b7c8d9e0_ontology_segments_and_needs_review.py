"""ontology_segments_and_needs_review

Revision ID: f5a6b7c8d9e0
Revises: f4a5b6c7d8e9
Create Date: 2026-08-31 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f5a6b7c8d9e0'
down_revision = 'b7c1d4e2f8a3'
branch_labels = None
depends_on = None


def upgrade():
    # 新增 ontology_segments 表
    op.create_table(
        'ontology_segments',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('ontology_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        # 锚点：度数最高的 K 个成员的 source_ref，重算时的对齐键
        sa.Column('anchor_refs', sa.Text(), nullable=True),  # JSON array of source_refs
        sa.Column('member_count', sa.Integer(), nullable=False),
        # ProvenanceMixin 同款字段
        sa.Column('origin', sa.String(length=30), nullable=False, server_default='machine'),
        sa.Column('machine_baseline', sa.Text(), nullable=True),
        sa.Column('overridden_fields', sa.Text(), nullable=True),
        sa.Column('conflict_json', sa.Text(), nullable=True),
        sa.Column('user_created', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('deleted_by_user', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('upstream_removed', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('last_generation_id', sa.String(length=36), nullable=True),
        sa.Column('needs_review', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['ontology_id'], ['ontologies.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ontology_segments_ontology_id'), 'ontology_segments', ['ontology_id'], unique=False)
    op.create_index(op.f('ix_ontology_segments_needs_review'), 'ontology_segments', ['needs_review'], unique=False)

    # object_types 增加 segment_id 和 is_hub（使用 batch 模式处理 SQLite 约束）
    with op.batch_alter_table('object_types', schema=None) as batch_op:
        batch_op.add_column(sa.Column('segment_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('is_hub', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.create_index(batch_op.f('ix_object_types_segment_id'), ['segment_id'], unique=False)
        batch_op.create_foreign_key('fk_object_types_segment_id', 'ontology_segments', ['segment_id'], ['id'])

    # relation_types 增加 needs_review
    with op.batch_alter_table('relation_types', schema=None) as batch_op:
        batch_op.add_column(sa.Column('needs_review', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.create_index(batch_op.f('ix_relation_types_needs_review'), ['needs_review'], unique=False)


def downgrade():
    with op.batch_alter_table('relation_types', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_relation_types_needs_review'))
        batch_op.drop_column('needs_review')

    with op.batch_alter_table('object_types', schema=None) as batch_op:
        batch_op.drop_constraint('fk_object_types_segment_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_object_types_segment_id'))
        batch_op.drop_column('is_hub')
        batch_op.drop_column('segment_id')

    op.drop_index(op.f('ix_ontology_segments_needs_review'), table_name='ontology_segments')
    op.drop_index(op.f('ix_ontology_segments_ontology_id'), table_name='ontology_segments')
    op.drop_table('ontology_segments')
