from app.domain.check_key import SPAPI_RESTRICTIONS_CONTEXT, make_check_key

BASE = dict(
    seller_account_id="seller-1",
    marketplace_id="A21TJRUUN4KGV",
    asin="B08PDL14VR",
    condition="new_new",
    verification_context=SPAPI_RESTRICTIONS_CONTEXT,
)


def test_key_is_stable_and_hex_sha256():
    key = make_check_key(**BASE)
    assert key == make_check_key(**BASE)
    assert len(key) == 64 and int(key, 16) >= 0


def test_asin_is_case_insensitive():
    assert make_check_key(**{**BASE, "asin": "b08pdl14vr"}) == make_check_key(**BASE)


def test_each_component_changes_the_key():
    base = make_check_key(**BASE)
    for field, other in [
        ("seller_account_id", "seller-2"),
        ("marketplace_id", "ATVPDKIKX0DER"),
        ("asin", "B0BXM5CGRM"),
        ("condition", "used_good"),
        ("verification_context", "browser:seller-central-in:v1"),
    ]:
        assert make_check_key(**{**BASE, field: other}) != base, field


def test_adjacent_fields_cannot_collide():
    a = make_check_key(**{**BASE, "seller_account_id": "ab", "marketplace_id": "c"})
    b = make_check_key(**{**BASE, "seller_account_id": "a", "marketplace_id": "bc"})
    assert a != b
