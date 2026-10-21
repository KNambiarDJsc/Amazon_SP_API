# Security

What is protected, how, and, as importantly, what is **not**. Written for the operator deciding whether
this is safe to expose to the internet.

## Trust model

* Each **seller account** has one API key. Whoever holds it can do everything that seller can do, including
  creating jobs, reading all of that seller's results and disconnecting their Amazon authorisation.
* There are **no user accounts, roles, SSO or MFA.** The dashboard signs in with that same key. Treat the key
  like a password; rotate it if it may have leaked (`python -m app.cli rotate-key`).
* Sellers are mutually untrusted. Operators (anyone with shell or database access) are fully trusted.

## Controls and how they were checked

| Concern | Control | Checked by |
|---|---|---|
| Exposure | Only Caddy publishes ports (80/443). PostgreSQL and Redis sit on an `internal` network with no route in or out; Grafana binds to `127.0.0.1`. `/metrics` is 404 at the edge and needs a bearer token if `METRICS_TOKEN` is set. | Experiment on the running stack: the edge-only container cannot resolve `postgres` or `redis`; `/metrics` → 404 via Caddy and 401 without the token. |
| Containers | Non-root (uid 10001), read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`. (Exceptions with a reason in `docker-compose.yml`: PostgreSQL and Prometheus are not read-only; Caddy keeps `NET_BIND_SERVICE`.) | `id`, a write attempt to `/app`, and `CapEff` inside the running containers. |
| Data stores | Redis requires a password; PostgreSQL requires one for network access. Both passwords are mandatory in the compose file (it refuses to start without them). | Missing and wrong Redis passwords rejected; `compose config` fails when unset. |
| Client identity behind the proxy | The API trusts `X-Forwarded-For` only from Caddy's pinned address; Caddy ignores any value a client sends. Without this, the per-client rate limits and the failed-login throttle would see one address for everybody. | Three requests on the running stack: from Caddy (trusted, header honoured), from another container (header ignored), from outside through Caddy (spoof ignored). |
| Dashboard | Content Security Policy `default-src 'self'` with no inline scripts or styles, `frame-ancestors 'none'`, `nosniff`, `no-referrer`. Links from Amazon's responses are rendered only if `http(s)`. | A Playwright run across the main screens captured no CSP violations, with a positive control proving the probe sees one. Unit tests for the link filter. |
| API | Security headers, body-size caps (413), per-key rate limit, throttle on failed authentication per client, API docs off in production. | `test_security.py`. |
| Tenant isolation | Child rows reference parents through composite `(id, seller_account_id)` keys, so the database rejects cross-seller references. Another seller's job is a 404. | Model tests; Playwright test where seller B opens seller A's job URL. |
| API keys | 256-bit random; stored only as SHA-256; shown once; rotation invalidates the old key at once; deactivation also pauses running jobs. | `test_accounts_ops.py`, mutation-checked. |
| Amazon tokens | Encrypted with Fernet (`CREDENTIAL_ENCRYPTION_KEYS`, rotation supported); the seller id is inside the ciphertext so a blob moved to another seller's row is refused; never logged or returned; logs redact tokens and bearer values. | `test_credentials.py`, `test_security.py`. |
| Uploads and reports | Size and row caps, zip-bomb check before parsing, formulas never evaluated; cells starting `= + - @` are forced to text so a hostile brand name cannot become a spreadsheet formula. | `test_ingestion.py`; a test scans the report's XML for formula elements. |
| Browser automation | Off by default; per-seller written acknowledgement; self-identifying User-Agent; stops at any sign-in or CAPTCHA; cannot click sell/apply controls. | `docs/BROWSER_VERIFICATION.md`. Verified only against an offline fake. |
| Dependencies | `pip-audit`: no known vulnerabilities; `npm audit`: 0 vulnerabilities (both run 2026-10-21, including dev dependencies). | Re-run before each release. |

## Not built, and what to do about it

| Gap | Implication | Mitigation |
|---|---|---|
| No user accounts / SSO / MFA / per-key scopes / key expiry | A leaked key is a full compromise of that seller's data until rotated. | Rotate on any doubt; give each seller their own key; restrict access by network (VPN / IP allow-list in Caddy) if the audience is small. |
| API key kept in `sessionStorage` | Readable by any script running on the page; mitigated by the strict CSP, not eliminated. | Keep the CSP; do not add third-party scripts to the dashboard. |
| Backups are not encrypted | A stolen dump contains seller data and encrypted tokens. | Encrypt the backup destination or pipe dumps through `age`/`gpg`; the encryption keys are not in the dump. |
| Secrets live in a `.env` file | Anyone who can read it has the database and Redis passwords and the encryption keys. | `chmod 600`; or inject from your secret manager. No integration is built. |
| No TLS inside the Docker network | Traffic between containers on one host is unencrypted. | Acceptable on a single host; do not stretch this network across hosts. |
| Images pinned by tag, not digest; no SBOM or image scanning | A tag can be re-pushed upstream. | Pin digests and scan in CI for a regulated environment. |
| No WAF / DDoS protection | Caddy and the API rate limits handle ordinary abuse only. | Put a CDN or cloud WAF in front for hostile environments. |
| No audit-log export UI | The audit trail is in PostgreSQL and on each job's *Activity* tab. | Query `audit_events` directly. |

## Not verified

* Behaviour against the real Selling Partner API and the real Seller Central (only fakes built from the
  documentation and screenshots), and Amazon's Agent Policy wording (read from secondary sources).
* Automatic certificate issuance (no public DNS in the test environment).
* The optional browser image (`WITH_BROWSER=1`) and OpenTelemetry extras in a container.

## Reporting a vulnerability

Add the contact for your deployment here (a security mailbox or a private issue tracker) before exposing the
service to other people.
