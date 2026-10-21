"""seller credentials and oauth state

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 20:12:18.121888
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('oauth_states',
    sa.Column('state', sa.String(length=64), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_oauth_states_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('state', name=op.f('pk_oauth_states'))
    )
    op.create_index(op.f('ix_oauth_states_seller_account_id'), 'oauth_states', ['seller_account_id'], unique=False)
    op.create_table('seller_credentials',
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('authorization_type', sa.String(length=20), nullable=False),
    sa.Column('refresh_token_encrypted', sa.LargeBinary(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_error', sa.String(length=300), nullable=True),
    sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_seller_credentials_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('seller_account_id', name=op.f('pk_seller_credentials'))
    )


def downgrade() -> None:
    op.drop_table('seller_credentials')
    op.drop_index(op.f('ix_oauth_states_seller_account_id'), table_name='oauth_states')
    op.drop_table('oauth_states')
