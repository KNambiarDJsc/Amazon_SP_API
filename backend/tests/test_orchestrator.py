"""End-to-end behaviour of the brand pipeline against the mock Amazon client.

Each test drives real jobs through the real scheduler and orchestrator; only Amazon, the
clock and the queue are faked.
"""

import pytest
from support import (
    Crash,
    audit_types,
    brands_of,
    candidates_of,
    checks_of,
    job_of,
    make_runtime,
    observations_of,
    submit,
)

from app.amazon import errors as err
from app.amazon.mock import (
    NO_RESTRICTIONS,
    MockAmazonClient,
    brand_gated,
    catalog_item,
    category_gated,
    demo_client,
    reason,
    restrictions_body,
)
from app.db import session_scope
from app.enums import (
    BrandMatch,
    BrandStatus,
    CheckState,
    DataSource,
    JobStatus,
    ObservationOutcome,
)
from app.enums import EligibilityStatus as S
from app.models import JobBrand, SellerAccount
from app.services.jobs import pause_job, resume_job
from app.services.orchestrator import run_brand
from app.services.scheduling import Dispatch
from app.services.tick import run_until_idle, tick

CONDITION = "new_new"


def only(brands, name):
    return brands[name]


# ---- the screenshot scenarios --------------------------------------------------------------


def test_demo_dataset_end_to_end(engine, seller, clock):
    """The five brands from the client's workbook, through every brand-level outcome."""
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"], clock)

    run_until_idle(rt)

    brands = brands_of(job_id)
    assert job_of(job_id).status == JobStatus.COMPLETED
    assert {n: b.brand_status for n, b in brands.items()} == {
        "eSSL": S.VERIFIED_LISTABLE,
        "Havells": S.BRAND_APPROVAL_REQUIRED,
        "Kangaro": S.MIXED_RESULTS,
        "Taparia": S.OTHER_APPROVAL_REQUIRED,
        "Oddy": S.NO_PRODUCT_FOUND,
    }
    assert all(b.status == BrandStatus.COMPLETED for b in brands.values())
    # A brand-level result never claims more than the sample.
    assert all(b.brand_summary["brand_wide_claim"] is False for b in brands.values())


def test_no_restrictions_is_verified_listable_with_evidence(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["eSSL"], clock)
    run_until_idle(rt)

    check = checks_of(job_id)[("B08PDL14VR", CONDITION)]
    assert check.status == S.VERIFIED_LISTABLE
    assert check.state == CheckState.DONE
    assert check.rule_id == "SPAPI_NO_RESTRICTIONS"
    assert check.verification_methods == ["SP_API_RESTRICTIONS"]
    assert check.checked_at == clock.now()
    (obs,) = [o for o in observations_of(job_id) if o.check_id == check.id]
    assert obs.outcome == ObservationOutcome.OK
    assert obs.raw == {"restrictions": []}  # exactly what Amazon returned


def test_brand_approval_message_is_recorded_with_its_link(engine, seller, clock):
    """The Havells screenshot: 'You need approval to list in this brand.'"""
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(rt)

    check = checks_of(job_id)[("B08Y9C5H38", CONDITION)]
    assert check.status == S.BRAND_APPROVAL_REQUIRED
    assert check.reason_codes == ["APPROVAL_REQUIRED"]
    assert check.messages == ["You need approval to list in this brand."]
    assert check.approval_links[0]["title"] == "Apply to sell"


def test_category_restriction_is_not_reported_as_brand_gating(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["Taparia"], clock)
    run_until_idle(rt)

    statuses = {c.status for c in checks_of(job_id).values()}
    assert statuses == {S.OTHER_APPROVAL_REQUIRED}


