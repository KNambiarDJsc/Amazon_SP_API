"""The Seller Central adapter against a real Chromium and an offline fake of the site."""

import re
import time
from pathlib import Path

import pytest
from browser_helpers import CHROME, HAVE_PLAYWRIGHT, open_browser, verifier_for
from fake_sellercentral import LIMITATION, FakeSellerCentral

from app.domain.classification import classify_observation
from app.domain.observations import Observation, ObservationContext
from app.enums import EligibilityStatus as S
from app.enums import ObservationOutcome, ObservationSource

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not (HAVE_PLAYWRIGHT and CHROME), reason="needs Playwright and a Chromium executable"
    ),
]

if HAVE_PLAYWRIGHT:
    from app.browser.safe_page import ForbiddenAction
    from app.browser.verifier import HumanRequired


def status_of(asin, signals, condition="new_new", validated=True):
    ctx = ObservationContext(
        asin=asin,
        marketplace_id="A21TJRUUN4KGV",
        condition=condition,
        seller_ref="A1",
        expected_brand="x",
        brand_validated=validated,
    )
    return classify_observation(
        Observation(source=ObservationSource.BROWSER, context=ctx, browser=signals)
    )


def never_pressed(handle, site):
    assert handle.page.evaluate("window.__actionClicks") == []
    assert site.submits == []


# ---- what it reads, against the behaviours in the screenshots --------------------------------


def test_screenshot_1_flow_selects_the_condition_and_finds_sell_enabled():
    site = FakeSellerCentral().add("B08PDL14VR", "ESSL IDENTIX K30 PRO", "ungated")
    with verifier_for(site) as (v, handle, _):
        result = v.verify("B08PDL14VR", "new_new")
        assert result.outcome == ObservationOutcome.OK
        s = result.signals
        assert (s.page_asin, s.selected_condition) == ("B08PDL14VR", "new_new")
        assert s.notices == [LIMITATION]
        assert (s.action_label, s.action_enabled) == ("Sell this product", True)
        verdict = status_of("B08PDL14VR", s)
        assert verdict.status == S.VERIFIED_LISTABLE and verdict.rule_id == "BROWSER_SELL_ENABLED"
        never_pressed(handle, site)


def test_screenshot_2_flow_reads_the_approval_message_and_apply_button():
    site = FakeSellerCentral().add("B08Y9C5H38", "Havells Hair Dryer", "gated")
    with verifier_for(site) as (v, handle, _):
        s = v.verify("B08Y9C5H38", "new_new").signals
        assert s.notices == ["You need approval to list in this brand."]
        assert (s.action_label, s.action_enabled) == ("Apply to sell", True)
        assert status_of("B08Y9C5H38", s).status == S.BRAND_APPROVAL_REQUIRED
        never_pressed(handle, site)  # "Apply to sell" was visible, enabled, and left alone


def test_a_category_approval_is_not_mistaken_for_a_brand_approval():
    site = FakeSellerCentral().add("B0CATEGORY", "Tool", "category")
    with verifier_for(site) as (v, handle, _):
        s = v.verify("B0CATEGORY", "new_new").signals
        assert status_of("B0CATEGORY", s).status == S.OTHER_APPROVAL_REQUIRED
        never_pressed(handle, site)


def test_a_limitation_that_applies_to_the_condition_is_not_listable():
    site = FakeSellerCentral().add("B0LIMITED1", "Thing", "limited_new")
    with verifier_for(site) as (v, _, _):
        s = v.verify("B0LIMITED1", "new_new").signals
        verdict = status_of("B0LIMITED1", s)
        assert (
            verdict.status == S.NEEDS_MANUAL_CHECK
            and verdict.rule_id == "BROWSER_LISTING_LIMITATION"
        )


def test_a_missing_condition_option_leaves_the_condition_unselected():
    site = FakeSellerCentral().add("B0NOCOND01", "Thing", "no_condition_option")
    with verifier_for(site) as (v, _, _):
        s = v.verify("B0NOCOND01", "new_new").signals
        assert s.selected_condition is None
        assert status_of("B0NOCOND01", s).rule_id == "BROWSER_CONDITION_NOT_SELECTED"


