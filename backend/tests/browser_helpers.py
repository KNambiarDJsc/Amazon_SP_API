"""Shared setup for browser tests. They drive a real Chromium against an offline fake site.

The browser is launched with every route to the network cut: no proxy, and a resolver that fails
every lookup. Combined with request interception, nothing a test does can reach Amazon.
"""

import glob
import os
from contextlib import contextmanager

from app.browser.factory import PlaywrightFactory
from app.browser.safe_page import SafePage
from app.browser.verifier import SellerCentralVerifier
from app.config import Settings

ISOLATE = "--no-sandbox,--no-proxy-server,--host-resolver-rules=MAP * ~NOTFOUND"


def find_chromium() -> str | None:
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None


CHROME = find_chromium()
try:
    import playwright  # noqa: F401

    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False


def cfg(**overrides) -> Settings:
    base = dict(
        database_url="x",
        browser_automation_enabled=True,
        browser_agent_contact="ops@example.test",
        browser_executable_path=CHROME or "",
        browser_launch_args=ISOLATE,
        browser_timeout_ms=3000,
        browser_max_attempts=2,
        browser_min_interval_seconds=4.0,
        browser_screenshots=True,
    )
    return Settings(**{**base, **overrides})


@contextmanager
def open_browser(site, **overrides):
    """A browser whose every request must be answered by the fake. A request that bypasses the
    interception (a redirect target, a pre-connect) cannot leave the machine, but it is a bug in
    the test setup, so it fails the test."""
    config = cfg(**overrides)
    leaks: list[str] = []

    def watch(context):
        site.install(context)
        context.on(
            "requestfailed",
            lambda r: leaks.append(r.url) if "ERR_NAME_NOT_RESOLVED" in (r.failure or "") else None,
        )

    with PlaywrightFactory(config, on_context=watch).open() as handle:
        yield handle, config
    assert not leaks, f"requests escaped the fake site: {leaks}"


@contextmanager
def verifier_for(site, **overrides):
    with open_browser(site, **overrides) as (handle, config):
        safe = SafePage(
            handle.page,
            allowed_host="sellercentral.amazon.in",
            timeout_ms=config.browser_timeout_ms,
        )
        yield (
            SellerCentralVerifier(
                safe,
                listing_url=config.browser_listing_url,
                max_attempts=config.browser_max_attempts,
                screenshots=config.browser_screenshots,
            ),
            handle,
            safe,
        )


def make_factory(site, config):
    """A production PlaywrightFactory whose context is wired to the fake site, plus the list that
    collects any request that escaped interception (must stay empty)."""
    leaks: list[str] = []

    def watch(context):
        site.install(context)
        context.on(
            "requestfailed",
            lambda r: leaks.append(r.url) if "ERR_NAME_NOT_RESOLVED" in (r.failure or "") else None,
        )

    return PlaywrightFactory(config, on_context=watch), leaks