def test_wrong_brand_keyword_match_is_rejected_and_never_checked(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)
    run_until_idle(rt)

    brand = brands_of(job_id)["eSSL"]
    decoy = candidates_of(brand.id)["B0MOCKWR01"]
    assert decoy.brand_match == BrandMatch.MISMATCH
    assert decoy.selected is False and decoy.rank is None
    assert "Generic Mounts" in decoy.rejection_reason
    assert decoy.raw is None
    assert ("B0MOCKWR01", CONDITION) not in checks_of(job_id)
    assert "B0MOCKWR01" not in [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert brand.brand_summary["candidates_total"] == 5
    assert brand.brand_summary["candidates_brand_matched"] == 4


def test_every_candidate_is_preserved_even_when_not_selected(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["eSSL"], clock, max_asins_per_brand=2, min_sample_size=2)
    run_until_idle(rt)

    candidates = candidates_of(brands_of(job_id)["eSSL"].id)
    assert len(candidates) == 5
    assert sum(c.selected for c in candidates.values()) == 2
    assert len(checks_of(job_id)) == 2
    brand = brands_of(job_id)["eSSL"]
    # Two of four matching ASINs checked: a sample, so the summary must say so.
    assert brand.brand_summary["asins_checked"] == 2
    assert brand.brand_summary["candidates_brand_matched"] == 4


def test_no_product_found_when_catalog_has_nothing(engine, seller, clock):
    client = MockAmazonClient()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Plantex"], clock)
    run_until_idle(rt)

    brand = brands_of(job_id)["Plantex"]
    assert brand.status == BrandStatus.COMPLETED
    assert brand.brand_status == S.NO_PRODUCT_FOUND
    assert checks_of(job_id) == {}
    assert client.calls_to("getListingsRestrictions") == []


def test_brand_with_only_foreign_products_is_no_product_found(engine, seller, clock):
    client = MockAmazonClient(
        catalog={"Oddy": [catalog_item("B0AAAAAAA1", "Compatible with Oddy", "NotOddy")]}
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Oddy"], clock)
    run_until_idle(rt)

    brand = brands_of(job_id)["Oddy"]
    assert brand.brand_status == S.NO_PRODUCT_FOUND
    assert brand.brand_summary["candidates_total"] == 1
    assert client.calls_to("getListingsRestrictions") == []


# ---- ambiguity and unavailable products ----------------------------------------------------


def test_unavailable_product_needs_manual_check_not_listable(engine, seller, clock):
    client = MockAmazonClient(
        catalog={"Zed": [catalog_item("B0ZED00001", "Zed Widget", "Zed")]},
        restrictions={},  # nothing configured: the mock answers ASIN_NOT_FOUND
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Zed"], clock, min_sample_size=1)
    run_until_idle(rt)

    check = checks_of(job_id)[("B0ZED00001", CONDITION)]
    assert check.status == S.NEEDS_MANUAL_CHECK
    assert check.rule_id == "SPAPI_ASIN_NOT_FOUND"
    assert brands_of(job_id)["Zed"].brand_status == S.NEEDS_MANUAL_CHECK


def test_malformed_restrictions_body_is_manual_check_never_permission(engine, seller, clock):
    client = MockAmazonClient(
        catalog={"Zed": [catalog_item("B0ZED00001", "Zed Widget", "Zed")]},
        restrictions={"B0ZED00001": {"unexpected": "shape"}},
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Zed"], clock, min_sample_size=1)
    run_until_idle(rt)

    check = checks_of(job_id)[("B0ZED00001", CONDITION)]
    assert check.status == S.NEEDS_MANUAL_CHECK
    assert check.state == CheckState.DONE  # a bad body is answered, not retried forever


def test_unrecognised_reason_code_is_ambiguous(engine, seller, clock):
    odd = restrictions_body(reason("SOMETHING_NEW", "Brand new rule."))
    client = MockAmazonClient(
        catalog={"Zed": [catalog_item("B0ZED00001", "Zed Widget", "Zed")]},
        restrictions={"B0ZED00001": odd},
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Zed"], clock, min_sample_size=1)
    run_until_idle(rt)

    assert checks_of(job_id)[("B0ZED00001", CONDITION)].status == S.NEEDS_MANUAL_CHECK


def test_request_rejected_for_one_asin_isolates_to_that_check(engine, seller, clock):
    client = demo_client()
    client.restrictions["B0MOCKKG01"] = err.AmazonRequestError(400, "bad ASIN")
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Kangaro"], clock)
    run_until_idle(rt)

    checks = checks_of(job_id)
    assert checks[("B0MOCKKG01", CONDITION)].status == S.NEEDS_MANUAL_CHECK
    assert checks[("B0MOCKKG02", CONDITION)].status == S.VERIFIED_LISTABLE
    (failed,) = [o for o in observations_of(job_id) if o.http_status == 400]
    assert failed.outcome == ObservationOutcome.HTTP_ERROR


def test_multiple_conditions_are_checked_independently(engine, seller, clock):
    client = MockAmazonClient(
        catalog={"Zed": [catalog_item("B0ZED00001", "Zed Widget", "Zed")]},
        restrictions={
            ("B0ZED00001", "new_new"): NO_RESTRICTIONS,
            ("B0ZED00001", "used_good"): category_gated("B0ZED00001", "used_good"),
        },
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Zed"], clock, conditions=["new_new", "used_good"], min_sample_size=1)
    run_until_idle(rt)

    checks = checks_of(job_id)
    assert checks[("B0ZED00001", "new_new")].status == S.VERIFIED_LISTABLE
    assert checks[("B0ZED00001", "used_good")].status == S.OTHER_APPROVAL_REQUIRED
    assert brands_of(job_id)["Zed"].brand_status == S.MIXED_RESULTS


# ---- throttling ----------------------------------------------------------------------------


def test_amazon_429_defers_the_brand_without_burning_an_attempt(engine, seller, clock):
    client = demo_client()
    client.restrictions["B0MOCKHV01"] = [err.AmazonRateLimited(retry_after=20), brand_gated("x")]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    start = clock.now()

    run_until_idle(rt)

    brand = brands_of(job_id)["Havells"]
    assert brand.status == BrandStatus.COMPLETED
    assert brand.attempts == 1  # the throttled run was refunded
    assert (clock.now() - start).total_seconds() >= 20  # it really waited
    assert "brand_released" in audit_types(job_id)
    calls = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert calls.count("B0MOCKHV01") == 2
    assert calls.count("B08Y9C5H38") == 1  # finished work was not repeated
    assert len(client.calls_to("searchCatalogItems")) == 1  # nor was discovery


def test_local_rate_limit_spaces_calls_and_never_exceeds_the_rate(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(
        client,
        clock,
        rate_restrictions_per_sec=1.0,
        rate_restrictions_burst=1,
        max_rate_wait_seconds=0.0,
    )
    job_id = submit(seller, ["eSSL"], clock)
    stamps: list[float] = []
    original = client.get_listings_restrictions

    def recording(**kwargs):
        stamps.append(clock.current.timestamp())
        return original(**kwargs)

    client.get_listings_restrictions = recording
    run_until_idle(rt)

    assert brands_of(job_id)["eSSL"].status == BrandStatus.COMPLETED
    assert len(stamps) == 4
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= 1.0 - 1e-6 for g in gaps), gaps


def test_throttled_catalog_search_is_deferred_too(engine, seller, clock):
    client = demo_client()
    client.catalog_behaviour = [err.AmazonRateLimited(retry_after=5), None]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(rt)

    assert brands_of(job_id)["Havells"].status == BrandStatus.COMPLETED
    assert len(client.calls_to("searchCatalogItems")) == 2


# ---- expired authorisation -----------------------------------------------------------------


def test_expired_authorisation_pauses_the_job_and_decides_nothing(engine, seller, clock):
    client = demo_client()
    client.restrictions["B08PDL14VR"] = [err.AmazonAuthError("token revoked"), NO_RESTRICTIONS]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)

    run_until_idle(rt)

    job = job_of(job_id)
    assert job.status == JobStatus.PAUSED
    assert "reauthorise" in job.pause_reason
    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.PENDING and brand.attempts == 0
    assert brand.brand_status is None
    check = checks_of(job_id)[("B08PDL14VR", CONDITION)]
    assert check.state == CheckState.PENDING and check.status is None
    auth_obs = [o for o in observations_of(job_id) if o.outcome == ObservationOutcome.AUTH_ERROR]
    assert len(auth_obs) == 1
    assert "authorisation_failed" in audit_types(job_id)

    # The seller reauthorises and resumes; the same check is simply asked again.
    with session_scope() as s:
        resume_job(s, job_of_in(s, job_id), now=clock.now())
    run_until_idle(rt)
    assert job_of(job_id).status == JobStatus.COMPLETED
    assert checks_of(job_id)[("B08PDL14VR", CONDITION)].status == S.VERIFIED_LISTABLE


def test_auth_failure_pauses_every_running_job_of_that_seller_only(engine, seller, clock):
    other = SellerAccount(name="Other", api_key_hash="b" * 64, api_key_prefix="oth")
    with session_scope() as s:
        s.add(other)
    client = demo_client()
    client.restrictions["B0MOCKHV01"] = err.AmazonAuthError("expired")
    rt = make_runtime(client, clock, seller_max_concurrent_brands=1)
    mine_1 = submit(seller, ["Havells"], clock)
    mine_2 = submit(seller, ["Kangaro"], clock)
    theirs = submit(other, ["Taparia"], clock)

    run_until_idle(rt)

    assert job_of(mine_1).status == JobStatus.PAUSED
    assert job_of(mine_2).status == JobStatus.PAUSED
    assert job_of(theirs).status == JobStatus.COMPLETED


def test_live_mode_without_a_seller_id_is_an_authorisation_problem(engine, seller, clock):
    client = demo_client()
    client.data_source = DataSource.LIVE
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)

    run_until_idle(rt)

    assert job_of(job_id).status == JobStatus.PAUSED
    assert client.calls == []  # nothing was asked of Amazon on behalf of an unknown seller


# ---- retries and dead-lettering ------------------------------------------------------------


def test_transient_failure_retries_with_backoff_then_succeeds(engine, seller, clock):
    client = demo_client()
    client.restrictions["B08Y9C5H38"] = [
        err.AmazonTransientError("503"),
        brand_gated("B08Y9C5H38"),
    ]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    start = clock.now()

    run_until_idle(rt)

    brand = brands_of(job_id)["Havells"]
    assert brand.status == BrandStatus.COMPLETED
    assert brand.attempts == 2
    assert (clock.now() - start).total_seconds() >= 5  # waited out the backoff (10s base, jittered)
    outcomes = [o.outcome for o in observations_of(job_id)]
    assert ObservationOutcome.HTTP_ERROR in outcomes  # the failed attempt stays in the record
    assert "brand_retry_scheduled" in audit_types(job_id)


def test_timeouts_are_recorded_as_timeouts(engine, seller, clock):
    client = demo_client()
    client.restrictions["B08Y9C5H38"] = [err.AmazonTimeout("read timed out"), brand_gated("x")]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(rt)

    assert ObservationOutcome.TIMEOUT in [o.outcome for o in observations_of(job_id)]


def test_persistent_failure_dead_letters_into_manual_review(engine, seller, clock):
    client = demo_client()
    client.restrictions["B0MOCKKG02"] = err.AmazonTransientError("503 forever")
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Kangaro"], clock)

    run_until_idle(rt)

    brand = brands_of(job_id)["Kangaro"]
    assert brand.status == BrandStatus.FAILED
    assert brand.attempts == 3
    assert "503 forever" in brand.last_error
    assert brand.brand_status == S.MIXED_RESULTS
    checks = checks_of(job_id)
    assert checks[("B0MOCKKG02", CONDITION)].status == S.NEEDS_MANUAL_CHECK
    assert checks[("B0MOCKKG01", CONDITION)].status == S.VERIFIED_LISTABLE  # earlier work kept
    # The flaky ASIN did not starve the one after it: that got a real answer on the first run.
    assert checks[("B0MOCKKG03", CONDITION)].status == S.BRAND_APPROVAL_REQUIRED
    asked = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert asked.count("B0MOCKKG03") == 1 and asked.count("B0MOCKKG02") == 3
    assert job_of(job_id).status == JobStatus.COMPLETED  # a failed brand does not hang the job
    assert "brand_failed" in audit_types(job_id)


def test_consecutive_transient_failures_stop_the_run_early(engine, seller, clock):
    """Two failures in a row look like an outage, so the brand backs off rather than hammering."""
    client = demo_client()
    # Checks run in ASIN order: B084FZXB48, B08PDL14VR, B0B63WV6CW, B0BXM5CGRM. Fail the first two.
    for asin in ("B084FZXB48", "B08PDL14VR"):
        client.restrictions[asin] = [err.AmazonTransientError("503"), NO_RESTRICTIONS]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)

    tick_sent: list[Dispatch] = []
    tick(rt, tick_sent.append)
    assert run_brand(tick_sent[0].brand_id, tick_sent[0].token, "w", rt) == "RETRY"

    asked = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert len(asked) == 2  # stopped after the second consecutive failure
    assert brands_of(job_id)["eSSL"].status == BrandStatus.PENDING


def test_failed_brands_can_be_retried_after_amazon_recovers(engine, seller, clock):
    from app.services.jobs import retry_failed_brands

    client = demo_client()
    client.restrictions["B0MOCKKG02"] = err.AmazonTransientError("down")
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Kangaro"], clock)
    run_until_idle(rt)
    assert brands_of(job_id)["Kangaro"].status == BrandStatus.FAILED

    client.restrictions["B0MOCKKG02"] = NO_RESTRICTIONS  # Amazon recovers
    with session_scope() as s:
        assert retry_failed_brands(s, job_of_in(s, job_id), now=clock.now()) == 1
    run_until_idle(rt)

    brand = brands_of(job_id)["Kangaro"]
    assert brand.status == BrandStatus.COMPLETED and brand.attempts == 1
    assert checks_of(job_id)[("B0MOCKKG02", CONDITION)].status == S.VERIFIED_LISTABLE
    # The failed attempt's evidence is history, not overwritten.
    outcomes = [o.outcome for o in observations_of(job_id)]
    assert ObservationOutcome.HTTP_ERROR in outcomes and ObservationOutcome.OK in outcomes
    assert job_of(job_id).status == JobStatus.COMPLETED


def test_catalog_outage_is_not_reported_as_no_product_found(engine, seller, clock):
    client = demo_client()
    client.catalog_behaviour = err.AmazonTransientError("catalog down")
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)
    run_until_idle(rt)

    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.FAILED
    assert brand.brand_status == S.NEEDS_MANUAL_CHECK
    assert brand.brand_summary["rule_id"] == "BRAND_DISCOVERY_FAILED"


def test_catalog_4xx_dead_letters_immediately(engine, seller, clock):
    client = demo_client()
    client.catalog_behaviour = err.AmazonRequestError(400, "invalid keywords")
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)
    run_until_idle(rt)

    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.FAILED and brand.attempts == 1  # retrying cannot help


def test_unexpected_exception_hurts_one_brand_not_the_worker(engine, seller, clock):
    client = demo_client()
    original = client.search_catalog_items

    def explode_for_havells(**kwargs):
        if kwargs["keywords"] == "Havells":
            raise RuntimeError("bug in the client")
        return original(**kwargs)

    client.search_catalog_items = explode_for_havells
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells", "eSSL"], clock)
    run_until_idle(rt)

    brands = brands_of(job_id)
    assert brands["Havells"].status == BrandStatus.FAILED
    assert "bug in the client" in brands["Havells"].last_error
    assert brands["eSSL"].status == BrandStatus.COMPLETED


# ---- pause, resume, cancel -----------------------------------------------------------------


def job_of_in(session, job_id):
    from app.models import Job

    return session.get(Job, job_id)


def test_pause_mid_brand_then_resume_does_not_repeat_finished_checks(engine, seller, clock):
    client = demo_client()
    original = client.get_listings_restrictions
    calls = {"n": 0}

    def pause_on_second_call(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            with session_scope() as s:
                pause_job(s, job_of_in(s, job_id), reason="user paused", now=clock.now())
        return original(**kwargs)

    client.get_listings_restrictions = pause_on_second_call
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)

    run_until_idle(rt)

    assert job_of(job_id).status == JobStatus.PAUSED
    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.PENDING and brand.attempts == 0
    done = [c for c in checks_of(job_id).values() if c.state == CheckState.DONE]
    assert len(done) == 2  # the two that ran before the pause are kept

    with session_scope() as s:
        resume_job(s, job_of_in(s, job_id), now=clock.now())
    client.get_listings_restrictions = original
    run_until_idle(rt)

    assert job_of(job_id).status == JobStatus.COMPLETED
    asked = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert sorted(asked) == sorted(set(asked)) == sorted(a for a, _ in checks_of(job_id))
    assert len(client.calls_to("searchCatalogItems")) == 1


def test_paused_job_is_left_alone_by_the_scheduler(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)
    with session_scope() as s:
        pause_job(s, job_of_in(s, job_id), reason="hold", now=clock.now())

    assert run_until_idle(rt) == 0
    assert client.calls == []
    assert brands_of(job_id)["eSSL"].status == BrandStatus.PENDING


def test_cancel_stops_pending_brands_and_keeps_finished_work(engine, seller, clock):
    from app.services.jobs import cancel_job

    client = demo_client()
    original = client.search_catalog_items

    def cancel_during_second_brand(**kwargs):
        if kwargs["keywords"] == "Havells":
            with session_scope() as s:
                cancel_job(s, job_of_in(s, job_id), now=clock.now())
        return original(**kwargs)

    client.search_catalog_items = cancel_during_second_brand
    rt = make_runtime(client, clock, seller_max_concurrent_brands=1)
    job_id = submit(seller, ["eSSL", "Havells", "Kangaro"], clock)

    run_until_idle(rt)

    brands = brands_of(job_id)
    assert job_of(job_id).status == JobStatus.CANCELLED
    assert brands["eSSL"].status == BrandStatus.COMPLETED
    assert brands["Havells"].status == BrandStatus.CANCELLED
    assert brands["Kangaro"].status == BrandStatus.CANCELLED
    assert brands["eSSL"].brand_status == S.VERIFIED_LISTABLE  # results survive cancellation
    assert all(c["asin"] != "B0MOCKKG01" for c in client.calls_to("getListingsRestrictions"))


# ---- crashes and lost messages -------------------------------------------------------------


def test_worker_crash_mid_brand_is_recovered_without_repeating_finished_checks(
    engine, seller, clock
):
    client = demo_client()
    original = client.get_listings_restrictions
    state = {"calls": 0, "armed": True}

    def die_on_third_call(**kwargs):
        state["calls"] += 1
        if state["armed"] and state["calls"] == 3:
            raise Crash()  # the process is gone: no handler, no clean-up
        return original(**kwargs)

    client.get_listings_restrictions = die_on_third_call
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)

    with pytest.raises(Crash):
        run_until_idle(rt)

    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.IN_PROGRESS  # orphaned, still holding its lease
    assert brand.lease_owner.startswith("worker:")
    assert len([c for c in checks_of(job_id).values() if c.state == CheckState.DONE]) == 2

    state["armed"] = False
    run_until_idle(rt)  # waits out the lease, reclaims, retries

    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.COMPLETED
    assert brand.attempts == 2  # the crashed run counted, so a poison brand cannot loop forever
    assert brand.brand_status == S.VERIFIED_LISTABLE
    assert "lease_expired" in audit_types(job_id)
    asked = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    # The crash fired before the third call reached Amazon, so each of the four ASINs was asked
    # exactly once: the two finished before the crash were not asked again on recovery.
    assert len(asked) == len(set(asked)) == 4
    assert len(checks_of(job_id)) == 4  # no duplicate checks were created


