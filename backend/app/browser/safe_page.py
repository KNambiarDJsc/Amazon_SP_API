"""The only code that may touch a Seller Central page.

It exposes a handful of primitives and nothing else: navigate within one host, type into the
search box, press *Search*, open a search result, choose a condition in the dropdown. It cannot
click "Sell this product", "Apply to sell", "Copy listing", or any control that submits, offers,
lists or requests approval: those are refused even if a caller asks. Reading is unrestricted.
"""

import re
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page

_FORBIDDEN_CONTROL = re.compile(
    r"apply to sell|sell this product|copy listing|request approval|submit|confirm"
    r"|create (a )?listing|save and finish|add (an )?offer|continue|publish",
    re.IGNORECASE,
)
_NEXT_TWO_FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"


class ForbiddenAction(RuntimeError):
    """An action outside the read-only allowlist was attempted."""


class SafePage:
    def __init__(self, page: Page, *, allowed_host: str, timeout_ms: int):
        self._page = page
        self._host = allowed_host
        page.set_default_timeout(timeout_ms)
        page.on("dialog", lambda dialog: dialog.dismiss())  # never accept a confirmation prompt

    # ---- navigation (one host only) ----------------------------------------------------------

    def goto(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != self._host:
            raise ForbiddenAction(f"navigation outside {self._host} refused: {parsed.hostname}")
        self._page.goto(url, wait_until="domcontentloaded")

    # ---- read-only handles -------------------------------------------------------------------

    def by_role(self, role: str, **kwargs) -> Locator:
        return self._page.get_by_role(role, **kwargs)

    def locator(self, selector: str) -> Locator:
        return self._page.locator(selector)

    def url(self) -> str:
        return self._page.url

    def body_text(self) -> str:
        return self._page.locator("body").inner_text()

    def next_frames(self) -> None:
        """Let rendering catch up (two animation frames). Not a timed sleep."""
        self._page.evaluate(_NEXT_TWO_FRAMES)

    def wait_for_load(self) -> None:
        self._page.wait_for_load_state("domcontentloaded")

    # ---- the whole interaction allowlist -----------------------------------------------------

    @staticmethod
    def _describe(locator: Locator) -> tuple[str, str]:
        tag, name = locator.evaluate(
            "el => [el.tagName, (el.getAttribute('aria-label') || el.innerText || '').trim()]"
        )
        return tag, name

    def type_into_search(self, box: Locator, text: str) -> None:
        box.fill(text)

    def press_search(self, button: Locator) -> None:
        _, name = self._describe(button)
        if not re.fullmatch(r"search", name.strip(), re.IGNORECASE):
            raise ForbiddenAction(f"only the Search button may be pressed here, not {name!r}")
        button.click()

    def open_result(self, row: Locator, asin: str) -> None:
        tag, name = self._describe(row)
        if f"ASIN: {asin}".casefold() not in name.casefold():
            raise ForbiddenAction("refusing to open a result that is not the requested ASIN")
        if tag == "BUTTON" and _FORBIDDEN_CONTROL.search(name):
            raise ForbiddenAction(f"refusing to press a control named {name!r}")
        row.click()

    def choose_condition(self, dropdown: Locator, label: str) -> None:
        dropdown.select_option(label=label)  # changes the page's view state; submits nothing
