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
| 3 | Seller authorisation, live SP-API | done (verified against a fake of Amazon's documented contract, not the real service) |
| 4 | Human-assisted review workflow; optional, policy-gated browser verification | done (browser adapter verified against an offline fake of the site, not the real one) |
| 5 | Dashboard, observability, hardening, performance, Docker Compose, backups | done (deployment verified end to end on Docker; see [docs/DEPLOY.md](docs/DEPLOY.md)) |

## Quickstart with Docker (offline demo, no Amazon credentials)

```bash
cp .env.example .env            # set POSTGRES_PASSWORD and REDIS_PASSWORD
docker compose up -d --build
docker compose exec api python -m app.cli create-seller --name "Acme Traders"   # prints an API key once
```

Open <http://localhost>, sign in with the key, paste some brands (`eSSL`, `Havells`, `Kangaro`, `Taparia`,
`Oddy`, `Acme Audio`) and watch the job run. The dashboard shows progress, verified / restricted / needs-review
tables, the evidence behind every status, a form to record what Seller Central shows for unresolved ASINs, and the
Excel report. In `AMAZON_MODE=mock` every job and export is labelled demo data. Production setup (TLS, live Amazon
data, backups, monitoring): [docs/DEPLOY.md](docs/DEPLOY.md) and [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Quickstart without Docker

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ".[dev]"

# Needs PostgreSQL and Redis. Copy .env.example to .env and adjust.
cd backend && ../.venv/bin/alembic upgrade head && cd ..

# 1. One-shot demo, in-process, writes an Excel report (no Redis or worker needed):
.venv/bin/python -m app.cli demo --out demo-report.xlsx

# 2. The real thing: API + worker + queue + dashboard
.venv/bin/python -m app.cli create-seller --name "Acme Traders"      # prints an API key once
.venv/bin/uvicorn app.api.main:app --port 8000 &
.venv/bin/dramatiq app.workers.main --processes 2 --threads 4 &
(cd frontend && npm install && npm run dev)                          # http://localhost:5173

curl -H "Authorization: Bearer $KEY" -F file=@brands.xlsx localhost:8000/api/v1/jobs/upload
curl -H "Authorization: Bearer $KEY" localhost:8000/api/v1/jobs/$JOB
curl -H "Authorization: Bearer $KEY" -o report.xlsx localhost:8000/api/v1/jobs/$JOB/export.xlsx
```

Interactive API docs (development only; disabled when `ENVIRONMENT=production`): `http://localhost:8000/docs`.

### API

| | |
|---|---|
| `POST /api/v1/jobs` | Create from `{"brands": [...], "settings": {...}}` |
| `POST /api/v1/jobs/upload` | Create from `.xlsx`/`.csv` (uses a `Brand`/`Brand Name` column, else column A) |
| `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}` | Jobs with live progress |
| `POST /api/v1/jobs/{id}/pause\|resume\|cancel` | Lifecycle |
| `GET /api/v1/jobs/{id}/brands` | Brand results; filter `status`, `brand_status`, `q` |
| `GET /api/v1/jobs/{id}/brands/{brand}/candidates` | Every catalog candidate and why it was kept or rejected |
| `GET /api/v1/jobs/{id}/checks` | ASIN checks; filter `status` (repeatable), `state`, `brand_id`, `q`; paginated |
| `GET /api/v1/jobs/{id}/checks/{check}` | One check with its full evidence trail |
| `GET /api/v1/jobs/{id}/export.xlsx` | Four-sheet Excel report (jobs above `EXPORT_SYNC_MAX_CHECKS` get a 409 pointing at the next row) |
| `POST /api/v1/jobs/{id}/exports`, `GET .../exports/{export}`, `GET .../exports/{export}/download` | Build a large report in the background, poll, download |
| `GET /api/v1/jobs/{id}/review-queue` | What still needs a person, with instructions |
| `POST /api/v1/jobs/{id}/checks/{check}/manual-evidence` | Record what you saw in Seller Central; the status is derived, never supplied |
| `POST /api/v1/jobs/{id}/checks/{check}/recheck` | Ask the API again about one ASIN |
| `POST /api/v1/jobs/{id}/brands/{brand}/retry` | Re-run a dead-lettered or empty brand (`{"rediscover": true}` searches again) |
| `GET/PUT/DELETE /api/v1/amazon/connection` | Connection status; self-authorise with a refresh token; disconnect |
| `POST /api/v1/amazon/connection/verify` | Prove the stored credential still works |
| `POST /api/v1/amazon/authorize`, `GET /api/v1/amazon/callback` | Website authorisation flow |

### Going live (read before your first real run)

1. In the Solution Provider Portal register an SP-API application with the **Product Listing** role
   (needed by both Catalog Items and Listings Restrictions) for the Amazon India marketplace.
2. Generate an encryption key: `python -m app.cli generate-encryption-key` and set, in `.env`:
   `AMAZON_MODE=live`, `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `CREDENTIAL_ENCRYPTION_KEYS`.
3. Connect the seller. **Private app (your own account):** put the refresh token you generated in a file,
   `python -m app.cli connect-seller --seller-account <uuid> --amazon-seller-id <A...> --refresh-token-file token.txt`,
   then delete the file. **Public app:** also set `SP_API_APPLICATION_ID` and `OAUTH_REDIRECT_URI`, then
   `POST /api/v1/amazon/authorize` and send the seller to the returned URL.
4. `GET /api/v1/amazon/connection` should report `connected: true`.
5. **Start small.** Run one job with 2–3 brands you understand, and compare every result with what Seller
   Central shows for the same ASIN. Check `Rationale` and the evidence trail on anything surprising.
   This is the first contact with the real service; the test suite has only exercised Amazon's documented
   contract.
6. Check your account's SP-API usage plan before raising any `RATE_*` setting. Amazon's own pages
   disagree about `searchCatalogItems` (2/s vs 5/s); the default is the conservative one.

Use `SP_API_BASE_URL=https://sandbox.sellingpartnerapi-eu.amazon.com` to exercise authentication against
Amazon's sandbox first; sandbox catalog data is static and will not match real brands.

### Unresolved items and the browser

Anything the API cannot settle lands in the review queue. The recommended way to resolve it is a
person recording what Seller Central shows (see [docs/BROWSER_VERIFICATION.md](docs/BROWSER_VERIFICATION.md)).
An optional browser adapter exists, **off by default** and gated by Amazon's March 2026 Agent Policy
(self-identification, human sign-in, stop on any CAPTCHA, never presses a sell/apply control). Read that
document before enabling it; it also lists what has *not* been verified against the real site.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design, failure handling and guarantees.

## Development and tests

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest                      # offline, SQLite, no credentials needed
.venv/bin/pytest -m "not browser"    # skip the tests that drive a real Chromium

# Full suite: real PostgreSQL (migration drift, concurrency races) and real Redis
# (rate limiter, Dramatiq workers):
TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/eligibility_test \
REDIS_TEST_URL=redis://localhost:6379/1 .venv/bin/pytest

# Dashboard (frontend/README.md): format, types, API contract, unit tests, build
cd frontend && npm ci && npm run verify

# Dashboard end to end against the real API, worker, PostgreSQL and Redis
E2E_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/eligibility_e2e \
E2E_REDIS_URL=redis://localhost:6379/5 npm run e2e

# The same browser suite against the full Docker deployment (Caddy -> API -> worker)
scripts/e2e_compose.sh --down
```

The dashboard's API client is generated from the backend's OpenAPI document (`frontend/openapi.json`); a
backend test fails when that file is stale, so the two cannot drift apart.

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, rules, failure handling, performance, observability |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Docker Compose deployment, TLS, upgrades, monitoring |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Backups and restore drill, keys, alerts, what happens when things break |
| [docs/SECURITY.md](docs/SECURITY.md) | Trust model, controls and how they were checked, known gaps |
| [docs/BROWSER_VERIFICATION.md](docs/BROWSER_VERIFICATION.md) | The optional, policy-gated browser adapter |
| [frontend/README.md](frontend/README.md) | The dashboard |
