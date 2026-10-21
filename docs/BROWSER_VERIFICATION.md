# Browser verification (optional, off by default)

**Short version:** you almost certainly do not need this. The core product answers "can this seller
list this ASIN?" through the Selling Partner API (`getListingsRestrictions`), which is Amazon's
supported route and needs no browser. The browser path exists for the remainder: ASINs the API
could not settle. Even there, a **person recording what they see** (see *Human-assisted
verification*) is the recommended route, and it needs nothing in this document.

## What Amazon's policy says, and what I could and could not verify

* Reporting from several seller-tooling sites says Amazon updated its Business Solutions Agreement
  on **4 March 2026**, adding an **Agent Policy (Section 19)**: automated systems must **identify
  themselves as automated**, **follow the policy at all times**, and **stop when Amazon asks**.
  Commentary says browser automation is in scope and that tools which *mimic human interaction to
  extract data Amazon does not expose through SP-API* are in "considerably riskier" territory.
* **I could not read the primary text.** It is published in Seller Central Help under *Program
  Policies*. Amazon has not, according to that coverage, specified a technical self-identification
  mechanism. **You, the account holder, are responsible for reading it and for deciding whether this
  use is permitted for your account.** Nothing here is legal advice.
* This product's data *is* exposed through SP-API, which is the reason the browser path is a
  fallback and not the engine.

## What the tool does to stay inside that policy

| Obligation / risk | Safeguard |
|---|---|
| Must identify as automated | The browser's `User-Agent` ends with `AmazonSellerEligibilityChecker/0.1 (automated agent; operator: <your contact>)`. `BROWSER_AGENT_CONTACT` is **required**; without it the tool refuses to run. No stealth patches, no hidden automation flags (a test asserts `navigator.webdriver` is left visible). |
| Must be on purpose | Off unless `BROWSER_AUTOMATION_ENABLED=true`, **and** the seller recorded the acknowledgement (`browser-acknowledge --i-confirm`, versioned: a wording change asks again). Started explicitly per seller and job; never scheduled or triggered by the queue. |
| Must stop when asked | `python -m app.cli browser-forget` deletes the saved session; unsetting the flag disables the feature. |
| No evasion | Stops **immediately** on any sign-in page, CAPTCHA, robot check or verification prompt (noticed the moment it appears, not after a timeout; a test enforces this), records a `BROWSER_SESSION_STOPPED` audit event, and never touches the prompt. A person completes it. |
| Passwords | Never seen or stored. A person signs in themselves in a visible browser (`browser-login`); only the resulting session is saved, **encrypted** and bound to the seller. |
| No side effects | The page wrapper (`browser/safe_page.py`) can navigate within one host, type in the search box, press *Search*, open the result for the requested ASIN, and choose a condition in the dropdown. It **cannot** press *Sell this product*, *Apply to sell*, *Copy listing* or anything that lists, offers or requests approval. Dialogs are dismissed; downloads are off. Tests prove zero presses/submits and scan the source for any other way to click. |
| Human pace | One page at a time, one session per seller (PostgreSQL advisory lock), a jittered pause (default 4 s) between ASINs, bounded attempts (2 per page, 3 per check overall), explicit waits (no fixed sleeps). |
| Data minimisation | A screenshot of the *product panel only*, never the whole page, can be disabled (`BROWSER_SCREENSHOTS=false`). It is stored locally under `EVIDENCE_DIR` with its SHA-256 in the evidence record. |

## What it can conclude

The adapter reads **screen state** (ASIN shown, condition selected, the primary button and whether
it is enabled, every notice) and the same deterministic rules used for API evidence derive the
status. The trap in the original brief (*"no approval message therefore ungated"*) is explicitly
refused: in your first screenshot the product shows no approval message **but also** no chosen
condition and a disabled *Sell this product* button, which proves nothing. `VERIFIED_LISTABLE`
needs the right product on screen, the target condition selected, an **enabled** *Sell this
product*, and no applicable notice. Anything unfamiliar is `NEEDS_MANUAL_CHECK`.

A browser (or person) result is combined with the API result: an ASIN the API did not know can be
resolved by the screen; but if the sources contradict each other the check stays unresolved
(`SOURCE_CONFLICT`) for a person.

## Setting it up

1. Read the Agent Policy in Seller Central Help. Decide whether your use is permitted.
2. `pip install '.[browser]'`, then set in `.env`: `BROWSER_AUTOMATION_ENABLED=true`,
   `BROWSER_AGENT_CONTACT=you@example.com`. (In locked-down environments set
   `BROWSER_EXECUTABLE_PATH` to an installed Chromium.)
3. `python -m app.cli browser-acknowledge --seller-account <id> --i-confirm`
4. On a machine with a display: `python -m app.cli browser-login --seller-account <id>`, sign in
   yourself (including any verification), press Enter. (It needs the same database and encryption
   key; it cannot sign in on a headless server, by design.)
5. `python -m app.cli browser-verify --seller-account <id> --job <job> --limit 10 --headed`
   Start with a handful and watch.

Exit code 2 means a sign-in or challenge appeared: complete it yourself, then re-run.

## Calibrate before trusting it (important)

The locators in `browser/verifier.py` were written from **screenshots** and verified only against an
offline fake that I also wrote from those screenshots. They use accessible roles and names (dialog
"Product information", searchbox, combobox "Condition", alert/status notices, buttons *Sell this
product* / *Apply to sell*), all collected at the top of the file. **I could not inspect the real
Seller Central DOM**, so the real page may differ: the listing URL (`BROWSER_LISTING_URL`),
the control names, the condition labels other than "New" (`domain/conditions.py`), and how the sell
control enables after choosing a condition.

The failure mode is designed to be safe: when the page is not what the adapter expects, it finds
*less*, and less evidence can only produce `NEEDS_MANUAL_CHECK`, never a false "listable". Run a few
ASINs with `--headed` and compare against what you see before relying on any result.

## Human-assisted verification (recommended)

`GET /api/v1/jobs/{id}/review-queue` lists what still needs a person, with instructions.
For each ASIN, look in Seller Central yourself and record **what you saw**:

```bash
curl -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  localhost:8000/api/v1/jobs/$JOB/checks/$CHECK/manual-evidence -d '{
    "page_asin": "B08PDL14VR", "selected_condition": "new_new",
    "action_label": "Sell this product", "action_enabled": true,
    "notices": ["Used, Collectible, Refurbished conditions: This product has other listing limitations. Learn more"],
    "note": "checked 14:05"}'
```

You cannot send a status; unknown fields are rejected. The rules derive it, label it `MANUAL`,
append your entry to the evidence trail (corrections supersede earlier entries; nothing is
deleted), refresh the brand summary and audit the change. `POST .../recheck` asks the API again;
`POST .../brands/{id}/retry` re-runs a dead-lettered brand or searches again with `rediscover`.
