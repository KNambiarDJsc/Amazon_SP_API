"""seller authorization and oauth state

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 10:00:00.000000
"""
import sqlalchemy as sa

from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('seller_authorizations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('selling_partner_id', sa.String(length=40), nullable=False),
    sa.Column('method', sa.Enum('OAUTH', 'SELF_AUTHORIZED', name='authorization_method', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('status', sa.Enum('PENDING_VERIFICATION', 'ACTIVE', 'NEEDS_REAUTHORIZATION', 'REVOKED', name='authorization_status', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('refresh_token_encrypted', sa.Text(), nullable=True),
    sa.Column('store_name', sa.String(length=200), nullable=True),
    sa.Column('authorized_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("method IN ('OAUTH', 'SELF_AUTHORIZED')", name=op.f('ck_seller_authorizations_authorization_method')),
    sa.CheckConstraint("status IN ('PENDING_VERIFICATION', 'ACTIVE', 'NEEDS_REAUTHORIZATION', 'REVOKED')", name=op.f('ck_seller_authorizations_authorization_status')),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_seller_authorizations_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_seller_authorizations')),
    sa.UniqueConstraint('seller_account_id', name=op.f('uq_seller_authorizations_seller_account_id'))
    )
    op.create_table('oauth_states',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('state_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_oauth_states_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oauth_states')),
    sa.UniqueConstraint('state_hash', name=op.f('uq_oauth_states_state_hash'))
    )
    op.create_index(op.f('ix_oauth_states_seller_account_id'), 'oauth_states', ['seller_account_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_oauth_states_seller_account_id'), table_name='oauth_states')
    op.drop_table('oauth_states')
    op.drop_table('seller_authorizations')
