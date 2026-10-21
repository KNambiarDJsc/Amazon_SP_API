"""Brand-name normalisation and matching. Pure functions, no I/O."""

import re
import unicodedata

from app.enums import BrandMatch

# Zero-width characters and the BOM survive str.split() and break equality checks.
_INVISIBLE = re.compile("[​‌‍⁠﻿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def clean_whitespace(value: str) -> str:
    """NFKC-normalise, drop invisible/control characters, collapse runs of whitespace."""
    text = unicodedata.normalize("NFKC", value)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    return _WS.sub(" ", text).strip()


def dedupe_key(value: str) -> str:
    """Case-insensitive identity used to collapse duplicate rows in an upload."""
    return clean_whitespace(value).casefold()


def match_key(value: str) -> str:
    """Looser identity that also ignores punctuation and spacing."""
    return _NON_ALNUM.sub("", dedupe_key(value))


def match_brand(searched: str, attribute: str | None) -> BrandMatch:
    """Compare the brand a client asked for with the brand attribute Amazon reports.

    Anything weaker than a normalised equality is a mismatch: a title that merely
    mentions the brand, or a longer company name that contains it, is not evidence.
    """
    if attribute is None or not clean_whitespace(attribute):
        return BrandMatch.MISSING
    if dedupe_key(searched) == dedupe_key(attribute):
        return BrandMatch.EXACT
    searched_key = match_key(searched)
    if searched_key and searched_key == match_key(attribute):
        return BrandMatch.NORMALIZED
    return BrandMatch.MISMATCH


def brands_equivalent(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return match_brand(a, b) in (BrandMatch.EXACT, BrandMatch.NORMALIZED)
