"""Policy gate, evidence recording, stopping on prompts, locking, and session protection."""

import hashlib
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from browser_helpers import CHROME, HAVE_PLAYWRIGHT, cfg, make_factory
from fake_sellercentral import FakeSellerCentral
from sqlalchemy import select

from app.amazon.mock import MockAmazonClient, catalog_item, demo_client
from app.browser.policy import (
    ACK_TEXT,
    ACK_VERSION,
    BrowserNotPermitted,
    agent_token,
    ensure_permitted,
)
from app.browser.runner import (
    BrowserBusy,
    login_interactively,
    run_browser_verification,
    seller_browser_lock,
)
from app.browser.session_store import delete_state, load_state, save_state
from app.db import session_scope
from app.enums import EligibilityStatus as S
from app.models import AuditEvent, BrowserSession, CheckObservation, SellerAccount
from app.services.credentials import CredentialCipher, generate_key
from app.services.jobs import JobNotFound

needs_browser = pytest.mark.skipif(
    not (HAVE_PLAYWRIGHT and CHROME), reason="needs Playwright and a Chromium executable"
)
browser = pytest.mark.browser
ASINS = {"B0UNGATED1": "ungated", "B0GATED001": "gated", "B0LIMITED1": "limited_new"}


@pytest.fixture
def cipher():
    return CredentialCipher([generate_key()])


@pytest.fixture
def site():
    s = FakeSellerCentral()
    for asin, behavior in ASINS.items():
        s.add(asin, f"Oddy {behavior} product", behavior)
    return s


@pytest.fixture
def ready(seller, cipher):
    """The seller has acknowledged the policy and signed in (a saved session)."""
    with session_scope() as s:
        row = s.get(SellerAccount, seller.id)
        row.browser_ack_at, row.browser_ack_version = datetime.now(UTC), ACK_VERSION
        save_state(s, cipher, seller.id, {"cookies": [], "origins": []})
    return seller


@pytest.fixture
def job(make_harness, ready):
    """A job whose three ASINs the API could not settle, so all need a person (or the browser)."""
    items = [catalog_item(a, f"Oddy {b}", "Oddy", category_id="c") for a, b in ASINS.items()]
    h = make_harness(MockAmazonClient(catalog={"Oddy": items}))
    job_id = h.submit(["Oddy"])
    h.drain()
    assert set(h.statuses(job_id).values()) == {"NEEDS_MANUAL_CHECK"}
    return h, job_id


def run(seller, job_id, site, cipher, tmp_path, *, sleeps=None, **overrides):
    config = cfg(evidence_dir=str(tmp_path), **overrides)
    factory, leaks = make_factory(site, config)
    sleeps = sleeps if sleeps is not None else []
    stats = run_browser_verification(
        seller_id=seller.id,
        job_id=job_id,
        cfg=config,
        cipher=cipher,
        factory=factory,
        sleep=sleeps.append,
        jitter=lambda: 0.5,
    )
    assert not leaks, f"requests escaped the fake site: {leaks}"
    return stats, config


# ---- the policy gate (no browser is ever started) --------------------------------------------


class NoBrowser:
    @contextmanager
    def open(self, *_a, **_k):
        raise AssertionError("a browser was launched although the policy gate should have refused")
        yield


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        (dict(browser_automation_enabled=False), "BROWSER_AUTOMATION_ENABLED"),
        (dict(browser_agent_contact=""), "BROWSER_AGENT_CONTACT"),
        (dict(browser_agent_contact="   "), "BROWSER_AGENT_CONTACT"),
    ],
)
def test_the_operator_must_enable_and_identify_themselves(job, cipher, overrides, needle):
    h, job_id = job
    with pytest.raises(BrowserNotPermitted, match=needle):
        run_browser_verification(
            seller_id=h.seller.id,
            job_id=job_id,
            cfg=cfg(**overrides),
            cipher=cipher,
            factory=NoBrowser(),
        )


def test_the_seller_must_have_acknowledged_the_current_terms(make_harness, seller, cipher):
    config = cfg()
    with pytest.raises(BrowserNotPermitted, match="has not acknowledged"):
        ensure_permitted(config, seller)  # never acknowledged
    with session_scope() as s:
        row = s.get(SellerAccount, seller.id)
        row.browser_ack_at, row.browser_ack_version = datetime.now(UTC), "2020-01-01"
        stale = s.get(SellerAccount, seller.id)
    with pytest.raises(BrowserNotPermitted, match="has not acknowledged"):
        ensure_permitted(config, stale)  # acknowledged an older wording: ask again
    with session_scope() as s:
        s.get(SellerAccount, seller.id).browser_ack_version = ACK_VERSION
        ensure_permitted(config, s.get(SellerAccount, seller.id))


