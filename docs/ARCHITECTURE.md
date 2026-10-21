# Architecture

A modular monolith (one FastAPI app, one worker image) with clear internal boundaries.
Nothing here is a microservice; every boundary below is a package that could become one.

```
 client ──HTTPS──▶ FastAPI ──────────────▶ PostgreSQL   (source of truth)
                     │  submit/pause/…          ▲
                     ▼                          │ checkpoints, results, audit
                  Redis ◀── wake-up hints ── Dramatiq workers ──▶ Amazon SP-API
                (queue + rate buckets)         (BrandProcessor)     (or offline mock)
```

| Package | Responsibility | I/O? |
|---|---|---|
| `app/domain` | Rules engine, brand matching, candidate ranking, brand summary, check keys | none |
| `app/amazon` | Client protocol, typed errors, response parsers, offline mock | network (live, Phase 3) |
| `app/services` | Ingestion, job lifecycle, dispatch/recovery, processor, reporting | DB |
| `app/api` | HTTP routes, API-key auth | HTTP |
| `app/workers` | Dramatiq actor, broker wiring, recovery sweeper | Redis |

## 1. Evidence is stored apart from the status derived from it

`check_observations` is append-only: what a source returned (parsed `signals`, the raw body, HTTP
status, outcome, timestamp, evidence reference). `eligibility_checks.status` is *derived* by
`domain/classification.py` and carries the `rule_id`, `rationale` and `rules_version` that produced
it. `services/evidence.reclassify_check` re-derives any status from stored evidence, so a rules
change can be replayed over history and a corrupted status can always be recomputed.

### Classification rules (SP-API source)

| Observed | Derived status |
|---|---|
| Well-formed body, `restrictions: []` for the asked ASIN/condition/marketplace | `VERIFIED_LISTABLE` |
| `APPROVAL_REQUIRED` whose message is about the brand and not a category | `BRAND_APPROVAL_REQUIRED` |
| `APPROVAL_REQUIRED` otherwise (category, both, unclear, empty message) | `OTHER_APPROVAL_REQUIRED` |
| `NOT_ELIGIBLE` | `NOT_ELIGIBLE` |
| `ASIN_NOT_FOUND`, unknown reason code, entry without reasons, wrong marketplace/condition | `NEEDS_MANUAL_CHECK` |
| No `restrictions` list, malformed body, HTTP/auth/timeout failure | `NEEDS_MANUAL_CHECK` |

Several blockers on one ASIN: `NOT_ELIGIBLE` > `BRAND_APPROVAL_REQUIRED` > `OTHER_APPROVAL_REQUIRED`.
Sources that disagree, or one saying "listable" while another is ambiguous, give `NEEDS_MANUAL_CHECK`
(`SOURCE_CONFLICT`). A source that produced *no answer* never overrides a real answer.

### Classification rules (screen evidence: browser adapter or a human reviewer)

Both record the same `BrowserSignals` and are classified by the same rules; a reviewer records what
they *saw*, not a verdict. Checked in this order:

| Signals | Derived |
|---|---|
| signed out / challenge / page not loaded / failed outcome | no usable answer (`NEEDS_MANUAL_CHECK`) |
| product unavailable | `NEEDS_MANUAL_CHECK` |
| ASIN on screen is not the requested one; brand on screen differs; brand unconfirmed anywhere | `NEEDS_MANUAL_CHECK`, **no information** (evidence about another product is never used) |
| approval notice naming the brand / otherwise; "not eligible" notice | `BRAND_APPROVAL_REQUIRED` / `OTHER_APPROVAL_REQUIRED` / `NOT_ELIGIBLE` |
| target condition not selected | `NEEDS_MANUAL_CHECK` (the sell control says nothing yet) |
| `"<Used, Collectible…> conditions: …"` notice that names the target condition; any unrecognised notice | `NEEDS_MANUAL_CHECK` (a restriction may exist) |
| *Apply to sell* shown | `OTHER_APPROVAL_REQUIRED` |
| *Sell this product* **enabled**, everything above clear | `VERIFIED_LISTABLE` |
| *Sell this product* disabled / no recognised control | `NEEDS_MANUAL_CHECK` |

**Combining sources.** Per source, only the latest observation counts. "No information" ambiguity
(an ASIN the API does not know, a screen about another product, a condition not chosen) is dropped
when anything informative exists, so a person's screen evidence can resolve what the API could not.
Ambiguity that *suggests a restriction* (an unrecognised reason code, an applicable limitation) and
any disagreement between conclusive answers become `SOURCE_CONFLICT`: never resolved by whoever
spoke last. A second randomised test (6,000 cases) asserts `VERIFIED_LISTABLE` is produced only when
every positive condition holds.

