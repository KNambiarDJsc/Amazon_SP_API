import pytest

from app.domain.brand import (
    brands_equivalent,
    clean_whitespace,
    dedupe_key,
    match_brand,
    match_key,
)
from app.enums import BrandMatch


def test_clean_whitespace_collapses_and_strips_invisible_characters():
    assert clean_whitespace("  eSSL  \t Pro\n") == "eSSL Pro"
    assert clean_whitespace("e​SSL") == "eSSL"
    assert clean_whitespace("﻿Taparia") == "Taparia"


def test_clean_whitespace_applies_nfkc():
    assert clean_whitespace("ＥＳＳＬ") == "ESSL"


def test_dedupe_key_is_case_and_whitespace_insensitive():
    assert dedupe_key("  eSSL ") == dedupe_key("ESSL") == dedupe_key("essl")
    assert dedupe_key("Plantex  India") == dedupe_key("plantex india")


def test_dedupe_key_distinguishes_different_brands():
    assert dedupe_key("Oddy") != dedupe_key("Oddy Pro")


@pytest.mark.parametrize(
    ("searched", "attribute", "expected"),
    [
        ("eSSL", "eSSL", BrandMatch.EXACT),
        ("eSSL", "ESSL", BrandMatch.EXACT),
        ("Kangaro", "  kangaro ", BrandMatch.EXACT),
        ("eSSL", "e-SSL", BrandMatch.NORMALIZED),
        ("e.s.s.l", "ESSL", BrandMatch.NORMALIZED),
        ("Plant ex", "Plantex", BrandMatch.NORMALIZED),
        ("Café", "CAFÉ", BrandMatch.EXACT),
        # A longer company name or a product title that merely contains the brand is not a match.
        ("eSSL", "eSSL Security Systems Pvt Ltd", BrandMatch.MISMATCH),
        ("eSSL", "Generic Mounts", BrandMatch.MISMATCH),
        ("Oddy", "Oddy Pro", BrandMatch.MISMATCH),
        ("eSSL", None, BrandMatch.MISSING),
        ("eSSL", "", BrandMatch.MISSING),
        ("eSSL", "   ", BrandMatch.MISSING),
    ],
)
def test_match_brand(searched, attribute, expected):
    assert match_brand(searched, attribute) == expected


def test_punctuation_only_names_never_match_each_other():
    assert match_key("---") == ""
    assert match_brand("---", "***") == BrandMatch.MISMATCH


def test_brands_equivalent_handles_none():
    assert not brands_equivalent(None, "eSSL")
    assert not brands_equivalent("eSSL", None)
    assert brands_equivalent("eSSL", "e-ssl")
