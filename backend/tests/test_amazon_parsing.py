import pytest
from conftest import load_fixture

from app.amazon.errors import AmazonMalformedResponse, AmazonRateLimited
from app.amazon.mock import (
    NO_RESTRICTIONS,
    MockAmazonClient,
    brand_gated,
    catalog_item,
    demo_client,
)
from app.amazon.parsing import parse_catalog_page, parse_restrictions_observation
from app.domain.classification import classify_observation
from app.domain.observations import ObservationContext
from app.enums import MARKETPLACE_ID_IN, DataSource
from app.enums import EligibilityStatus as S


def ctx(asin, condition="new_new"):
    return ObservationContext(
        asin=asin, marketplace_id=MARKETPLACE_ID_IN, condition=condition, seller_ref="s1"
    )


def classify(client, asin, condition="new_new"):
    body = client.get_listings_restrictions(
        asin=asin, seller_id="s1", marketplace_id=MARKETPLACE_ID_IN, condition=condition
    )
    return classify_observation(parse_restrictions_observation(body, context=ctx(asin, condition)))


# ---- catalog parsing -------------------------------------------------------------------------


def test_catalog_page_uses_the_requested_marketplace_summary():
    page = parse_catalog_page(load_fixture("spapi/catalog_search_page1.json"), MARKETPLACE_ID_IN)
    first = page.items[0]
    assert first.asin == "B08PDL14VR"  # upper-cased
    assert first.brand == "eSSL"  # not the other marketplace's brand
    assert first.title == "ESSL IDENTIX K30 PRO BIOMETRIC Machine"
    assert first.category_name == "Biometric Attendance Systems"
    assert first.product_type == "ELECTRONIC_DEVICE"
    assert page.next_token == "NEXT-PAGE-TOKEN"
    assert page.total_results == 3


def test_catalog_items_without_asin_are_dropped_and_missing_brand_is_none():
    page = parse_catalog_page(load_fixture("spapi/catalog_search_page1.json"), MARKETPLACE_ID_IN)
    assert [i.asin for i in page.items] == ["B08PDL14VR", "B0NOBRAND1"]
    assert page.items[1].brand is None


def test_catalog_page_without_items_list_is_malformed():
    with pytest.raises(AmazonMalformedResponse):
        parse_catalog_page({"numberOfResults": 0}, MARKETPLACE_ID_IN)


def test_catalog_page_keeps_raw_item_for_audit():
    page = parse_catalog_page(load_fixture("spapi/catalog_search_page1.json"), MARKETPLACE_ID_IN)
    assert page.items[0].raw["summaries"][1]["brand"] == "eSSL"


# ---- the mock behaves like the API shape the parser expects ----------------------------------


def test_mock_paginates_with_next_tokens():
    items = [catalog_item(f"B0MOCK{i:04d}", f"Item {i}", "eSSL") for i in range(5)]
    client = MockAmazonClient(catalog={"eSSL": items})
    seen, token = [], None
    while True:
        page = parse_catalog_page(
            client.search_catalog_items(
                keywords="essl", marketplace_id=MARKETPLACE_ID_IN, page_token=token, page_size=2
            ),
            MARKETPLACE_ID_IN,
        )
        seen += [i.asin for i in page.items]
        token = page.next_token
        if not token:
            break
    assert seen == [f"B0MOCK{i:04d}" for i in range(5)]
    assert len(client.calls_to("searchCatalogItems")) == 3


def test_mock_unknown_brand_returns_empty_results():
    page = parse_catalog_page(
        demo_client().search_catalog_items(
            keywords="Nonexistent", marketplace_id=MARKETPLACE_ID_IN
        ),
        MARKETPLACE_ID_IN,
    )
    assert page.items == [] and page.total_results == 0


def test_unconfigured_asin_is_never_reported_as_listable():
    verdict = classify(MockAmazonClient(), "B0UNKNOWN01")
    assert verdict.status == S.NEEDS_MANUAL_CHECK
    assert verdict.rule_id == "SPAPI_ASIN_NOT_FOUND"


def test_scripted_behaviours_raise_and_then_recover():
    client = MockAmazonClient(
        restrictions={
            "B0FLAKY001": [AmazonRateLimited(retry_after=2), AmazonRateLimited(), NO_RESTRICTIONS]
        }
    )
    for _ in range(2):
        with pytest.raises(AmazonRateLimited):
            client.get_listings_restrictions(
                asin="B0FLAKY001",
                seller_id="s",
                marketplace_id=MARKETPLACE_ID_IN,
                condition="new_new",
            )
    assert classify(client, "B0FLAKY001").status == S.VERIFIED_LISTABLE
    assert classify(client, "B0FLAKY001").status == S.VERIFIED_LISTABLE  # last behaviour repeats


def test_condition_specific_behaviour_takes_priority():
    client = MockAmazonClient(
        restrictions={
            "B0COND0001": NO_RESTRICTIONS,
            ("B0COND0001", "used_good"): brand_gated("B0COND0001", "used_good"),
        }
    )
    assert classify(client, "B0COND0001", "new_new").status == S.VERIFIED_LISTABLE
    assert classify(client, "B0COND0001", "used_good").status == S.BRAND_APPROVAL_REQUIRED


# ---- demo dataset end-to-end through parser + rules engine -----------------------------------


@pytest.mark.parametrize(
    ("asin", "expected"),
    [
        ("B08PDL14VR", S.VERIFIED_LISTABLE),
        ("B08Y9C5H38", S.BRAND_APPROVAL_REQUIRED),  # the Havells case from the screenshots
        ("B0MOCKTP01", S.OTHER_APPROVAL_REQUIRED),
        ("B0MOCKKG03", S.BRAND_APPROVAL_REQUIRED),
    ],
)
def test_demo_dataset_classifies_as_designed(asin, expected):
    assert classify(demo_client(), asin).status == expected


def test_demo_client_is_flagged_as_mock():
    assert demo_client().data_source == DataSource.MOCK
