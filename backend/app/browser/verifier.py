"""Reads the state of one product panel in Seller Central, without acting on it.

Waits are for explicit conditions (an element appearing, a spinner going away, rendering
settling), never fixed sleeps. Locators are by accessible role and name, kept together here so
they can be calibrated in one place against the live site. If the page does not look as expected
the safe outcome is always "unresolved", never "listable".
"""

import re
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from app.browser.safe_page import SafePage
from app.domain.conditions import condition_from_label, label_for
from app.domain.observations import BrowserSignals
from app.enums import ObservationOutcome

# ---- locator vocabulary: the one place to calibrate against the live DOM ---------------------
SEARCH_BOX = {"role": "searchbox", "name": re.compile(r"search", re.I)}
SEARCH_BUTTON = {"role": "button", "name": re.compile(r"^search$", re.I)}
RESULTS = {"role": "region", "name": re.compile(r"search results", re.I)}
NO_RESULTS_TEXT = re.compile(r"no results|didn't match|could not find", re.I)
PANEL = {"role": "dialog", "name": re.compile(r"product information", re.I)}
CONDITION_DROPDOWN = {"role": "combobox", "name": re.compile(r"condition", re.I)}
ACTION_BUTTON = {"role": "button", "name": re.compile(r"^(sell this product|apply to sell)$", re.I)}
ASIN_IN_PANEL = re.compile(r"ASIN:\s*([A-Z0-9]{10})")
UNAVAILABLE = re.compile(r"\bunavailable\b|no longer available", re.I)

SIGNIN_URL = re.compile(r"/ap/signin|/signin\b", re.I)
CHALLENGE_TEXT = re.compile(
    r"not a robot|enter the characters you see|captcha|two-step verification|one-time password"
    r"|enter (the )?otp|verification required|automated access",
    re.I,
)