**Invariant, enforced by a 3,000-case randomised test (API evidence):** `VERIFIED_LISTABLE` is produced only for a
literally empty `restrictions` list in an otherwise well-formed response.

### Brand-level summary

Computed from the ASINs actually checked, and always labelled as describing the sample
(`brand_wide_claim: false`).

| Checked ASINs | Brand status |
|---|---|
| No brand-matched candidate (and discovery finished) | `NO_PRODUCT_FOUND` |
| Discovery did not finish | `NEEDS_MANUAL_CHECK` (`BRAND_DISCOVERY_INCOMPLETE`) |
| Disagree with each other | `MIXED_RESULTS` |
| All the same non-listable status | that status |
| All listable, ≥ `min_sample_size` ASINs **or** every matching ASIN checked and search not truncated | `VERIFIED_LISTABLE` (`BRAND_SAMPLE_LISTABLE`) |
| All listable but fewer, or planned checks unfinished | `NEEDS_MANUAL_CHECK` (the listable ASINs remain listed per product) |

Candidates are validated on Amazon's **brand attribute**, not title keywords. Every candidate is kept
with the reason it was rejected. Sales rank is never used.

## 2. Durable execution

State machines (the database row is authoritative; queue messages are only hints):

```
brand:  PENDING ──dispatch──▶ QUEUED ──claim (CAS)──▶ IN_PROGRESS ──▶ COMPLETED
           ▲                     ▲  │                     │  └────────▶ FAILED   (dead-lettered)
           │ pause / lease       │  └─ defer (429, pacing) │  └────────▶ CANCELLED
           └─────────────────────┴──────────────────────── ┘
job:    RUNNING ⇄ PAUSED ;  RUNNING/PAUSED ─▶ CANCELLED ;  RUNNING ─▶ COMPLETED
```

* **Windowed dispatch.** A seller has at most `SELLER_MAX_CONCURRENT_BRANDS` brands `QUEUED` or
  `IN_PROGRESS` across all their jobs. A large job cannot flood the queue or starve other sellers.
  Each finished brand refills the window.
* **Claim** is a compare-and-set `QUEUED → IN_PROGRESS` with a lease. Duplicate or stale messages
  find nothing to claim and are ignored.
* **Lease fencing.** Every progress write re-verifies ownership in the same transaction (row lock on
  PostgreSQL). A worker whose lease expired stops writing; it cannot corrupt a brand another worker
  now owns.
* **Checkpoints.** Each catalog page and each ASIN check commits before the next step. Resuming
  skips finished work. Re-running is idempotent (candidates upsert by ASIN; checks are keyed by a
  hash of seller + marketplace + ASIN + condition + verification context, unique per job).
* **Recovery** (`services/dispatch.recover`, run by a sweeper thread in every worker and by
  `python -m app.cli recover`): expired leases → `PENDING` (attempt counted, so a brand that keeps
  killing workers is dead-lettered); `QUEUED` brands that lost their message → `PENDING`; refill
  windows; complete jobs.
* **Pause/cancel** are honoured at every checkpoint, so they take effect within one Amazon call.

### Failure taxonomy

| Failure | Response | Bound |
|---|---|---|
| Amazon 429 | Defer the brand using `Retry-After`; no ASIN penalised | `MAX_THROTTLE_DEFERRALS`, then dead-letter |
| Our per-seller token bucket empty | Sleep if wait ≤ `MAX_RATE_WAIT_SECONDS`, else defer | none needed (self-limiting) |
| Transient error on one ASIN (5xx, timeout, malformed) | Retry with backoff | `MAX_CHECK_ATTEMPTS`, then that ASIN → `NEEDS_MANUAL_CHECK`, brand continues |
| Request for one ASIN rejected (4xx) | That ASIN → `NEEDS_MANUAL_CHECK` immediately | — |
| Credentials rejected | **Pause the whole job** (`AUTH_REQUIRED`); not recorded as evidence about any product | until the seller reconnects and resumes |
| Unexpected error | Exponential backoff with jitter, capped | `MAX_BRAND_ATTEMPTS` consecutive failures, then dead-letter |
| Worker killed | Lease expires, recovered from checkpoint | counts toward `MAX_BRAND_ATTEMPTS` |

Dead-lettering keeps every per-ASIN result, sets the brand to `NEEDS_MANUAL_CHECK`
(`BRAND_DEAD_LETTERED`) and lists it in the *Manual Review* sheet with the last error. `attempts`
resets whenever progress is made, so only *consecutive* failures count.

