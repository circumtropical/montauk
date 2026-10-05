"""semantic index: chunk vectors + per-person index state

Revision ID: 7a3e9c1d5b20
Revises: de20cbace194
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a3e9c1d5b20'
down_revision: Union[str, Sequence[str], None] = 'de20cbace194'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('semantic_chunks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('person_id', sa.Uuid(), nullable=False),
    sa.Column('chunk_id', sa.String(length=64), nullable=False),
    sa.Column('chunk_type', sa.String(length=16), nullable=False),
    sa.Column('local_id', sa.String(length=32), nullable=True),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('chunk_total', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('embedding', sa.LargeBinary(), nullable=False),
    sa.CheckConstraint("chunk_type IN ('summary', 'fact', 'interaction')", name=op.f('ck_semantic_chunks_chunk_type_valid')),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], name=op.f('fk_semantic_chunks_person_id_people'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_semantic_chunks_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_semantic_chunks')),
    sa.UniqueConstraint('person_id', 'chunk_id', name=op.f('uq_semantic_chunks_person_id_chunk_id'))
    )
    op.create_index('ix_semantic_chunks_workspace_person', 'semantic_chunks', ['workspace_id', 'person_id'], unique=False)
    op.create_table('semantic_person_state',
    sa.Column('person_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('model_name', sa.String(length=120), nullable=False),
    sa.Column('dimension', sa.Integer(), nullable=False),
    sa.Column('chunk_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('chunk_count', sa.Integer(), nullable=False),
    sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], name=op.f('fk_semantic_person_state_person_id_people'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_semantic_person_state_workspace_id_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('person_id', name=op.f('pk_semantic_person_state'))
    )
    op.create_index(op.f('ix_semantic_person_state_workspace_id'), 'semantic_person_state', ['workspace_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_semantic_person_state_workspace_id'), table_name='semantic_person_state')
    op.drop_table('semantic_person_state')
    op.drop_index('ix_semantic_chunks_workspace_person', table_name='semantic_chunks')
    op.drop_table('semantic_chunks')