class HumanRequired(Exception):
    """A sign-in, CAPTCHA or verification prompt. A person must deal with it; never the agent."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "signed_out" | "challenge"


@dataclass
class VerificationResult:
    outcome: ObservationOutcome
    signals: BrowserSignals | None = None
    error_detail: str | None = None
    screenshot: bytes | None = None
    url_path: str = ""


def _clean(text: str) -> str:
    return " ".join(text.split())


class SellerCentralVerifier:
    def __init__(self, page: SafePage, *, listing_url: str, max_attempts: int, screenshots: bool):
        self._p = page
        self._url = listing_url
        self._attempts = max_attempts
        self._screenshots = screenshots

    # ---- public ------------------------------------------------------------------------------

    def verify(self, asin: str, condition: str) -> VerificationResult:
        """Raises HumanRequired when a person must take over; otherwise returns a result."""
        last = "timed out"
        for _ in range(self._attempts):
            try:
                return self._verify_once(asin, condition)
            except PlaywrightTimeout as exc:
                self._guard()  # a prompt the locators missed must still stop us, not be retried
                last = f"timed out waiting for the page: {str(exc).splitlines()[0][:150]}"
            except PlaywrightError as exc:
                return VerificationResult(
                    ObservationOutcome.PAGE_UNAVAILABLE,
                    error_detail=f"browser error: {str(exc).splitlines()[0][:150]}",
                )
        return VerificationResult(
            ObservationOutcome.TIMEOUT,
            error_detail=f"gave up after {self._attempts} attempts; {last}",
        )

    # ---- one attempt -------------------------------------------------------------------------

    def _guard(self) -> None:
        """Stop the moment the page is a sign-in or challenge page."""
        if SIGNIN_URL.search(self._p.url()) or self._p.locator("input[type=password]").count():
            raise HumanRequired("signed_out")
        if (
            CHALLENGE_TEXT.search(self._p.body_text())
            or self._p.locator("form[action*=captcha i]").count()
        ):
            raise HumanRequired("challenge")

    def _interrupts(self):
        """Anything that means a person must take over, as a locator. Every wait includes it, so
        a sign-in page or CAPTCHA is noticed the moment it appears instead of after a timeout."""
        body = self._p.locator("body")
        return (
            body.get_by_text(CHALLENGE_TEXT)
            .filter(visible=True)
            .or_(self._p.locator("input[type=password]"))
        )

    def _await(self, target) -> None:
        """Wait for `target` to be visible, or for an interruption, then check which it was."""
        target.or_(self._interrupts()).first.wait_for(state="visible")
        self._guard()

    def _verify_once(self, asin: str, condition: str) -> VerificationResult:
        p = self._p
        p.goto(self._url)
        self._guard()

        box = p.by_role(SEARCH_BOX["role"], name=SEARCH_BOX["name"]).first
        self._await(box)
        p.type_into_search(box, asin)
        p.press_search(p.by_role(SEARCH_BUTTON["role"], name=SEARCH_BUTTON["name"]).first)

        # Either the results list or a "no results" message: wait for whichever appears.
        results = p.by_role(RESULTS["role"], name=RESULTS["name"])
        row = results.get_by_role("listitem").filter(has_text=f"ASIN: {asin}").first
        nothing = p.locator("body").get_by_text(NO_RESULTS_TEXT).filter(visible=True)
        self._await(row.or_(nothing))
        if not row.is_visible():
            return self._result(BrowserSignals(product_unavailable=True))

        p.open_result(row, asin)
        panel = p.by_role(PANEL["role"], name=PANEL["name"])
        self._await(panel)
        p.by_role("progressbar").first.wait_for(state="hidden")  # still loading? wait it out
        self._guard()

        panel_text = _clean(panel.inner_text())
        shown = ASIN_IN_PANEL.search(panel_text)
        if UNAVAILABLE.search(panel_text):
            seen = BrowserSignals(
                page_asin=shown.group(1) if shown else None, product_unavailable=True
            )
            return self._result(seen, panel=panel)

        selected = self._select_condition(panel, condition)
        p.by_role("progressbar").first.wait_for(state="hidden")
        signals = self._read(panel, selected, shown.group(1) if shown else None)
        return self._result(signals, panel=panel)

    def _select_condition(self, panel, condition: str) -> str | None:
        dropdown = panel.get_by_role(CONDITION_DROPDOWN["role"], name=CONDITION_DROPDOWN["name"])
        if dropdown.count() == 0:
            return None
        wanted = label_for(condition)
        current = self._selected_label(dropdown)
        if condition_from_label(current) != condition:
            try:
                self._p.choose_condition(dropdown.first, wanted)
            except PlaywrightError:  # that option does not exist for this product
                return condition_from_label(self._selected_label(dropdown))
        return condition_from_label(self._selected_label(dropdown))

    @staticmethod
    def _selected_label(dropdown) -> str | None:
        return dropdown.first.evaluate(
            "el => el.options ? (el.options[el.selectedIndex] || {}).text : el.textContent"
        )

    def _read(self, panel, selected: str | None, shown_asin: str | None) -> BrowserSignals:
        """Read the panel until two consecutive reads agree, so a render still in flight cannot be
        mistaken for the final state. Between reads we wait for rendering, not for time."""
        previous = None
        for _ in range(4):
            self._p.next_frames()
            notices = []
            for role in ("alert", "status"):
                notices += [_clean(t) for t in panel.get_by_role(role).all_inner_texts()]
            notices = [n for n in dict.fromkeys(notices) if n]
            button = panel.get_by_role(ACTION_BUTTON["role"], name=ACTION_BUTTON["name"])
            label = enabled = None
            if button.count():
                label = _clean(button.first.inner_text())
                enabled = button.first.is_enabled()
            current = (notices, label, enabled)
            if current == previous:
                break
            previous = current
        notices, label, enabled = current
        return BrowserSignals(
            page_asin=shown_asin,
            selected_condition=selected,
            notices=notices,
            action_label=label,
            action_enabled=enabled,
        )

    def _result(self, signals: BrowserSignals, panel=None) -> VerificationResult:
        shot = None
        if self._screenshots and panel is not None:
            shot = panel.screenshot()  # the product panel only, never the whole page
        return VerificationResult(
            ObservationOutcome.OK,
            signals=signals,
            screenshot=shot,
            url_path=self._p.url().split("?", 1)[0].split("//", 1)[-1].split("/", 1)[-1],
        )
