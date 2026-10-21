"""windowed dispatch: QUEUED brand state, queued_at, seller/status index

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

OLD = "('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED')"
NEW = "('PENDING', 'QUEUED', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED')"
NAME = "ck_job_brands_brand_status"


def upgrade() -> None:
    op.add_column("job_brands", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f(NAME), "job_brands", type_="check")
    op.create_check_constraint(op.f(NAME), "job_brands", f"status IN {NEW}")
    op.create_index("ix_job_brands_seller_status", "job_brands", ["seller_account_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_job_brands_seller_status", table_name="job_brands")
    op.execute("UPDATE job_brands SET status = 'PENDING' WHERE status = 'QUEUED'")
    op.drop_constraint(op.f(NAME), "job_brands", type_="check")
    op.create_check_constraint(op.f(NAME), "job_brands", f"status IN {OLD}")
    op.drop_column("job_brands", "queued_at")