def test_without_a_saved_session_it_tells_a_person_to_sign_in(make_harness, seller, cipher):
    with session_scope() as s:
        row = s.get(SellerAccount, seller.id)
        row.browser_ack_at, row.browser_ack_version = datetime.now(UTC), ACK_VERSION
    h = make_harness(demo_client())
    job_id = h.submit(["Havells"])
    h.drain()
    with pytest.raises(BrowserNotPermitted, match="browser-login"):
        run_browser_verification(
            seller_id=seller.id, job_id=job_id, cfg=cfg(), cipher=cipher, factory=NoBrowser()
        )


def test_the_acknowledgement_text_states_the_obligations():
    text = ACK_TEXT
    for phrase in ("Agent Policy", "automated agent", "CAPTCHA", "SP-API already provides"):
        assert phrase in text


def test_another_sellers_job_is_not_reachable(job, cipher, make_harness):
    h, job_id = job
    with session_scope() as s:
        other = SellerAccount(
            id=uuid.uuid4(),
            name="O",
            api_key_hash="b" * 64,
            api_key_prefix="o",
            browser_ack_at=datetime.now(UTC),
            browser_ack_version=ACK_VERSION,
        )
        s.add(other)
        s.flush()
        save_state(s, cipher, other.id, {"cookies": [], "origins": []})
    with pytest.raises(JobNotFound):
        run_browser_verification(
            seller_id=other.id, job_id=job_id, cfg=cfg(), cipher=cipher, factory=NoBrowser()
        )


# ---- a normal run ----------------------------------------------------------------------------


@needs_browser
@browser
def test_a_run_resolves_what_it_can_and_records_evidence(job, site, cipher, tmp_path):
    h, job_id = job
    sleeps = []
    stats, config = run(h.seller, job_id, site, cipher, tmp_path, sleeps=sleeps)

    assert (stats.considered, stats.resolved, stats.unresolved, stats.failed, stats.stopped) == (
        3,
        2,
        1,
        0,
        None,
    )
    assert h.statuses(job_id) == {
        "B0GATED001/new_new": "BRAND_APPROVAL_REQUIRED",
        "B0LIMITED1/new_new": "NEEDS_MANUAL_CHECK",  # an applicable limitation: a person decides
        "B0UNGATED1/new_new": "VERIFIED_LISTABLE",
    }
    by_asin = {c.asin: c for c in h.checks(job_id)}
    assert by_asin["B0UNGATED1"].verification_methods == ["BROWSER"]
    assert by_asin["B0LIMITED1"].rule_id == "BROWSER_LISTING_LIMITATION"

    # nothing was ever pressed, submitted, or fetched from elsewhere
    assert site.submits == [] and site.external_requests == []
    assert all(token in ua for ua in site.user_agents for token in [agent_token(config)])

    # evidence: raw observation + a screenshot of the panel whose hash was recorded
    with session_scope() as s:
        obs = [o for o in s.scalars(select(CheckObservation)) if o.source.value == "BROWSER"]
    assert len(obs) == 3
    for o in obs:
        assert o.raw["observer"] == f"browser-agent:{agent_token(config)}"
        assert o.raw["url_path"] == "product-search"
        path = tmp_path / o.evidence_ref.removeprefix("file:")
        assert path.read_bytes().startswith(b"\x89PNG")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == o.raw["screenshot_sha256"]
        assert o.signals["page_asin"] in ASINS and o.context["brand_validated"] is True

    # the brand summary followed, and the audit trail shows the run
    brand = h.brand(job_id, "Oddy")
    assert brand.brand_status == S.MIXED_RESULTS
    types = [e.event_type for e in h.events(job_id)]
    assert types.count("BROWSER_RUN_STARTED") == 1 and types.count("BROWSER_EVIDENCE_ADDED") == 3
    # a person's pace: a jittered pause between pages, none before the first
    assert sleeps == [4.0, 4.0]


@needs_browser
@browser
def test_only_unresolved_checks_are_visited(make_harness, ready, site, cipher, tmp_path):
    client = demo_client()  # Havells etc. are settled by the API
    client.catalog["oddy"] = [catalog_item("B0UNGATED1", "x", "Oddy", category_id="c")]
    h = make_harness(client)
    job_id = h.submit(["Havells", "Oddy"])
    h.drain()
    stats, _ = run(h.seller, job_id, site, cipher, tmp_path)
    assert stats.considered == 1  # Havells' ASINs were never touched
    assert site.searches == 1


