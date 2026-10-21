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

**Invariant, enforced by a 3,000-case randomised test:** `VERIFIED_LISTABLE` is produced only for a
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
| Last two brands finish together, job never completes | Job-row lock in `maybe_complete_job` | deterministic barrier test |
| Cross-seller data reference | Composite `(id, seller_account_id)` FKs | model tests |
| Migration drifts from models | Alembic autogenerate diff must be empty | migration test |

## 3. Rate limits

Token buckets per `(seller, operation)`: in-memory for tests, Redis Lua (atomic, server clock) for
workers. Defaults (`searchCatalogItems` 2/s burst 2, `getListingsRestrictions` 5/s burst 10) are
configuration, not constants: **verify them against the seller's current SP-API usage plan before
raising them.** Adding workers does not raise throughput, because the buckets are shared.

## 4. Reports

`GET /api/v1/jobs/{id}/export.xlsx` builds four sheets from committed rows at request time. Strings
beginning `= + - @` (brand names and product titles are attacker-influenced) are forced to text
cells; a test scans the raw XML for formula elements. Jobs run against the offline mock are labelled
`MOCK` on every row and in the file properties.

## 5. Security notes (current state)

* API keys: 256-bit random, shown once, stored as SHA-256. Cross-seller access is a 404, never 403.
* Uploads: size limit, zip-bomb check before parsing, formulas never evaluated, row/column caps.
* Search parameters are escaped (`%`/`_` are literal).
* **Not yet built (Phase 3–5):** SP-API authorisation and credential encryption, TLS/reverse proxy,
  request-size and API rate limits, user login, security headers, backups.