def test_the_panel_showing_a_different_asin_is_not_evidence_for_this_one():
    site = FakeSellerCentral().add("B0WRONG001", "Thing", "wrong_asin")
    with verifier_for(site) as (v, _, _):
        s = v.verify("B0WRONG001", "new_new").signals
        assert s.page_asin == "B0OTHER000"
        assert status_of("B0WRONG001", s).rule_id == "BROWSER_ASIN_MISMATCH"


def test_an_unavailable_product_and_an_asin_with_no_results():
    site = FakeSellerCentral().add("B0GONE0001", "Thing", "unavailable")
    with verifier_for(site) as (v, _, _):
        s = v.verify("B0GONE0001", "new_new").signals
        assert s.product_unavailable and s.page_asin == "B0GONE0001"
        assert v.verify("B0NOTHERE1", "new_new").signals.product_unavailable  # no search hit


# ---- waiting: explicit conditions, bounded ---------------------------------------------------


def test_a_slow_panel_with_a_spinner_is_waited_for_not_guessed_at():
    site = FakeSellerCentral().add("B0SLOW0001", "Thing", "ungated", delay_ms=1200)
    with verifier_for(site, browser_timeout_ms=5000) as (v, _, _):
        start = time.monotonic()
        result = v.verify("B0SLOW0001", "new_new")
        elapsed = time.monotonic() - start
    assert result.signals.action_enabled is True
    assert elapsed >= 1.0  # it really did wait for the delayed render ...
    assert elapsed < 4.5  # ... and not for the full timeout


def test_a_page_that_never_finishes_loading_times_out_after_bounded_attempts():
    site = FakeSellerCentral().add("B0STUCK001", "Thing", "never_loads")
    with verifier_for(site, browser_timeout_ms=700, browser_max_attempts=2) as (v, _, _):
        start = time.monotonic()
        result = v.verify("B0STUCK001", "new_new")
        elapsed = time.monotonic() - start
    assert result.outcome == ObservationOutcome.TIMEOUT and result.signals is None
    assert "gave up after 2 attempts" in result.error_detail
    assert elapsed < 6  # two bounded attempts, not an endless wait
    assert site.searches == 2  # each attempt started over rather than hanging


# ---- people take over: sign-in and challenges stop everything --------------------------------


def test_a_sign_in_page_stops_the_agent_before_it_types_anything():
    site = FakeSellerCentral(signed_out=True).add("B08PDL14VR")
    with verifier_for(site, browser_timeout_ms=10_000) as (v, _, _):
        start = time.monotonic()
        with pytest.raises(HumanRequired) as info:
            v.verify("B08PDL14VR", "new_new")
        assert time.monotonic() - start < 5  # immediately, not after a timeout
    assert info.value.kind == "signed_out"
    assert site.paths() == ["/product-search"]  # it looked at the page and nothing else


def test_a_redirect_to_the_sign_in_url_is_recognised():
    site = FakeSellerCentral(redirect_to_signin=True).add("B08PDL14VR")
    with verifier_for(site) as (v, _, _):
        with pytest.raises(HumanRequired) as info:
            v.verify("B08PDL14VR", "new_new")
    assert info.value.kind == "signed_out" and "/api/search" not in site.paths()


def test_a_captcha_on_load_stops_the_agent_and_it_does_not_touch_the_form():
    site = FakeSellerCentral(captcha_on_load=True).add("B08PDL14VR")
    with verifier_for(site) as (v, handle, _):
        with pytest.raises(HumanRequired) as info:
            v.verify("B08PDL14VR", "new_new")
        assert handle.page.locator("input[name='field-keywords']").input_value() == ""  # untouched
    assert info.value.kind == "challenge" and site.paths() == ["/product-search"]


def test_a_captcha_appearing_after_a_search_stops_the_agent_there():
    site = FakeSellerCentral(captcha_after_searches=1).add("B08PDL14VR")
    with verifier_for(site, browser_timeout_ms=10_000) as (v, _, _):
        start = time.monotonic()
        with pytest.raises(HumanRequired) as info:
            v.verify("B08PDL14VR", "new_new")
        elapsed = time.monotonic() - start
    # Noticed the moment it appeared, not after waiting out the 10 s timeout for results.
    assert elapsed < 5, elapsed
    assert info.value.kind == "challenge"
    assert "/api/product" not in site.paths()  # no further interaction after the prompt
    assert site.paths().count("/captcha") == 1


# ---- it never leaves the one host, never presses a listing control ---------------------------


