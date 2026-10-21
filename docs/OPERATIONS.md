# Operations runbook

Commands assume the compose deployment ([DEPLOY.md](DEPLOY.md)) and run from the repository root.

## Health

```bash
docker compose ps                                  # every service "healthy"; migrate "exited (0)"
curl -s https://your-host/readyz                   # {"status":"ready"} (checks the database)
docker compose logs --tail 100 api worker          # JSON logs; each line carries request/job ids
```

Logs are JSON (`LOG_JSON=true`), secrets and tokens are redacted, and Docker rotates them (10 MB × 5 per
service).

## Backups

What the database holds: sellers, jobs, brands, candidates, **every check and its evidence history**, the
audit trail, encrypted Amazon credentials. It is the source of truth: queue messages in Redis are only hints, and
a lost message is re-created from the database.

```bash
scripts/ops/backup.sh                      # backups/eligibility-<UTC>.dump, verified, 14 newest kept
RETENTION=30 BACKUP_DIR=/mnt/backups scripts/ops/backup.sh
```

Schedule it (host cron, one line): `17 2 * * * cd /opt/eligibility && scripts/ops/backup.sh >> backups/backup.log 2>&1`.
A dump that cannot be listed by `pg_restore --list`, or that contains no table data, is discarded rather than
kept.

**Not in the dump, back these up separately:**

| What | Why |
|---|---|
| `.env`, in particular `CREDENTIAL_ENCRYPTION_KEYS` | Without it stored Amazon refresh tokens cannot be decrypted (sellers must reconnect). |
| Your off-site copy of the dumps | A backup on the same disk is not a backup. **Dumps are not encrypted**: encrypt the destination (or pipe through `age`/`gpg`). |

Not worth backing up: Redis (rebuildable), the `exports` volume (reports expire after 24 h and can be
regenerated), `caddy_data` (certificates are re-issued).

### Prove a backup restores (do this on a schedule, not during an incident)

```bash
scripts/ops/restore.sh backups/eligibility-20261021T021700Z.dump --into eligibility_check
```

Restores into a scratch database next to the live one, prints every table's row count, and touches nothing
live. Compare with production, then drop it as the script suggests.

### Restore into production

```bash
scripts/ops/restore.sh backups/<file>.dump --replace-live      # asks you to type the database name
```

Stops `web`, `api` and `worker`, replaces the database, runs migrations to the current code's schema, and
starts everything, returning only when all services are healthy. Brands that were mid-flight when the backup
was taken resume on their own once a worker finds their lease expired (within `BRAND_LEASE_SECONDS`, default 5
minutes); `docker compose exec worker python -m app.cli recover` does it immediately.

Tested end to end on the running stack: dump, wipe the data, restore into the live database, same API key works,
jobs and checks are back, and a new job completes through the worker afterwards.

## Sellers and keys

```bash
docker compose exec api python -m app.cli list-sellers
docker compose exec api python -m app.cli create-seller --name "Acme Traders"
docker compose exec api python -m app.cli rotate-key --seller-account <uuid>      # the old key dies at once
docker compose exec api python -m app.cli deactivate-seller --seller-account <uuid>  # revokes access, pauses running jobs
docker compose exec api python -m app.cli activate-seller --seller-account <uuid>    # paused jobs stay paused
```

Keys are 256-bit and stored only as hashes: a lost key cannot be recovered, only rotated. Every rotation,
deactivation and activation is written to the audit trail (prefixes only, never a key).

**Rotating `CREDENTIAL_ENCRYPTION_KEYS`:** generate a new key, put it *first* in the comma-separated list (keep the
old one after it), restart `api` and `worker`. Existing tokens still decrypt; each is re-encrypted with the new key
when the seller's credential is next saved (reconnecting). Remove the old key only after every seller has
reconnected.

## Alerts

(`docker/prometheus-rules.yml`; the same signals are on the Grafana dashboard.)

| Alert | Means | Do |
|---|---|---|
| `EligibilityApiDown` | Prometheus cannot scrape the API | `docker compose ps`, `logs api`; check `METRICS_TOKEN`. |
| `EligibilityWorkerLeasesExpired` | Brand tasks are leased to workers that stopped heartbeating | Workers are crashing or hung: `logs worker`, memory limits. The sweeper re-queues the work. |
| `EligibilityQueueStuck` | A queued brand has waited > 15 min | Is `worker` healthy? Is Redis up? Or is a seller's concurrency window full (`SELLER_MAX_CONCURRENT_BRANDS`)? |
| `EligibilityJobsPausedForAuth` | Amazon rejected the credentials | The seller reconnects (dashboard → Amazon connection). If **every** seller is affected, the app's `LWA_CLIENT_SECRET` is probably wrong or rotated. |
| `EligibilityCredentialsRevoked` | A seller revoked access in Seller Central | The seller reconnects. |
| `EligibilityBrandDeadLettered` | A brand exhausted its retries | It is in that job's *Needs review* tab with the last error; retry it from the dashboard. |
| `EligibilityAmazonErrorRatio` | > 20 % of SP-API calls failing | Break down `eligibility_amazon_requests_total` by `outcome`: `rate_limited` (lower `RATE_*`, check the usage plan), `auth`, `request_error`. |
| `EligibilityApiServerErrors` / `EligibilityApiSlow` | 5xx above 5 % / p95 above 2 s | `logs api`; database load. |

## What happens when things break

None of this needs operator action. Each row has an automated test (`test_resilience.py`, `test_exports.py`; the full suite runs against real PostgreSQL), except where marked.

| Event | Result |
|---|---|
| Worker killed mid-brand | Its lease expires; another worker resumes after the last checkpointed ASIN. No ASIN is checked twice or skipped. |
| Redis lost or flushed | Queue messages vanish, but state is in PostgreSQL: the sweeper re-dispatches brands stuck in `QUEUED` (tested as "a lost message"). Rate-limit buckets also reset, so pacing is briefly more permissive *(a consequence of the design, not tested)*. |
| API restarts | Nothing in flight is lost; clients retry. |
| Amazon throttles (429) | The brand is deferred with backoff; the seller's other brands continue. |
| Amazon returns garbage for one ASIN | That ASIN becomes `NEEDS_MANUAL_CHECK`; it is never treated as listable. |
| Amazon rejects the credentials | The whole job pauses (not one brand) and the dashboard says to reconnect. |
| Large report worker dies | The export is failed after `EXPORT_STALE_MINUTES` (30) instead of hanging; request it again. |

## Housekeeping

* **Disk:** Postgres grows with stored raw Amazon responses (evidence). Reports in `exports` expire after
  `EXPORT_TTL_HOURS` (24) and are purged. Docker logs are capped.
* **Metrics volume:** counters live in files named `<service>-<pid>`, so they survive restarts and do not pile up.
  Only a handful of files exist per service.
* **Updating images:** base images are pinned by tag (not digest). Rebuild periodically (`docker compose build --pull`)
  and run `pip-audit` / `npm audit`.
