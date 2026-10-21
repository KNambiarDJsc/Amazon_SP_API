import hashlib

# Version the verification context so a change in API version or method never reuses old keys.
SPAPI_RESTRICTIONS_CONTEXT = "sp_api:getListingsRestrictions:2021-08-01"
BROWSER_CONTEXT = "browser:seller-central-in:v1"


def make_check_key(
    *,
    seller_account_id: str,
    marketplace_id: str,
    asin: str,
    condition: str,
    verification_context: str,
) -> str:
    """Idempotency key for one eligibility check.

    The unit-separator joiner keeps ("ab", "c") and ("a", "bc") from colliding.
    """
    parts = (
        seller_account_id,
        marketplace_id,
        asin.strip().upper(),
        condition,
        verification_context,
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
