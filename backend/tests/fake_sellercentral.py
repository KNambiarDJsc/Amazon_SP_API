"""An offline stand-in for the Seller Central listing flow, modelled on the supplied screenshots.

Served through Playwright request interception: no network is used and any request to another
host is aborted and recorded. The page structure uses accessible roles and names (dialog,
searchbox, combobox, alert, progressbar) the way the real flow appears to; the *real* DOM must
still be calibrated (see browser/verifier.py), which this fixture cannot prove.

Every click on a sell/apply/copy control is counted in window.__actionClicks and every POST to
the submit endpoint is recorded, so tests can prove the adapter never pressed them.
"""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

HOST = "sellercentral.amazon.in"
LIMITATION = (
    "Used, Collectible, Refurbished conditions: "
    "This product has other listing limitations. Learn more"
)


@dataclass
class Product:
    asin: str
    title: str
    behavior: str = "ungated"
    delay_ms: int = 0


@dataclass
class FakeSellerCentral:
    products: dict[str, Product] = field(default_factory=dict)
    signed_out: bool = False  # serve a sign-in form
    redirect_to_signin: bool = False  # client-side redirect to /ap/signin
    captcha_on_load: bool = False
    captcha_after_searches: int | None = None  # the Nth search is answered with a robot check
    requests: list[tuple[str, str]] = field(default_factory=list)  # (method, path)
    user_agents: list[str] = field(default_factory=list)
    external_requests: list[str] = field(default_factory=list)
    submits: list[str] = field(default_factory=list)
    searches: int = 0

    def add(self, asin, title="Test product", behavior="ungated", delay_ms=0):
        self.products[asin] = Product(asin, title, behavior, delay_ms)
        return self

    # ---- playwright wiring -----------------------------------------------------------------
    def install(self, context) -> None:
        context.route("**/*", self._route)

    def _route(self, route) -> None:
        request = route.request
        url = urlparse(request.url)
        if url.hostname != HOST:
            self.external_requests.append(request.url)
            route.abort()
            return
        self.requests.append((request.method, url.path))
        self.user_agents.append(request.headers.get("user-agent", ""))
        path = url.path
        if request.method == "POST" and path == "/api/listing/submit":
            self.submits.append(path)
            route.fulfill(status=200, json={"submitted": True})
        elif path == "/product-search":
            if self.redirect_to_signin:
                # A 302 would be followed outside request interception, i.e. to the real network.
                route.fulfill(status=200, content_type="text/html", body=REDIRECT_HTML)
            elif self.captcha_on_load:
                route.fulfill(status=200, content_type="text/html", body=CAPTCHA_HTML)
            elif self.signed_out:
                route.fulfill(status=200, content_type="text/html", body=SIGNIN_HTML)
            else:
                route.fulfill(status=200, content_type="text/html", body=LISTING_HTML)
        elif path == "/ap/signin":
            route.fulfill(status=200, content_type="text/html", body=SIGNIN_HTML)
        elif path == "/captcha":
            route.fulfill(status=200, content_type="text/html", body=CAPTCHA_HTML)
        elif path == "/api/search":
            self.searches += 1
            if self.captcha_after_searches and self.searches >= self.captcha_after_searches:
                route.fulfill(status=200, json={"captcha": True})
                return
            asin = parse_qs(url.query).get("q", [""])[0].upper()
            hits = [self.products[asin]] if asin in self.products else []
            route.fulfill(
                status=200,
                json={"results": [{"asin": p.asin, "title": p.title} for p in hits]},
            )
        elif path == "/api/product":
            asin = parse_qs(url.query).get("asin", [""])[0].upper()
            p = self.products[asin]
            route.fulfill(
                status=200,
                json={
                    "asin": p.asin,
                    "title": p.title,
                    "behavior": p.behavior,
                    "delay": p.delay_ms,
                    "limitation": LIMITATION,
                },
            )
        else:
            route.fulfill(status=404, body="not found")

    # ---- assertions helpers ----------------------------------------------------------------
    def paths(self) -> list[str]:
        return [path for _, path in self.requests]


REDIRECT_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'></head><body>"
    "<script>location.replace('/ap/signin')</script></body></html>"
)

SIGNIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign in</title></head>
<body><main><h1>Sign in</h1>
<form action="/ap/signin" method="post"><label>Email or mobile number<input type="email" name="email"></label>
<label>Password<input type="password" name="password"></label><button type="submit">Sign in</button></form>
</main></body></html>"""

CAPTCHA_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Robot check</title></head>
<body><main><p>Sorry, we just need to make sure you're not a robot. For best results, please make sure
your browser is accepting cookies.</p><p>Enter the characters you see below</p>
<form action="/errors/validateCaptcha"><input name="field-keywords"><button>Continue shopping</button></form>
</main></body></html>"""

