"""exports

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 21:17:47.876157
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('exports',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'READY', 'FAILED', name='export_status', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('checks_count', sa.Integer(), nullable=True),
    sa.Column('file_name', sa.String(length=80), nullable=True),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.CheckConstraint("status IN ('QUEUED', 'RUNNING', 'READY', 'FAILED')", name=op.f('ck_exports_export_status')),
    sa.ForeignKeyConstraint(['job_id', 'seller_account_id'], ['jobs.id', 'jobs.seller_account_id'], name='fk_exports_job_seller'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exports'))
    )
    op.create_index('ix_exports_job_requested', 'exports', ['job_id', 'requested_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_exports_job_requested', table_name='exports')
    op.drop_table('exports')
