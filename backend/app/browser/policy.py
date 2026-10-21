"""The gate in front of any automated browsing of Seller Central.

Amazon's Business Solutions Agreement (Agent Policy, in force since 4 March 2026) requires
automated systems to identify themselves as automated, to follow the policy at all times and to
stop when Amazon asks. This module makes those obligations explicit: the feature is off unless the
operator enables it, names themselves, and the seller has recorded that they confirmed their own
agreement permits it.
"""

from app.config import Settings
from app.models import SellerAccount

ACK_VERSION = "2026-10-21"
ACK_TEXT = """\
By continuing I confirm that:
  1. I am authorised to access this Seller Central account.
  2. I have read Amazon's current Business Solutions Agreement and its Agent Policy (Seller
     Central Help > Program Policies) and, for my account and this use, automated browser access
     is permitted. SP-API already provides this data; I am using the browser only where it
     cannot.
  3. This tool identifies itself as an automated agent and I will not alter that.
  4. It stops at any sign-in, CAPTCHA or verification prompt; I will complete those myself, and I
     will stop it at once if Amazon asks.
"""


class BrowserNotPermitted(RuntimeError):
    """Automated browsing is not enabled/confirmed. The message says what is missing."""


def agent_token(cfg: Settings) -> str:
    contact = cfg.browser_agent_contact
    return f"AmazonSellerEligibilityChecker/0.1 (automated agent; operator: {contact})"


def ensure_permitted(cfg: Settings, seller: SellerAccount) -> None:
    if not cfg.browser_automation_enabled:
        raise BrowserNotPermitted(
            "Browser verification is disabled. The operator must set "
            "BROWSER_AUTOMATION_ENABLED=true after reading docs/BROWSER_VERIFICATION.md."
        )
    if not cfg.browser_agent_contact.strip():
        raise BrowserNotPermitted(
            "Set BROWSER_AGENT_CONTACT (an email or URL for the operator): the agent must "
            "identify itself and who runs it."
        )
    if seller.browser_ack_at is None or seller.browser_ack_version != ACK_VERSION:
        raise BrowserNotPermitted(
            "This seller has not acknowledged the Agent Policy terms for browser verification. "
            "Run: python -m app.cli browser-acknowledge --seller-account <id> --i-confirm"
        )