@needs_browser
@browser
def test_a_repeat_run_skips_what_the_browser_already_answered(job, site, cipher, tmp_path):
    h, job_id = job
    run(h.seller, job_id, site, cipher, tmp_path)
    site.requests.clear()
    stats, _ = run(h.seller, job_id, site, cipher, tmp_path)
    # the two settled ones left the queue; the limitation one is *answered* ("a person decides"),
    # so looking at the same page again cannot change anything
    assert stats.considered == 0 and site.requests == []


@needs_browser
@browser
def test_a_page_that_never_loads_is_retried_a_bounded_number_of_times_across_runs(
    make_harness, ready, cipher, tmp_path
):
    site = FakeSellerCentral().add("B0STUCK001", "stuck", "never_loads")
    h = make_harness(MockAmazonClient(catalog={"Oddy": [catalog_item("B0STUCK001", "x", "Oddy")]}))
    job_id = h.submit(["Oddy"])
    h.drain()
    counts = []
    for _ in range(5):
        stats, _ = run(
            h.seller, job_id, site, cipher, tmp_path, browser_timeout_ms=600, browser_max_attempts=1
        )
        counts.append((stats.considered, stats.failed))
    assert counts == [
        (1, 1),
        (1, 1),
        (1, 1),
        (0, 0),
        (0, 0),
    ]  # three tries, then it is left to a person
    with session_scope() as s:
        outcomes = [
            o.outcome.value
            for o in s.scalars(select(CheckObservation))
            if o.source.value == "BROWSER"
        ]
    assert outcomes == ["TIMEOUT"] * 3
    assert h.statuses(job_id) == {"B0STUCK001/new_new": "NEEDS_MANUAL_CHECK"}  # never "listable"


# ---- a person has to take over ---------------------------------------------------------------


@needs_browser
@browser
def test_a_captcha_stops_the_whole_run_and_the_session_is_left_untouched(
    job, site, cipher, tmp_path
):
    h, job_id = job
    with session_scope() as s:
        before = bytes(s.get(BrowserSession, h.seller.id).state_encrypted)
    site.captcha_after_searches = 2  # the 2nd search is met with a robot check
    stats, _ = run(h.seller, job_id, site, cipher, tmp_path)

    assert stats.stopped == "challenge" and stats.considered == 2
    assert site.searches == 2  # the 3rd ASIN was never even searched for
    statuses = h.statuses(job_id)
    assert list(statuses.values()).count("NEEDS_MANUAL_CHECK") == 2  # the blocked one + untouched
    with session_scope() as s:
        obs = [o for o in s.scalars(select(CheckObservation)) if o.source.value == "BROWSER"]
        assert bytes(s.get(BrowserSession, h.seller.id).state_encrypted) == before  # not rewritten
    assert len(obs) == 2
    blocked = [o for o in obs if o.signals["challenge_detected"]]
    assert len(blocked) == 1 and blocked[0].signals["action_label"] is None
    stop = [e for e in h.events(job_id) if e.event_type == "BROWSER_SESSION_STOPPED"]
    assert len(stop) == 1 and stop[0].payload["reason"] == "challenge"
    assert "did not interact" in stop[0].payload["detail"]


@needs_browser
@browser
def test_a_signed_out_session_stops_the_run_and_asks_for_a_new_sign_in(job, site, cipher, tmp_path):
    h, job_id = job
    site.signed_out = True
    stats, _ = run(h.seller, job_id, site, cipher, tmp_path)
    assert stats.stopped == "signed_out" and stats.considered == 1
    with session_scope() as s:
        (obs,) = [o for o in s.scalars(select(CheckObservation)) if o.source.value == "BROWSER"]
    assert obs.outcome.value == "SESSION_EXPIRED"
    assert set(h.statuses(job_id).values()) == {"NEEDS_MANUAL_CHECK"}  # nothing invented


@needs_browser
@browser
def test_a_clean_run_refreshes_the_saved_session(job, site, cipher, tmp_path):
    h, job_id = job
    with session_scope() as s:
        before = bytes(s.get(BrowserSession, h.seller.id).state_encrypted)
    run(h.seller, job_id, site, cipher, tmp_path)
    with session_scope() as s:
        after = bytes(s.get(BrowserSession, h.seller.id).state_encrypted)
        assert load_state(s, cipher, h.seller.id) is not None
    assert after != before  # re-encrypted with fresh state


def test_only_one_browser_session_per_seller_at_a_time(job, cipher):
    h, job_id = job
    with seller_browser_lock(h.seller.id):
        with pytest.raises(BrowserBusy):
            run_browser_verification(
                seller_id=h.seller.id, job_id=job_id, cfg=cfg(), cipher=cipher, factory=NoBrowser()
            )
        with pytest.raises(BrowserBusy):
            with seller_browser_lock(h.seller.id):
                pass
    with seller_browser_lock(h.seller.id):  # released afterwards
        pass


# ---- signing in is a person's job ------------------------------------------------------------


