"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-21 19:40:55.050308
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('seller_accounts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('marketplace_id', sa.String(length=20), nullable=False),
    sa.Column('amazon_seller_id', sa.String(length=40), nullable=True),
    sa.Column('api_key_hash', sa.String(length=64), nullable=False),
    sa.Column('api_key_prefix', sa.String(length=12), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_seller_accounts')),
    sa.UniqueConstraint('api_key_hash', name=op.f('uq_seller_accounts_api_key_hash'))
    )
    op.create_table('jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=True),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'PAUSED', 'CANCELLED', 'COMPLETED', name='job_status', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('data_source', sa.Enum('LIVE', 'MOCK', name='data_source', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('pause_reason', sa.String(length=200), nullable=True),
    sa.Column('source_filename', sa.String(length=300), nullable=True),
    sa.Column('settings', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('ingestion_report', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("data_source IN ('LIVE', 'MOCK')", name=op.f('ck_jobs_data_source')),
    sa.CheckConstraint("status IN ('QUEUED', 'RUNNING', 'PAUSED', 'CANCELLED', 'COMPLETED')", name=op.f('ck_jobs_job_status')),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_jobs_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('id', 'seller_account_id', name='uq_jobs_id_seller')
    )
    op.create_index(op.f('ix_jobs_seller_account_id'), 'jobs', ['seller_account_id'], unique=False)
    op.create_index(op.f('ix_jobs_status'), 'jobs', ['status'], unique=False)
    op.create_table('audit_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('job_brand_id', sa.Uuid(), nullable=True),
    sa.Column('check_id', sa.Uuid(), nullable=True),
    sa.Column('event_type', sa.String(length=60), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['job_id', 'seller_account_id'], ['jobs.id', 'jobs.seller_account_id'], name='fk_audit_job_seller'),
    sa.ForeignKeyConstraint(['seller_account_id'], ['seller_accounts.id'], name=op.f('fk_audit_events_seller_account_id_seller_accounts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index('ix_audit_job_created', 'audit_events', ['job_id', 'created_at'], unique=False)
    op.create_table('job_brands',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('original_name', sa.String(length=500), nullable=False),
    sa.Column('display_name', sa.String(length=300), nullable=False),
    sa.Column('dedupe_key', sa.String(length=300), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED', name='brand_status', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_owner', sa.String(length=100), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('checkpoint', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('brand_status', sa.Enum('VERIFIED_LISTABLE', 'BRAND_APPROVAL_REQUIRED', 'OTHER_APPROVAL_REQUIRED', 'NOT_ELIGIBLE', 'NO_PRODUCT_FOUND', 'NEEDS_MANUAL_CHECK', 'MIXED_RESULTS', name='brand_eligibility_status', native_enum=False, create_constraint=False, length=40), nullable=True),
    sa.Column('brand_summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("brand_status IN ('VERIFIED_LISTABLE', 'BRAND_APPROVAL_REQUIRED', 'OTHER_APPROVAL_REQUIRED', 'NOT_ELIGIBLE', 'NO_PRODUCT_FOUND', 'NEEDS_MANUAL_CHECK', 'MIXED_RESULTS')", name=op.f('ck_job_brands_brand_eligibility_status')),
    sa.CheckConstraint("status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED')", name=op.f('ck_job_brands_brand_status')),
    sa.ForeignKeyConstraint(['job_id', 'seller_account_id'], ['jobs.id', 'jobs.seller_account_id'], name='fk_job_brands_job_seller'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_brands')),
    sa.UniqueConstraint('id', 'seller_account_id', name='uq_job_brands_id_seller'),
    sa.UniqueConstraint('job_id', 'dedupe_key', name='uq_job_brands_job_dedupe')
    )
    op.create_index('ix_job_brands_job_status', 'job_brands', ['job_id', 'status'], unique=False)
    op.create_index('ix_job_brands_status_lease', 'job_brands', ['status', 'lease_expires_at'], unique=False)
    op.create_table('catalog_candidates',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_brand_id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('asin', sa.String(length=20), nullable=False),
    sa.Column('marketplace_id', sa.String(length=20), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('brand_attribute', sa.String(length=300), nullable=True),
    sa.Column('category_name', sa.String(length=300), nullable=True),
    sa.Column('category_id', sa.String(length=50), nullable=True),
    sa.Column('product_type', sa.String(length=100), nullable=True),
    sa.Column('brand_match', sa.Enum('EXACT', 'NORMALIZED', 'MISMATCH', 'MISSING', name='brand_match', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('rejection_reason', sa.String(length=200), nullable=True),
    sa.Column('rank', sa.Integer(), nullable=True),
    sa.Column('selected', sa.Boolean(), nullable=False),
    sa.Column('raw', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('discovered_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("brand_match IN ('EXACT', 'NORMALIZED', 'MISMATCH', 'MISSING')", name=op.f('ck_catalog_candidates_brand_match')),
    sa.ForeignKeyConstraint(['job_brand_id', 'seller_account_id'], ['job_brands.id', 'job_brands.seller_account_id'], name='fk_candidates_brand_seller'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_catalog_candidates')),
    sa.UniqueConstraint('id', 'seller_account_id', name='uq_candidates_id_seller'),
    sa.UniqueConstraint('job_brand_id', 'asin', name='uq_candidates_brand_asin')
    )
    op.create_table('eligibility_checks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('job_brand_id', sa.Uuid(), nullable=False),
    sa.Column('candidate_id', sa.Uuid(), nullable=False),
    sa.Column('asin', sa.String(length=20), nullable=False),
    sa.Column('marketplace_id', sa.String(length=20), nullable=False),
    sa.Column('condition', sa.String(length=40), nullable=False),
    sa.Column('verification_context', sa.String(length=100), nullable=False),
    sa.Column('check_key', sa.String(length=64), nullable=False),
    sa.Column('state', sa.Enum('PENDING', 'DONE', name='check_state', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('status', sa.Enum('VERIFIED_LISTABLE', 'BRAND_APPROVAL_REQUIRED', 'OTHER_APPROVAL_REQUIRED', 'NOT_ELIGIBLE', 'NO_PRODUCT_FOUND', 'NEEDS_MANUAL_CHECK', 'MIXED_RESULTS', name='check_eligibility_status', native_enum=False, create_constraint=False, length=40), nullable=True),
    sa.Column('rule_id', sa.String(length=80), nullable=True),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('rules_version', sa.String(length=20), nullable=True),
    sa.Column('reason_codes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('messages', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('approval_links', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('verification_methods', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(state = 'DONE') = (status IS NOT NULL)", name=op.f('ck_eligibility_checks_done_iff_status')),
    sa.CheckConstraint("state IN ('PENDING', 'DONE')", name=op.f('ck_eligibility_checks_check_state')),
    sa.CheckConstraint("status IN ('VERIFIED_LISTABLE', 'BRAND_APPROVAL_REQUIRED', 'OTHER_APPROVAL_REQUIRED', 'NOT_ELIGIBLE', 'NO_PRODUCT_FOUND', 'NEEDS_MANUAL_CHECK', 'MIXED_RESULTS')", name=op.f('ck_eligibility_checks_check_eligibility_status')),
    sa.ForeignKeyConstraint(['candidate_id', 'seller_account_id'], ['catalog_candidates.id', 'catalog_candidates.seller_account_id'], name='fk_checks_candidate_seller'),
    sa.ForeignKeyConstraint(['job_brand_id', 'seller_account_id'], ['job_brands.id', 'job_brands.seller_account_id'], name='fk_checks_brand_seller'),
    sa.ForeignKeyConstraint(['job_id', 'seller_account_id'], ['jobs.id', 'jobs.seller_account_id'], name='fk_checks_job_seller'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eligibility_checks')),
    sa.UniqueConstraint('id', 'seller_account_id', name='uq_checks_id_seller'),
    sa.UniqueConstraint('job_id', 'check_key', name='uq_checks_job_key')
    )
    op.create_index('ix_checks_job_brand', 'eligibility_checks', ['job_brand_id'], unique=False)
    op.create_index('ix_checks_job_status', 'eligibility_checks', ['job_id', 'status'], unique=False)
    op.create_table('check_observations',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('check_id', sa.Uuid(), nullable=False),
    sa.Column('seller_account_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.Enum('SP_API_RESTRICTIONS', 'BROWSER', 'MANUAL', name='obs_source', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('outcome', sa.Enum('OK', 'AUTH_ERROR', 'RATE_LIMITED', 'HTTP_ERROR', 'MALFORMED_RESPONSE', 'SESSION_EXPIRED', 'PAGE_UNAVAILABLE', 'TIMEOUT', name='obs_outcome', native_enum=False, create_constraint=False, length=40), nullable=False),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('error_detail', sa.Text(), nullable=True),
    sa.Column('context', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('signals', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('raw', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('evidence_ref', sa.String(length=300), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("outcome IN ('OK', 'AUTH_ERROR', 'RATE_LIMITED', 'HTTP_ERROR', 'MALFORMED_RESPONSE', 'SESSION_EXPIRED', 'PAGE_UNAVAILABLE', 'TIMEOUT')", name=op.f('ck_check_observations_obs_outcome')),
    sa.CheckConstraint("source IN ('SP_API_RESTRICTIONS', 'BROWSER', 'MANUAL')", name=op.f('ck_check_observations_obs_source')),
    sa.ForeignKeyConstraint(['check_id', 'seller_account_id'], ['eligibility_checks.id', 'eligibility_checks.seller_account_id'], name='fk_observations_check_seller'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_check_observations'))
    )
    op.create_index('ix_observations_check', 'check_observations', ['check_id', 'observed_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_observations_check', table_name='check_observations')
    op.drop_table('check_observations')
    op.drop_index('ix_checks_job_status', table_name='eligibility_checks')
    op.drop_index('ix_checks_job_brand', table_name='eligibility_checks')
    op.drop_table('eligibility_checks')
    op.drop_table('catalog_candidates')
    op.drop_index('ix_job_brands_status_lease', table_name='job_brands')
    op.drop_index('ix_job_brands_job_status', table_name='job_brands')
    op.drop_table('job_brands')
    op.drop_index('ix_audit_job_created', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index(op.f('ix_jobs_status'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_seller_account_id'), table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('seller_accounts')
