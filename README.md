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
   Over HTTP, another seller's job is indistinguishable from one that does not exist (404).
5. **The database is the source of truth; the queue is only a doorbell.** Losing Redis, a
   message, or a worker loses no work and cannot run a brand twice (see *How a job runs*).
6. **Demo data never passes for real data.** Mock-mode jobs carry a notice in the API and a
   red DEMO banner on every sheet of the export. Live mode refuses to start rather than fall
   back to fixtures, and a worker never runs a job created in the other mode.
7. **No seller passwords, no clear-text tokens.** Sellers approve the app inside Seller
   Central. The resulting LWA refresh token is stored encrypted, bound to its seller account,
   and decrypted only inside a worker when an access token is needed.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Schema, API contracts, rules engine, mocked Amazon responses | done |
| 2 | Ingestion, queue, orchestration, checkpointing, Excel reports | done |
| 3 | Seller authorisation, live SP-API | done |
| 4 | Policy-compliant browser verification, manual review | next |
| 5 | Dashboard, observability, hardening, Docker Compose | planned |

## Quick start (no setup)

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
cd backend && ../.venv/bin/python -m app.cli demo
```

Runs the five brands from the client's workbook through the whole pipeline against offline
fixtures in a throwaway in-memory database, prints the brand-level results and writes
`demo-report.xlsx`. Every sheet is stamped DEMO DATA.

## Running it

**Single machine (prototype):** API, scheduler and worker in one process, no Redis.

```bash
cd backend
export DATABASE_URL=sqlite:///./dev.db INLINE_WORKER=true RATE_LIMITER=memory
../.venv/bin/python -m app.cli init-db
../.venv/bin/python -m app.cli create-seller "Acme Traders"     # prints the API key once
../.venv/bin/uvicorn app.api.main:app --port 8000              # docs at /docs
```

**Deployment shape:** PostgreSQL + Redis, and three process types that scale independently.

```bash
cd backend && ../.venv/bin/alembic upgrade head
../.venv/bin/uvicorn app.api.main:app                          # API (stateless, any number)
../.venv/bin/python -m app.workers.scheduler                   # scheduler (one is enough; more is safe)
../.venv/bin/dramatiq app.workers.tasks --queues brand_checks  # workers (any number)
```

`python -m app.cli drain` runs every open job to completion in-process, for batch use.

## Connecting to Amazon (live mode)

The app calls two SP-API operations for the seller's own account: `searchCatalogItems`
(Catalog Items 2022-04-01) and `getListingsRestrictions` (Listings Restrictions 2021-08-01),
on the Europe endpoint that serves amazon.in, marketplace `A21TJRUUN4KGV`. Requests carry an
LWA access token only (SP-API dropped AWS SigV4 signing in October 2023).

**1. Register the app** in Seller Central India, under Apps and Services > Develop Apps, with
two roles:

| Role | Why |
|---|---|
| Product Listing | catalog search and listing restrictions (the actual checks) |
| Selling Partner Insights | the connection check confirms the account sells on amazon.in |

For one seller organisation, use a **private** app. Offering this to unrelated sellers needs
a **public** app and Amazon's public-app process.

**2. Configure the server** (`.env`, see `.env.example`):

```bash
AMAZON_MODE=live
LWA_CLIENT_ID=amzn1.application-oa2-client....      # from the app's LWA credentials
LWA_CLIENT_SECRET=...                                 # Amazon rotates these every 180 days
CREDENTIALS_ENCRYPTION_KEYS=<output of: python -m app.cli generate-key>   # store apart from DB backups
```

Then apply the new migration: `cd backend && ../.venv/bin/alembic upgrade head`.

**3. Connect the seller.** Pick one of two ways:

* **Private app (simplest).** In Seller Central, under Develop Apps, choose *Authorize app*.
  Amazon then shows a refresh token. Find the seller's Merchant Token under Settings >
  Account Info. Then run:

  ```bash
  ../.venv/bin/python -m app.cli connect-seller <seller-account-id> --selling-partner-id <MERCHANT_TOKEN>
  # paste the refresh token when prompted; it is read from stdin, never from the command line
  ```

* **Consent page (website workflow).** Use this when the seller should approve the app
  themselves:
  1. Set `SPAPI_APPLICATION_ID` and `SPAPI_REDIRECT_URI` (`https://<your host>/api/v1/amazon/callback`), and register that same URI on the app.
  2. For a draft app, also set `SPAPI_APP_IS_DRAFT=true`.
  3. `POST /api/v1/seller/authorization/start` returns a Seller Central URL.
  4. The seller opens it, signs in to Amazon, and approves.
  5. Amazon redirects back to the callback, which stores the grant.

Either way, nothing is stored until the grant passes a **connection check**:

* Amazon accepts the token.
* The account participates in amazon.in.
* The app can search the catalog. This catches a missing Product Listing role on day one, not
  in the middle of a job.

If Amazon is unreachable during the check, the grant is saved as `PENDING_VERIFICATION`
and can be re-checked with `POST /seller/authorization/verify` or `cli verify-seller`.

**What happens to a grant over time**

