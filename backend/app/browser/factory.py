"""Starts Chromium for a seller session. Playwright is an optional dependency."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from app.browser.policy import agent_token
from app.config import Settings


@dataclass
class BrowserHandle:
    page: Any
    context: Any

    def state(self) -> dict:
        return self.context.storage_state()


class PlaywrightFactory:
    """One browser, one context, one page per session. It identifies itself as an automated agent
    in the User-Agent and changes nothing else about how the browser presents itself: no stealth
    patches, no hidden automation flags."""

    def __init__(
        self,
        cfg: Settings,
        *,
        headless: bool | None = None,
        on_context: Callable[[Any], None] | None = None,
    ):
        self._cfg = cfg
        self._headless = cfg.browser_headless if headless is None else headless
        self._on_context = on_context

    @contextmanager
    def open(self, storage_state: dict | None = None) -> Iterator[BrowserHandle]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install the 'browser' extra: pip install '.[browser]'"
            ) from exc

        cfg = self._cfg
        args = [a.strip() for a in cfg.browser_launch_args.split(",") if a.strip()]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self._headless,
                executable_path=cfg.browser_executable_path or None,
                args=args,
            )
            try:
                probe = browser.new_context()
                default_ua = probe.new_page().evaluate("navigator.userAgent")
                probe.close()
                context = browser.new_context(
                    storage_state=storage_state,
                    user_agent=f"{default_ua} {agent_token(cfg)}",
                    accept_downloads=False,
                    locale="en-IN",
                )
                if self._on_context:
                    self._on_context(context)
                page = context.new_page()
                try:
                    yield BrowserHandle(page=page, context=context)
                finally:
                    context.close()
            finally:
                browser.close()
