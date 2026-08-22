"""add_modeling_case_tables

Revision ID: 6ac2622d9b62
Revises: bacbc3c392ad
Create Date: 2026-08-21 22:44:35.505983

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6ac2622d9b62'
down_revision: Union[str, Sequence[str], None] = 'bacbc3c392ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - add ModelingCase tables."""
    # ModelingCase 主表
    op.create_table('modeling_cases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('primary_domain_id', sa.String(length=36), nullable=True),
        sa.Column('domain_ids_json', sa.Text(), nullable=True),
        sa.Column('stage', sa.String(length=50), nullable=False),
        sa.Column('current_revision', sa.Integer(), nullable=False),
        sa.Column('owner_subject_id', sa.String(length=36), nullable=True),
        sa.Column('blocked_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('modeling_cases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_modeling_cases_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_cases_owner_subject_id'), ['owner_subject_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_cases_primary_domain_id'), ['primary_domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_cases_stage'), ['stage'], unique=False)

    # ModelingCaseSpec 版本化规格表
    op.create_table('modeling_case_specs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=50), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('based_on_json', sa.Text(), nullable=True),
        sa.Column('validation_report_json', sa.Text(), nullable=True),
        sa.Column('proposed_by', sa.String(length=36), nullable=True),
        sa.Column('confirmed_by', sa.String(length=36), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['modeling_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sqlite_autoincrement=True
    )
    with op.batch_alter_table('modeling_case_specs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_modeling_case_specs_case_id'), ['case_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_case_specs_content_hash'), ['content_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_case_specs_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_case_specs_status'), ['status'], unique=False)

    # ModelingCaseLink 引用表
    op.create_table('modeling_case_links',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=False),
        sa.Column('ref_kind', sa.String(length=50), nullable=False),
        sa.Column('ref_id', sa.String(length=200), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('spec_revision', sa.Integer(), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['modeling_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sqlite_autoincrement=True
    )
    with op.batch_alter_table('modeling_case_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_modeling_case_links_case_id'), ['case_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_case_links_ref_id'), ['ref_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_modeling_case_links_ref_kind'), ['ref_kind'], unique=False)


def downgrade() -> None:
    """Downgrade schema - remove ModelingCase tables."""
    op.drop_table('modeling_case_links')
    op.drop_table('modeling_case_specs')
    op.drop_table('modeling_cases')