class _ForbiddenPage:
    """Fails on any interaction other than opening the page."""

    def __init__(self):
        self.visited = []

    def goto(self, url):
        self.visited.append(url)

    def __getattr__(self, name):
        raise AssertionError(f"login must not call page.{name}(): signing in is for a person")


class _FakeFactory:
    def __init__(self):
        self.page = _ForbiddenPage()
        self.opened_with = "unset"

    @contextmanager
    def open(self, state=None):
        self.opened_with = state
        factory = self

        class Handle:
            page = factory.page

            @staticmethod
            def state():
                return {
                    "cookies": [{"name": "session-id", "value": "SECRET-COOKIE"}],
                    "origins": [],
                }

        yield Handle()


def test_login_opens_the_page_waits_for_a_person_and_saves_the_session_encrypted(seller, cipher):
    with session_scope() as s:
        row = s.get(SellerAccount, seller.id)
        row.browser_ack_at, row.browser_ack_version = datetime.now(UTC), ACK_VERSION
    factory, waited = _FakeFactory(), []
    login_interactively(
        seller_id=seller.id,
        cfg=cfg(),
        cipher=cipher,
        factory=factory,
        wait_for_person=lambda handle: waited.append(handle),
    )
    assert factory.page.visited == [cfg().browser_listing_url] and len(waited) == 1
    with session_scope() as s:
        raw = bytes(s.get(BrowserSession, seller.id).state_encrypted)
        assert b"SECRET-COOKIE" not in raw and b"session-id" not in raw  # cookies encrypted
        assert load_state(s, cipher, seller.id)["cookies"][0]["value"] == "SECRET-COOKIE"
        assert [e.event_type for e in s.scalars(select(AuditEvent))] == ["BROWSER_SESSION_SAVED"]


def test_login_still_requires_the_policy_gate(seller, cipher):
    with pytest.raises(BrowserNotPermitted):
        login_interactively(
            seller_id=seller.id,
            cfg=cfg(),
            cipher=cipher,
            factory=_FakeFactory(),
            wait_for_person=lambda h: None,
        )


# ---- the session store -----------------------------------------------------------------------


def test_a_session_is_bound_to_its_seller_and_unreadable_without_the_key(session, seller, cipher):
    state = {"cookies": [{"name": "at-main", "value": "TOP-SECRET"}], "origins": []}
    save_state(session, cipher, seller.id, state)
    session.commit()
    assert load_state(session, cipher, seller.id) == state
    assert load_state(session, CredentialCipher([generate_key()]), seller.id) is None  # wrong key

    other = SellerAccount(id=uuid.uuid4(), name="O", api_key_hash="a" * 64, api_key_prefix="o")
    session.add(other)
    session.flush()  # the seller row must exist before a row that references it
    session.add(
        BrowserSession(
            seller_account_id=other.id,
            state_encrypted=session.get(BrowserSession, seller.id).state_encrypted,
        )
    )
    session.commit()
    assert load_state(session, cipher, other.id) is None  # a blob moved to another seller's row

    assert b"TOP-SECRET" not in bytes(session.get(BrowserSession, seller.id).state_encrypted)
    assert delete_state(session, seller.id) is True and delete_state(session, seller.id) is False


def test_the_acknowledgement_cli_records_consent_only_when_explicitly_confirmed(seller, capsys):
    from app.cli import main

    assert main(["browser-acknowledge", "--seller-account", str(seller.id)]) == 1
    assert "Agent Policy" in capsys.readouterr().out  # the terms are always shown
    with session_scope() as s:
        assert s.get(SellerAccount, seller.id).browser_ack_at is None

    assert main(["browser-acknowledge", "--seller-account", str(seller.id), "--i-confirm"]) == 0
    with session_scope() as s:
        row = s.get(SellerAccount, seller.id)
        assert row.browser_ack_version == ACK_VERSION and row.browser_ack_at is not None
        assert [e.event_type for e in s.scalars(select(AuditEvent))] == [
            "BROWSER_POLICY_ACKNOWLEDGED"
        ]


def test_forget_deletes_the_saved_session_and_audits_it(ready, cipher, capsys):
    from app.cli import main

    with session_scope() as s:
        assert load_state(s, cipher, ready.id) is not None
    assert main(["browser-forget", "--seller-account", str(ready.id)]) == 0
    assert "deleted" in capsys.readouterr().out
    with session_scope() as s:
        assert load_state(s, cipher, ready.id) is None
        assert "BROWSER_SESSION_DELETED" in [e.event_type for e in s.scalars(select(AuditEvent))]
    assert main(["browser-forget", "--seller-account", str(ready.id)]) == 0  # idempotent
    assert "no saved session" in capsys.readouterr().out