### What protects what (all proven by tests against PostgreSQL)

| Hazard | Mechanism | Test |
|---|---|---|
| Two workers process one brand | CAS claim | 16 threads, exactly one wins |
| Slow worker overwrites its successor | Lease fencing | lost-lease test |
| Seller exceeds its window under concurrency | Per-seller advisory lock | 8 threads × 20 attempts |
| Last two brands finish together, job never completes | Per-job advisory lock (`lock_job_completion`) taken before any row write in the completion path | deterministic barrier test + deadlock regression test |
| Cross-seller data reference | Composite `(id, seller_account_id)` FKs | model tests |
| Migration drifts from models | Alembic autogenerate diff must be empty | migration test |

## 3. Rate limits

Token buckets per `(seller, operation)`: in-memory for tests, Redis Lua (atomic, server clock) for
workers. Defaults (`searchCatalogItems` 2/s burst 2, `getListingsRestrictions` 5/s burst 10) are
configuration, not constants: **verify them against the seller's current SP-API usage plan before
raising them.** Adding workers does not raise throughput, because the buckets are shared.

## 4. Amazon authorisation and the live client

India (`A21TJRUUN4KGV`) is served by the **EU** SP-API endpoint. Requests carry one LWA access token
(`x-amz-access-token`); AWS SigV4/IAM has not been required since October 2023.

| Concern | Design |
|---|---|
| Private app, seller's own org | **Self-authorisation**: the seller generates a refresh token for their own app and supplies it once (`PUT /amazon/connection` or `python -m app.cli connect-seller`, token via stdin/file, never an argument). It is proven valid with Amazon before anything is stored. |
| Public / multi-seller app | **Website authorisation**: `POST /amazon/authorize` returns the Seller Central consent URL; Amazon redirects the seller's browser to `GET /amazon/callback` with `state`, `selling_partner_id` and a 5-minute `spapi_oauth_code`, which is exchanged for a refresh token. The seller never types a password or token into this app. |
| Callback authentication | The callback is the only unauthenticated route (a browser redirect carries no API key). `state` is 256-bit, single-use, expiring (15 min) and consumed atomically before the exchange; unknown, expired and spent states are indistinguishable; the HTML response reflects no input. |
| Token storage | Refresh tokens are Fernet-encrypted (`CREDENTIAL_ENCRYPTION_KEYS`, newest first, rotation supported) with the seller id inside the ciphertext, so a blob copied to another seller's row is refused. Access tokens live only in process memory. Tokens are never logged, returned or placed in error messages (tested). |
| Token refresh | Cached per seller, renewed 60 s early, single-flight under a lock. A 401/403 triggers exactly one forced refresh and retry, because the cached token may simply have expired. |
| `invalid_grant` | The seller's token is dead: credential marked `REVOKED`, job paused, dashboard shows *reconnect*. |
| `invalid_client` | Our **application** secret is wrong: job paused, but the seller's credential is **not** revoked (no seller action can fix it). |
| 403 that survives a refresh | Almost always a missing *Product Listing* role or an unauthorised app: job paused with that hint. |
| Catalog search rejected (HTTP 4xx) | Permanent for that brand: dead-lettered immediately, not retried. |

`searchCatalogItems` is called with `keywords`, one `marketplaceIds`, `includedData=summaries,productTypes`,
`pageSize≤20`, `pageToken`. `getListingsRestrictions` with `asin`, `sellerId`, `marketplaceIds`,
`conditionType`, `reasonLocale` (default `en_US`, because the classifier reads English reason text).
Amazon's `brandNames` catalog filter is deliberately **not** used: validation happens on the returned
brand attribute, and the filter's exact matching semantics should be measured on a real account first.

### What is and is not verified

Verified by tests: request construction, token lifecycle, every HTTP status mapping, the full job over
the live code path against a strict fake of Amazon's HTTP surface (`tests/fake_spapi.py`), encryption,
and the OAuth callback's security properties.

**Not verified here (no Amazon access from CI):** behaviour against the real service. The fake encodes
Amazon's *documented* contract (endpoints, parameters, error shapes, reason codes), so any gap between
documentation and reality will only show up on a real account. Treat the first live run accordingly
(see README).

## 5. Reports and exports

`GET /api/v1/jobs/{id}/export.xlsx` builds four sheets from committed rows (REPEATABLE READ snapshot,
column-only loading, ordered by the stored `seq`). Strings beginning `= + - @` (brand names and
product titles are attacker-influenced) are forced to text cells; a test scans the raw XML for formula
elements. Jobs run against the offline mock are labelled `MOCK` on every row and in the file properties.

