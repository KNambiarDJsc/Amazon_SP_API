"""browser acknowledgement and session store

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21 20:25:58.049319
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('browser_sessions',
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('state_encrypted', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_browser_sessions_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('seller_account_id', name=op.f('pk_browser_sessions'))
    )
    op.add_column('seller_accounts', sa.Column('browser_ack_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('seller_accounts', sa.Column('browser_ack_version', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('seller_accounts', 'browser_ack_version')
    op.drop_column('seller_accounts', 'browser_ack_at')
    op.drop_table('browser_sessions')
