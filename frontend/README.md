# Dashboard

React + TypeScript + Tailwind single-page app for the eligibility platform. It talks only to the
backend's `/api/v1` over the same origin (Vite proxy in development, Caddy in production), so there
is no CORS configuration anywhere.

```bash
npm install
API_URL=http://127.0.0.1:8000 npm run dev      # http://localhost:5173
npm run verify                                  # format, types, API contract, unit tests, build
```

## The API contract is generated, not hand-written

`openapi.json` is the backend's OpenAPI document and `src/api/schema.d.ts` is generated from it.
Every call goes through `openapi-fetch`, so a backend change that breaks the UI is a compile error.

```bash
python scripts/export_openapi.py     # from the repo root: rewrite openapi.json
npm run gen:api                      # regenerate src/api/schema.d.ts
```

A backend test (`test_contract.py`) fails if `openapi.json` is stale, and `npm run check:api` fails if
the generated types are.

## Tests

* `npm test`: Vitest + Testing Library (logic, forms, auth, export fallback, list syncing).
* `npm run e2e`: Playwright against the **real stack** (PostgreSQL, Redis, Dramatiq worker, API, and the
  production build of the UI), on the offline Amazon mock. It needs `E2E_DATABASE_URL` (the database
  name must contain `e2e`: it is dropped and recreated) and `E2E_REDIS_URL`. Set `CHROMIUM_PATH` to use a
  system Chromium.

```bash
E2E_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/eligibility_e2e \
E2E_REDIS_URL=redis://localhost:6379/5 npm run e2e
```

## Design decisions worth knowing

* The API key lives in `sessionStorage` (gone when the tab closes). It is validated against the server
  *before* it is stored, and signing out, or the server rejecting it, clears the query cache.
* The evidence form records **observations only**. It has no status field and does not prefill the ASIN:
  the server derives the status with the same rules engine that classified the API result.
* Job lists refetch when the job's progress changes (not on their own timer), so a table can never be
  left disagreeing with its header after the job finishes.
* Links that come from Amazon's responses are rendered only if they are `http(s)`.
