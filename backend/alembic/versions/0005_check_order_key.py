"""index-friendly ordering key for ASIN checks

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_CONDITIONS = [
    "new_new", "new_open_box", "new_oem", "refurbished_refurbished", "used_like_new",
    "used_very_good", "used_good", "used_acceptable", "collectible_like_new",
    "collectible_very_good", "collectible_good", "collectible_acceptable", "club_club",
]


def upgrade() -> None:
    op.add_column("eligibility_checks", sa.Column("seq", sa.Integer(), nullable=False, server_default="0"))
    cases = " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(_CONDITIONS))
    op.execute(
        """
        UPDATE eligibility_checks SET seq =
          (SELECT b.position FROM job_brands b WHERE b.id = eligibility_checks.job_brand_id) * 100000
          + (SELECT COALESCE(k.rank, 99) FROM catalog_candidates k
             WHERE k.id = eligibility_checks.candidate_id) * 100
          + CASE condition """ + cases + """ ELSE 99 END
        """
    )
    op.alter_column("eligibility_checks", "seq", server_default=None)
    op.drop_index("ix_checks_job_status", table_name="eligibility_checks")
    op.create_index("ix_checks_job_seq", "eligibility_checks", ["job_id", "seq"])
    op.create_index("ix_checks_job_status_seq", "eligibility_checks", ["job_id", "status", "seq"])


def downgrade() -> None:
    op.drop_index("ix_checks_job_status_seq", table_name="eligibility_checks")
    op.drop_index("ix_checks_job_seq", table_name="eligibility_checks")
    op.create_index("ix_checks_job_status", "eligibility_checks", ["job_id", "status"])
    op.drop_column("eligibility_checks", "seq")
