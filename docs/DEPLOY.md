# Deploying with Docker Compose

One host, one command. The stack is Caddy (TLS) → API → PostgreSQL and Redis, with Dramatiq workers.
Tested with Docker Engine 29.9 and Compose 5.6 on Linux.

```
internet ─► web (Caddy :80/:443) ─► api ─► postgres
                 │                    │ └► redis ◄─ worker ─► Amazon SP-API
                 └ dashboard (static) └ /metrics: internal only
```

| Service | Role |
|---|---|
| `web` | Caddy: TLS, the dashboard's static files, proxies `/api`, `/healthz`, `/readyz`. Returns 404 for `/metrics`. |
| `api` | FastAPI. Stateless; many requests can be in flight. |
| `worker` | Dramatiq. Runs brand checks and builds large Excel reports. |
| `migrate` | One-shot: `alembic upgrade head` before `api` and `worker` start. |
| `postgres`, `redis` | State. On a network with **no route to the internet and none from the edge**. |
| `prometheus`, `grafana` | Optional (`--profile monitoring`). |

## 1. Quickstart (demo data, no Amazon account needed)

```bash
cp .env.example .env
# set POSTGRES_PASSWORD and REDIS_PASSWORD (required); leave AMAZON_MODE=mock
docker compose up -d --build
docker compose exec api python -m app.cli create-seller --name "My company"   # prints the API key once
```

Open `http://localhost` (set `HTTP_PORT` if 80 is taken), paste the key. Jobs run against built-in
fixtures and are labelled **demo data** everywhere, including the Excel report. Try the brands `eSSL`,
`Havells`, `Kangaro`, `Taparia`, `Oddy` and `Acme Audio` (the last one lands in manual review).

## 2. Production

1. **DNS** for your hostname must point at the host, and ports 80/443 must be reachable (Caddy obtains
   and renews a Let's Encrypt certificate itself). In `.env`:
   ```
   SITE_ADDRESS=eligibility.example.com
   ALLOWED_HOSTS=eligibility.example.com,localhost,127.0.0.1,api
   ```
   *Not verified here:* certificate issuance, because the test environment has no public DNS. Caddy's
   behaviour for a hostname site address is its documented default.
2. **Secrets** in `.env`: long random `POSTGRES_PASSWORD`, `REDIS_PASSWORD`. Keep `.env` out of git and
   readable only by the operator (`chmod 600 .env`). Back it up separately from the database: see
   `CREDENTIAL_ENCRYPTION_KEYS` below.
3. **Live Amazon data** (`AMAZON_MODE=live`): follow "Going live" in the [README](../README.md#going-live-read-before-your-first-real-run),
   then set `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET` and `CREDENTIAL_ENCRYPTION_KEYS` in `.env`
   (`docker compose run --rm api python -m app.cli generate-encryption-key`). **Losing that key makes
   every stored Amazon refresh token unreadable; sellers would have to reconnect.** Connect a seller from the
   dashboard's *Amazon connection* page, or with `connect-seller`.
   *Not verified here:* behaviour against the real Selling Partner API. Everything was verified against a
   fake of Amazon's documented contract. Start with two or three brands and compare with Seller Central.
4. **Start:** `docker compose up -d --build`, then create sellers as above.
5. **Back up** from day one: [OPERATIONS.md](OPERATIONS.md#backups).

## 3. Upgrades

```bash
scripts/ops/backup.sh                 # always first
git pull
docker compose up -d --build          # migrations run before api and worker restart
```

The API is briefly unavailable while it restarts. Jobs in flight are not lost: workers checkpoint between
ASINs, and brands whose worker disappeared are re-queued automatically. Rolling back means restoring the
backup and starting the previous version (`git checkout <tag>`); migrations are not reversible in place.

## 4. Capacity and scaling

Run **one `worker` container**; tune `WORKER_PROCESSES` × `WORKER_THREADS` (default 2 × 4). Throughput is
capped by Amazon's per-seller rate limits, not by CPU: in the benchmark the realistic scenario ran at 101% of
the time the rate limits alone impose, so more workers do not finish jobs sooner. The limiters are shared
through Redis, so extra workers would be *safe*, just pointless.

Do not use `docker compose up --scale worker=N` without reading this: metric files are named by
`METRICS_INSTANCE` (`worker`) and PID, so two containers with the same PID would share a file and corrupt each
other's counters. If you really need more than one, give each its own service with its own `METRICS_INSTANCE`.

## 5. Optional builds

| Need | How |
|---|---|
| OpenTelemetry traces | `BACKEND_EXTRAS=telemetry` in `.env`, set `OTEL_EXPORTER_OTLP_ENDPOINT`, rebuild. |
| Browser verification | `WITH_BROWSER=1` in `.env`, rebuild, then read [BROWSER_VERIFICATION.md](BROWSER_VERIFICATION.md). Off by default and policy-gated. *Not built or tested in this environment.* |

## 6. Monitoring

```bash
# .env: METRICS_TOKEN=<long random>, GRAFANA_ADMIN_PASSWORD=<something else>
docker compose --profile monitoring up -d
ssh -L 3000:127.0.0.1:3000 your-host      # Grafana is bound to localhost only
```

Grafana (provisioned dashboard *Eligibility platform*) and Prometheus (nine alert rules, see
[OPERATIONS.md](OPERATIONS.md#alerts)) start together. Prometheus has no Alertmanager attached: wire
`alerting:` in `docker/prometheus.yml` to yours, or watch the *Alerts* page in Prometheus.

## 7. Acceptance test

`scripts/e2e_compose.sh` runs the dashboard's Playwright suite against this exact deployment (a
throwaway `.env.e2e`, plain HTTP on `127.0.0.1:8080`, the offline Amazon mock): sign-in, a job through the real
worker, evidence, manual review, both Excel export paths and tenant isolation. `--down` removes it afterwards.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `api` unhealthy | `docker compose logs api migrate`. Usually a database password mismatch after changing `POSTGRES_PASSWORD` on an existing volume (it is only read when the volume is created). |
| `400 Invalid host header` | `ALLOWED_HOSTS` does not include the hostname users type. |
| Everyone shares one rate limit / lockout | The API is seeing the proxy's address. It trusts `X-Forwarded-For` only from the pinned Caddy address `172.29.0.10`; if you changed the `edge` subnet, change `FORWARDED_ALLOW_IPS` with it. |
| Prometheus target `401` | `METRICS_TOKEN` is empty or was changed without recreating `api` and `prometheus`. |
| `/metrics` is empty or errors after an upgrade that changed metric labels | `docker compose down && docker volume rm eligibility_metrics && docker compose up -d`. Counters restart from zero. |
| Large report "failed" | `docker compose logs worker`; check disk space on the `exports` volume. |