def test_poison_brand_that_always_crashes_the_worker_is_eventually_dead_lettered(
    engine, seller, clock
):
    client = demo_client()
    client.restrictions["B08Y9C5H38"] = lambda n: (_ for _ in ()).throw(Crash())
    rt = make_runtime(client, clock, max_brand_attempts=2)
    job_id = submit(seller, ["Havells"], clock)

    for _ in range(10):
        try:
            run_until_idle(rt)
            break
        except Crash:
            continue

    brand = brands_of(job_id)["Havells"]
    assert brand.status == BrandStatus.FAILED
    assert brand.attempts == 2
    assert "lease expired" in brand.last_error


def test_duplicate_message_delivery_cannot_run_a_brand_twice(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    sent: list[Dispatch] = []
    tick(rt, sent.append)
    (dispatch,) = sent

    assert run_brand(dispatch.brand_id, dispatch.token, "w1", rt) == "COMPLETED"
    calls_after_first = len(client.calls)
    assert run_brand(dispatch.brand_id, dispatch.token, "w2", rt) == "STALE"  # redelivered
    assert run_brand(dispatch.brand_id, "wrong-token", "w3", rt) == "STALE"
    assert len(client.calls) == calls_after_first
    assert brands_of(job_id)["Havells"].attempts == 1


def test_lost_queue_message_is_redispatched_without_penalty(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)

    tick(rt, lambda d: None)  # the message is "sent" into the void (Redis flushed, say)
    assert brands_of(job_id)["Havells"].status == BrandStatus.IN_PROGRESS
    clock.advance(rt.settings.queue_lease_seconds + 1)

    run_until_idle(rt)

    brand = brands_of(job_id)["Havells"]
    assert brand.status == BrandStatus.COMPLETED
    assert brand.attempts == 1  # never started the first time, so nothing was charged


def test_enqueue_failure_does_not_lose_the_brand(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["Havells"], clock)

    def broken_queue(_dispatch):
        raise ConnectionError("redis is down")

    with pytest.raises(ConnectionError):
        tick(rt, broken_queue)
    clock.advance(rt.settings.queue_lease_seconds + 1)
    run_until_idle(rt)

    assert brands_of(job_id)["Havells"].status == BrandStatus.COMPLETED


# ---- shared ASINs and pagination -----------------------------------------------------------


def test_two_brands_matching_the_same_asin_share_its_check(engine, seller, clock):
    """'e-SSL' and 'eSSL' are different rows but the same catalog products."""
    base = demo_client()
    catalog = {"eSSL": base.catalog["essl"], "e-SSL": base.catalog["essl"]}
    client = MockAmazonClient(catalog=catalog, restrictions=base.restrictions)
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL", "e-SSL"], clock)

    run_until_idle(rt)

    brands = brands_of(job_id)
    assert all(b.status == BrandStatus.COMPLETED for b in brands.values())
    assert len(checks_of(job_id)) == 4  # not 8: one check per ASIN in the job
    assert brands["eSSL"].brand_status == S.VERIFIED_LISTABLE
    assert brands["e-SSL"].brand_status == S.VERIFIED_LISTABLE
    asked = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert len(asked) == len(set(asked)) == 4  # Amazon was asked once per ASIN


def test_catalog_results_beyond_the_page_cap_make_the_sample_non_exhaustive(engine, seller, clock):
    items = [catalog_item(f"B0PAGED{n:03d}", f"Pagey {n}", "Pagey") for n in range(45)]
    client = MockAmazonClient(
        catalog={"Pagey": items}, restrictions={i["asin"]: NO_RESTRICTIONS for i in items}
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Pagey"], clock, max_catalog_pages=2, max_asins_per_brand=3)

    run_until_idle(rt)

    assert len(client.calls_to("searchCatalogItems")) == 2
    brand = brands_of(job_id)["Pagey"]
    assert len(candidates_of(brand.id)) == 40  # 2 pages of 20; the rest were never seen
    assert brand.brand_status == S.VERIFIED_LISTABLE  # 3 sampled ASINs, all listable
    assert brand.brand_summary["brand_wide_claim"] is False
    assert brand.brand_summary["checks_completed"] == 3


def test_brand_name_is_searched_as_cleaned_text_but_original_is_kept(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["  eSSL ​ "], clock)
    run_until_idle(rt)

    brand = brands_of(job_id)["eSSL"]
    assert brand.original_name == "  eSSL ​ "
    assert client.calls_to("searchCatalogItems")[0]["keywords"] == "eSSL"


def test_raw_evidence_is_never_mutated_by_a_recheck(engine, seller, clock):
    """Observations are append-only: a later attempt adds rows rather than editing old ones."""
    client = demo_client()
    client.restrictions["B08Y9C5H38"] = [err.AmazonTransientError("blip"), brand_gated("x")]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(rt)

    obs = [o for o in observations_of(job_id) if o.context["asin"] == "B08Y9C5H38"]
    assert [o.outcome for o in obs] == [ObservationOutcome.HTTP_ERROR, ObservationOutcome.OK]
    assert obs[0].error_detail == "blip" and obs[0].signals is None
    assert checks_of(job_id)[("B08Y9C5H38", CONDITION)].status == S.BRAND_APPROVAL_REQUIRED


def test_progress_counts_add_up(engine, seller, clock):
    from app.services.jobs import compute_progress

    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"], clock)
    run_until_idle(rt)

    with session_scope() as s:
        p = compute_progress(s, job_id)
    assert p.brands_total == p.brands_completed == 5
    assert p.percent_complete == 100.0
    assert p.checks_total == p.checks_done == 12  # 4 + 2 + 3 + 3
    assert p.verified_listable == 6  # 4 eSSL + 2 Kangaro
    assert p.restricted == 6  # 2 Havells brand + 1 Kangaro brand + 3 Taparia category
    assert p.needs_manual_check == 0
    assert sum(p.by_status.values()) == p.checks_done


def test_job_brand_rows_belong_to_the_submitting_seller(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(rt)
    with session_scope() as s:
        rows = s.query(JobBrand).filter(JobBrand.job_id == job_id).all()
    assert {r.seller_account_id for r in rows} == {seller.id}