| Event | Result |
|---|---|
| Access token expires (hourly) | Refreshed automatically, shared by all of a seller's workers |
| A token expires in flight (403) | Retried once with a fresh token |
| Seller revokes the app, role removed, refresh token past its 365 days | Seller's jobs pause; grant becomes `NEEDS_REAUTHORIZATION`; resume is refused until the seller connects again |
| `DELETE /seller/authorization` | Stored token erased, jobs paused. Revoking on Amazon's side is done in Seller Central > Manage Your Apps |
| Encryption keys changed so a token no longer opens | Treated as an authorisation failure: jobs pause, never fall back |

Rotate encryption keys by putting the new key first (`NEW,OLD`) and running
`python -m app.cli rotate-credentials`. Then drop the old key.

Every observation stores Amazon's `x-amzn-RequestId` as its evidence reference (the Excel
"evidence reference" column). Amazon support needs that ID to look up an individual call.

## API

All job routes take `Authorization: Bearer <seller API key>` and live under `/api/v1`.

| Route | Purpose |
|---|---|
| `POST /jobs` | Start a job from a JSON list of brands (plus optional settings) |
| `POST /jobs/upload` | Start a job from `.xlsx` / `.csv` / `.txt` (uses the "Brand" / "Brand Name" column if present) |
| `POST /jobs/preview` | Show accepted brands, duplicates and invalid rows without creating anything |
| `GET /jobs`, `GET /jobs/{id}` | Job status and progress counts |
| `POST /jobs/{id}/pause` · `resume` · `cancel` · `retry-failed` | Controls (idempotent where safe; `409` if impossible) |
| `GET /jobs/{id}/brands` | Brand-level results; filter by `status`, `result`, `q` |
| `GET /jobs/{id}/checks` | ASIN-level results; filter by `status`, `state`, `brand_id`, `q` |
| `GET /jobs/{id}/checks/{check_id}` | One check with its candidate and every raw observation |
| `GET /jobs/{id}/audit` | Audit trail |
| `GET /jobs/{id}/export.xlsx` | The report (below) |
| `GET /seller/authorization` | The seller's Amazon connection (never the token) |
| `POST /seller/authorization/start` | Seller Central consent URL (single-use state, 10 minutes) |
| `POST /seller/authorization/verify` | Re-run the connection check |
| `DELETE /seller/authorization` | Disconnect: erase the stored token and pause jobs |
| `GET /amazon/callback` | Where Seller Central returns the seller (no API key; the state authenticates it) |

Live mode refuses to create a job (`409`) until the seller's connection is `ACTIVE`.

## How a job runs

A job is split into brands; each brand is the unit of scheduling and carries its own
checkpoint. The scheduler hands a brand out by taking a short **queued lease** under a lock on
the seller's row (which is what makes the per-seller concurrency cap hold), then enqueues a
message. A worker turns that into a **worker lease** only if the message's token still matches,
and renews the lease between units of work. Every unit (a catalog page, one ASIN check)
commits on its own and every write re-checks the lease.

| What happens | Result |
|---|---|
| Queue message lost / Redis flushed | Queued lease expires, brand is dispatched again; no attempt charged |
| Message delivered twice, or a stale one arrives late | Token mismatch: refused, no Amazon calls |
| Worker killed mid-brand | Lease expires, brand retried; finished checks are not asked again |
| Worker stalls past its lease while another takes over | Its writes are refused (`LEASE_LOST`) |
| Amazon 429 / local rate limit | Brand deferred, attempt refunded |
| Seller authorisation rejected | All of that seller's jobs pause with a reason; resume after reauthorising |
| Timeout / 5xx on one ASIN | Only that check is retried (with backoff); its siblings still get answers |
| Retries exhausted | Brand dead-lettered as `FAILED`; its evidence lands in Manual Review; `retry-failed` re-runs it |
| Pause / cancel | Honoured at the next checkpoint; finished results are kept |

Catalog discovery rejects products whose brand attribute does not match (a title that merely
mentions the brand is not evidence), keeps every candidate for the record, and samples up to
`max_asins_per_brand` ASINs spread across categories, deterministically (Amazon's sales rank
and result order are deliberately not inputs).

## The Excel report

1. **Verified Listable Products** · 2. **Restricted Products** · 3. **Manual Review** ·
4. **Full Audit History** · 5. **Brand Summary**

Columns include brand, ASIN, title, marketplace, condition, status, approval reason and link,
rationale, verification method, seller account reference, evidence reference and checked time.
The export is built only from committed rows, says PARTIAL while a job is unfinished, never
lists an unchecked ASIN as listable, and neutralises spreadsheet formula injection in titles and
brand names.

## Development

The live path is tested end to end against `tests/fake_amazon.py`, an LWA + SP-API stand-in
served through `httpx.MockTransport`. The real client, token cache, error mapping and
orchestrator all run against it, so no Amazon credentials are needed.

```bash
.venv/bin/pytest            # offline: SQLite, mock Amazon, fake clock; no credentials needed
.venv/bin/ruff check backend && .venv/bin/ruff format --check backend
```

Against real services (also runs the migration-matches-models check, the Redis Lua limiter and
the multi-threaded race tests that SQLite cannot exercise):

```bash
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/eligibility_test \
TEST_REDIS_URL=redis://localhost:6379/15 .venv/bin/pytest
```
