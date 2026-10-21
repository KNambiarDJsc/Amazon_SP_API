# Amazon India Seller Eligibility Checker

Self-hostable platform that takes brand names, finds matching Amazon India catalog products,
checks **ASIN-level** listing restrictions for the seller's own account, and exports an
evidence-backed shortlist of products that were verified listable.

## Design rules that shape everything

1. **Status is derived, never observed.** Raw evidence (what Amazon returned, from which
   source) is stored separately from the status computed by a deterministic rules engine
   (`backend/app/domain/classification.py`). No LLM is involved in classification.
2. **Missing data is never permission.** `VERIFIED_LISTABLE` requires an affirmative,
   well-formed answer for the exact ASIN, condition and marketplace asked about. Errors,
   malformed bodies, expired sessions and unknown reason codes all become `NEEDS_MANUAL_CHECK`.
   A test fuzzes 3,000 response shapes to enforce this.
3. **One ASIN says nothing about a brand.** Brand-level summaries are computed conservatively
   from the ASINs actually checked, and never claim a brand is ungated everywhere.
4. **Tenant isolation is structural.** Child rows reference parents through composite
   `(id, seller_account_id)` foreign keys, so the database rejects cross-seller references.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Schema, API contracts, rules engine, mocked Amazon responses | done |
| 2 | Ingestion, queue, orchestration, checkpointing, Excel reports | in progress |
| 3 | Seller authorisation, live SP-API | planned |
| 4 | Policy-compliant browser verification, manual review | planned |
| 5 | Dashboard, observability, hardening, Docker Compose | planned |

## Development

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                      # offline, SQLite, no credentials needed

# Against real PostgreSQL (also checks that the Alembic migration matches the models):
TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/eligibility_test .venv/bin/pytest

cd backend && DATABASE_URL=postgresql+psycopg://... ../.venv/bin/alembic upgrade head
```