Building a workbook is CPU- and memory-bound (about 36 s for 50,000 checks on the benchmark machine), so
it must not run on an API worker. The synchronous endpoint refuses jobs above `EXPORT_SYNC_MAX_CHECKS`
(default 10,000) with `409`; larger jobs use `POST /jobs/{id}/exports`, which queues a `build_export`
Dramatiq message, then `GET .../exports/{id}` to poll and `GET .../exports/{id}/download`. Files live
in `EXPORT_DIR` (shared volume), are purged after `EXPORT_TTL_HOURS`, and a `RUNNING` export whose worker
died (`EXPORT_STALE_MINUTES`) is failed by the recovery sweeper instead of hanging forever.

## 6. Performance

`python scripts/perf.py reads|pipeline` (PostgreSQL; Redis for the pipeline). Measured on the
development container with 50,000 checks seeded for one job:

| Operation | Before | After | What changed |
|---|---|---|---|
| Job progress | not profiled | 46 ms | single aggregate query |
| Checks page (offset 40,000) | 160 ms (first page, redundant joins) | 46 ms | stored `seq` + `ix_checks_job_seq`; two-step query (ids first, then rows) |
| Checks, status filter | not profiled | 26 ms | `ix_checks_job_status_seq` |
| Checks, substring search | not profiled | 108 ms | still a scan; fine at this size, trigram index if it grows |
| Review queue | 1,458 ms (Python-side slicing) | 42 ms | SQL-side pagination |
| `/metrics` | not profiled | 38 ms | DB-derived gauges are cheap aggregates, cached briefly |
| Excel export, 50,000 checks | 49.6 s (CPU-bound) | 36 s | column-only loading; moved off the API process (async path) |

Pipeline: 300 brands x 5 ASINs run at roughly 30 checks/s per worker process (79 SQL statements per
brand). **The SP-API rate limit binds long before orchestration does**: the realistic scenario in
`scripts/perf.py pipeline` ran at about 101% of the time the rate limits alone impose, so adding workers does not make jobs faster. The only levers are the seller's actual
SP-API usage plan and checking fewer ASINs per brand (the job's `max_asins_per_brand`, default 5, range 1-10).

## 7. Observability

* `/metrics` (Prometheus; optional bearer token via `METRICS_TOKEN`): HTTP requests/latency, Amazon
  calls/latency by operation and outcome, brand tasks by outcome, checks classified by status, LWA
  refreshes, and DB-derived gauges (jobs by status, brands by status, queue depth, oldest queued age).
  Multi-process mode via `PROMETHEUS_MULTIPROC_DIR`.
* JSON logs with request/job/brand/check correlation ids; secrets, tokens and authorization headers are
  redacted by a logging filter (tested).
* Optional OpenTelemetry tracing (`pip install .[telemetry]`, standard `OTEL_*` variables). The
  SQLAlchemy instrumentation is skipped with a warning on SQLAlchemy 2.1.

## 8. Security notes (current state)

* API keys: 256-bit random, shown once, stored as SHA-256. Cross-seller access is a 404, never 403.
* Uploads: size limit, zip-bomb check before parsing, formulas never evaluated, row/column caps.
* Search parameters are escaped (`%`/`_` are literal).
* Amazon credentials: encrypted at rest, bound to the seller, never exposed (section 4).
* HTTP hardening: security headers, request body cap (413 as soon as the declared or streamed size
  exceeds it), per-key API rate limiting, authentication-failure throttling per client address (both
  fail open if Redis is down, because a limiter outage must not become an API outage), API docs disabled
  when `ENVIRONMENT=production`.
* Deployment hardening (non-root, read-only containers, network segmentation, proxy-header trust, CSP,
  backups) and the remaining gaps (no user accounts/SSO, unencrypted backups, secrets in an env file) are
  in [SECURITY.md](SECURITY.md).

## 9. Deployment

Caddy -> API -> PostgreSQL/Redis, Dramatiq workers, optional Prometheus/Grafana: see
[DEPLOY.md](DEPLOY.md). Two design points worth knowing:

* **Metric files are named `<service>-<pid>`** (`METRICS_INSTANCE`), not by PID alone. Containers have
  separate PID namespaces, so two containers sharing a metrics volume would otherwise overwrite each other's
  counters. The name is stable so a restart continues its own counters and files do not accumulate; the price is
  one container per service name.
* **The API trusts `X-Forwarded-For` from exactly one address** (Caddy's, pinned in the compose network),
  because per-client throttling is meaningless if every client appears to be the proxy.
