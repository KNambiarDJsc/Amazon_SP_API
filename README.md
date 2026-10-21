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
| 2 | Ingestion, queue, orchestration, checkpointing, Excel reports, HTTP API | done |
| 3 | Seller authorisation, live SP-API | planned |
| 4 | Policy-compliant browser verification, manual review | planned |
| 5 | Dashboard, observability, hardening, Docker Compose | planned |

## Quickstart (offline demo, no Amazon credentials)

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ".[dev]"

# Needs PostgreSQL and Redis. Copy .env.example to .env and adjust.
cd backend && ../.venv/bin/alembic upgrade head && cd ..

# 1. One-shot demo, in-process, writes an Excel report (no Redis or worker needed):
.venv/bin/python -m app.cli demo --out demo-report.xlsx

# 2. The real thing: API + worker + queue
.venv/bin/python -m app.cli create-seller --name "Acme Traders"      # prints an API key once
.venv/bin/uvicorn app.api.main:app --port 8000 &
.venv/bin/dramatiq app.workers.main --processes 2 --threads 4 &

curl -H "Authorization: Bearer $KEY" -F file=@brands.xlsx localhost:8000/api/v1/jobs/upload
curl -H "Authorization: Bearer $KEY" localhost:8000/api/v1/jobs/$JOB
curl -H "Authorization: Bearer $KEY" -o report.xlsx localhost:8000/api/v1/jobs/$JOB/export.xlsx
```

Interactive API docs: `http://localhost:8000/docs`. In `AMAZON_MODE=mock` every job and export is
labelled demo data; `live` arrives with Phase 3.

### API

| | |
|---|---|
| `POST /api/v1/jobs` | Create from `{"brands": [...], "settings": {...}}` |
| `POST /api/v1/jobs/upload` | Create from `.xlsx`/`.csv` (uses a `Brand`/`Brand Name` column, else column A) |
| `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}` | Jobs with live progress |
| `POST /api/v1/jobs/{id}/pause\|resume\|cancel` | Lifecycle |
| `GET /api/v1/jobs/{id}/brands` | Brand results; filter `status`, `brand_status`, `q` |
| `GET /api/v1/jobs/{id}/brands/{brand}/candidates` | Every catalog candidate and why it was kept or rejected |
| `GET /api/v1/jobs/{id}/checks` | ASIN checks; filter `status`, `state`, `brand_id`, `q`; paginated |
| `GET /api/v1/jobs/{id}/checks/{check}` | One check with its full evidence trail |
| `GET /api/v1/jobs/{id}/export.xlsx` | Four-sheet Excel report |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design, failure handling and guarantees.

## Development

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                      # offline, SQLite, no credentials needed

# Full suite: real PostgreSQL (migration drift, concurrency races) and real Redis
# (rate limiter, Dramatiq workers):
TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/eligibility_test \
REDIS_TEST_URL=redis://localhost:6379/1 .venv/bin/pytest
```