def test_navigation_outside_seller_central_is_refused():
    site = FakeSellerCentral()
    with verifier_for(site) as (_, _, safe):
        for url in (
            "https://evil.example/",
            "http://sellercentral.amazon.in/product-search",
            "https://sellercentral.amazon.com/",
        ):
            with pytest.raises(ForbiddenAction):
                safe.goto(url)
    assert site.requests == [] and site.external_requests == []


@pytest.mark.parametrize(
    ("control", "behavior"),
    [("Sell this product", "ungated"), ("Apply to sell", "gated"), ("Copy listing", "gated")],
)
def test_the_safe_page_refuses_to_press_listing_controls_even_if_asked(control, behavior):
    site = FakeSellerCentral().add("B08PDL14VR", "ESSL", behavior)
    with verifier_for(site) as (v, handle, safe):
        v.verify("B08PDL14VR", "new_new")
        button = safe.by_role("button", name=re.compile(f"^{control}$", re.I)).first
        with pytest.raises(ForbiddenAction):
            safe.press_search(button)
        never_pressed(handle, site)


def test_the_safe_page_refuses_to_open_a_result_for_a_different_asin():
    site = FakeSellerCentral().add("B08PDL14VR", "ESSL")
    with verifier_for(site) as (v, handle, safe):
        safe.goto("https://sellercentral.amazon.in/product-search")
        safe.type_into_search(safe.by_role("searchbox").first, "B08PDL14VR")
        safe.press_search(safe.by_role("button", name=re.compile("^search$", re.I)).first)
        row = safe.by_role("listitem").first
        row.wait_for(state="visible")
        with pytest.raises(ForbiddenAction):
            safe.open_result(row, "B0DIFFERNT")


def test_the_adapters_source_has_no_way_to_press_anything_but_through_the_guard():
    package = Path(__file__).resolve().parents[1] / "app" / "browser"
    risky = re.compile(
        r"\.(click|dblclick|press|check|uncheck|tap|dispatch_event|set_input_files"
        r"|drag_to|hover)\("
    )
    for path in package.glob("*.py"):
        text = path.read_text()
        if path.name == "safe_page.py":
            # every click lives in this file, behind a name or ASIN check
            assert len(re.findall(r"\.click\(", text)) == 2
            continue
        assert not risky.search(text), f"{path.name} interacts with the page directly"
    # and nowhere does it wait on the clock
    for path in package.glob("*.py"):
        text = path.read_text()
        assert (
            "wait_for_timeout" not in text
            and "time.sleep" not in text.replace("sleep: Callable", "")
            or path.name == "runner.py"
        )


# ---- identity and evidence -------------------------------------------------------------------


def test_every_request_identifies_the_agent_and_nothing_is_disguised():
    site = FakeSellerCentral().add("B08PDL14VR")
    with verifier_for(site, browser_agent_contact="ops@example.test") as (v, handle, _):
        v.verify("B08PDL14VR", "new_new")
        assert handle.page.evaluate("navigator.webdriver") is True  # automation flag left visible
        ua_in_page = handle.page.evaluate("navigator.userAgent")
    token = "AmazonSellerEligibilityChecker/0.1 (automated agent; operator: ops@example.test)"
    assert ua_in_page.endswith(token)
    assert site.user_agents and all(token in ua for ua in site.user_agents)
    assert site.external_requests == []


def test_a_screenshot_of_the_panel_is_captured_unless_disabled():
    site = FakeSellerCentral().add("B08PDL14VR")
    with verifier_for(site) as (v, _, _):
        shot = v.verify("B08PDL14VR", "new_new").screenshot
    assert shot and shot.startswith(b"\x89PNG") and len(shot) > 1000
    with verifier_for(site, browser_screenshots=False) as (v, _, _):
        assert v.verify("B08PDL14VR", "new_new").screenshot is None


def test_the_test_browser_cannot_reach_the_real_network():
    """Guards the guard: without the fake's interception, nothing can leave the machine."""
    site = FakeSellerCentral()
    with open_browser(site) as (handle, _):
        raw = handle.page.context.browser.new_context().new_page()
        with pytest.raises(Exception, match="ERR_NAME_NOT_RESOLVED|ERR_"):
            raw.goto("https://sellercentral.amazon.in/", timeout=4000)