LISTING_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>List Your Products</title></head>
<body><main>
<h1>List Your Products</h1>
<label for="q">Search your catalogue or Amazon's catalogue for a listing (or a variation) to sell or copy.</label>
<input id="q" type="search" autocomplete="off">
<button id="go" type="button">Search</button>
<div id="spin" role="progressbar" aria-label="Loading" hidden>Loading</div>
<section id="results" role="region" aria-label="Search results" hidden>
  <ul id="list"></ul><p id="none" hidden>No results found</p></section>
<section id="panel" role="dialog" aria-label="Product information" hidden></section>
</main>
<script>
window.__actionClicks = [];
const $ = id => document.getElementById(id);
const spin = on => { $('spin').hidden = !on; };
const submit = () => fetch('/api/listing/submit', {method: 'POST'});

$('go').addEventListener('click', async () => {
  spin(true); $('results').hidden = true; $('panel').hidden = true;
  const r = await (await fetch('/api/search?q=' + encodeURIComponent($('q').value))).json();
  spin(false);
  if (r.captcha) { location.href = '/captcha'; return; }
  const list = $('list'); list.innerHTML = '';
  $('none').hidden = r.results.length > 0;
  for (const item of r.results) {
    const li = document.createElement('li'); li.tabIndex = 0;
    li.textContent = item.title + ' ASIN: ' + item.asin + ' Amazon Sales Rank: 23918 Lowest Price: 6,290.00';
    li.addEventListener('click', () => openPanel(item.asin));
    list.appendChild(li);
  }
  $('results').hidden = false;
});

async function openPanel(asin) {
  spin(true);
  const p = await (await fetch('/api/product?asin=' + asin)).json();
  if (p.behavior === 'never_loads') return;            // spinner never goes away
  setTimeout(() => { spin(false); render(p); }, p.delay || 0);
}

function render(p) {
  const shownAsin = p.behavior === 'wrong_asin' ? 'B0OTHER000' : p.asin;
  const panel = $('panel');
  const options = p.behavior === 'no_condition_option'
    ? ['Condition', 'Used - Good'] : ['Condition', 'New', 'Used - Good', 'Used - Acceptable'];
  const preselected = (p.behavior === 'gated' || p.behavior === 'category') ? 'New' : 'Condition';
  panel.innerHTML = `
    <button type="button" aria-label="Close" id="close">x</button>
    <h2>Product information</h2>
    <a href="#">${p.title}</a>
    <p>ASIN: ${shownAsin}</p><p>Amazon Sales Rank: 23918</p><p>Lowest Price: 6,290.00</p>
    ${p.behavior === 'unavailable' ? '<p>This product is currently unavailable.</p>' : ''}
    <label for="cond">Select a condition and sell</label>
    <select id="cond" aria-label="Condition">${options.map(o =>
      `<option ${o === preselected ? 'selected' : ''}>${o}</option>`).join('')}</select>
    <button type="button" id="copy">Copy listing</button>
    <button type="button" id="act" disabled>Sell this product</button>
    <div id="notices"></div>`;
  panel.hidden = false;
  const act = $('act'), notices = $('notices');
  for (const id of ['copy', 'act']) {
    $(id).addEventListener('click', () => { window.__actionClicks.push(id); submit(); });
  }
  const note = (role, text) => { const d = document.createElement('div'); d.setAttribute('role', role); d.textContent = text; notices.appendChild(d); };
  const refresh = () => {
    notices.innerHTML = '';
    const cond = $('cond').value;
    if (p.behavior === 'gated') {
      act.textContent = 'Apply to sell'; act.disabled = false;
      note('alert', 'You need approval to list in this brand.');
    } else if (p.behavior === 'category') {
      act.textContent = 'Apply to sell'; act.disabled = false;
      note('alert', 'You need approval to list in this category.');
    } else if (p.behavior === 'limited_new') {
      note('status', cond === 'New' ? 'New conditions: This product has other listing limitations.' : p.limitation);
      act.disabled = cond !== 'New';
    } else {
      note('status', p.limitation);                      // screenshot 1: about other conditions
      act.textContent = 'Sell this product'; act.disabled = cond !== 'New';
    }
  };
  $('cond').addEventListener('change', () => { spin(true); setTimeout(() => { spin(false); refresh(); }, 120); });
  refresh();
}
</script></body></html>"""
