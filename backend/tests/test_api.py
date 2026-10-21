import io

import pytest
from apienv import Env  # noqa: E402
from openpyxl import Workbook, load_workbook

from app.db import session_scope
from app.models import SellerAccount


@pytest.fixture
def env(engine):
    return Env()


@pytest.fixture
def auth(env):
    return env.new_seller()[1]


def create(env, auth, brands, **extra):
    r = env.http.post("/api/v1/jobs", json={"brands": brands, **extra}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


# ---- authentication --------------------------------------------------------------------------


def test_requests_without_a_valid_key_are_rejected(env, auth):
    assert env.http.get("/api/v1/jobs").status_code == 401
    assert env.http.get("/api/v1/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert (
        env.http.get("/api/v1/jobs", headers={"Authorization": "Bearer spk_wrong"}).status_code
        == 401
    )
    assert env.http.get("/api/v1/jobs", headers=auth).status_code == 200


def test_deactivated_seller_loses_access(env):
    seller_id, headers = env.new_seller()
    with session_scope() as s:
        s.get(SellerAccount, seller_id).is_active = False
    assert env.http.get("/api/v1/me", headers=headers).status_code == 401


def test_api_keys_are_stored_hashed(env):
    seller_id, headers = env.new_seller()
    key = headers["Authorization"].removeprefix("Bearer ")
    with session_scope() as s:
        account = s.get(SellerAccount, seller_id)
        assert key not in (account.api_key_hash, account.api_key_prefix)
        assert len(account.api_key_hash) == 64 and key.startswith(account.api_key_prefix)


def test_me_reports_mode_and_connection(env, auth):
    body = env.http.get("/api/v1/me", headers=auth).json()
    assert body == {
        "name": "Seller A",
        "marketplace_id": "A21TJRUUN4KGV",
        "amazon_mode": "mock",
        "connected_to_amazon": False,
    }


def test_health_endpoints_need_no_auth(env):
    assert env.http.get("/healthz").json() == {"status": "ok"}
    assert env.http.get("/readyz").json() == {"status": "ready"}


# ---- creating jobs ---------------------------------------------------------------------------


def test_create_job_reports_ingestion_and_flags_demo_data(env, auth):
    job = create(env, auth, ["eSSL", "ESSL", " Havells ", "", "---"], name="First run")
    assert job["status"] == "RUNNING" and job["name"] == "First run"
    assert job["data_source"] == "MOCK" and "not live Amazon results" in job["notice"]
    report = job["ingestion_report"]
    assert report["accepted"] == 2 and report["empty_rows"] == [4]
    assert report["duplicates"][0] == {"row": 2, "original_name": "ESSL", "duplicate_of_row": 1}
    assert report["invalid"][0]["reason"] == "NO_LETTERS_OR_DIGITS"
    assert job["progress"]["brands_total"] == 2
    assert len(env.dispatcher.messages) == 2  # both within the seller window


@pytest.mark.parametrize(
    "body",
    [
        {"brands": []},
        {"brands": ["", "  "]},
        {"brands": ["x"], "settings": {"min_sample_size": 6, "max_asins_per_brand": 5}},
        {"brands": ["x"], "settings": {"conditions": ["new_new", "new_new"]}},
        {"brands": ["x"], "settings": {"conditions": ["brand_new"]}},
        {"brands": ["x"], "settings": {"max_catalog_pages": 50}},
        {"name": "n" * 300, "brands": ["x"]},
        {},
    ],
)
def test_invalid_job_requests_are_422(env, auth, body):
    assert env.http.post("/api/v1/jobs", json=body, headers=auth).status_code == 422


def test_job_size_limit(env, auth):
    env.cfg.max_brands_per_job = 3
    r = env.http.post("/api/v1/jobs", json={"brands": list("abcd")}, headers=auth)
    assert r.status_code == 422 and "Too many brands" in r.json()["detail"]


def test_upload_csv_and_xlsx(env, auth):
    csv_body = b"Brand Name,Status\neSSL,Pending\nKangaro,Pending\nessl,Pending\n"
    r = env.http.post(
        "/api/v1/jobs/upload",
        headers=auth,
        files={"file": ("brands.csv", csv_body)},
        data={"name": "csv run", "settings": '{"max_asins_per_brand": 4}'},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["source_filename"] == "brands.csv" and body["settings"]["max_asins_per_brand"] == 4
    assert body["ingestion_report"]["accepted"] == 2
    assert [d["duplicate_of_row"] for d in body["ingestion_report"]["duplicates"]] == [2]

    wb = Workbook()
    ws = wb.active
    ws.append(["Brand Name", "Status", "ASIN"])
    ws.append(["Taparia", "Pending", None])
    buf = io.BytesIO()
    wb.save(buf)
    r = env.http.post(
        "/api/v1/jobs/upload", headers=auth, files={"file": ("b.xlsx", buf.getvalue())}
    )
    assert r.status_code == 201 and r.json()["name"] == "b.xlsx"


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("b.csv", b"", "empty"),
        ("b.csv", b"\n\n,\n", "No usable"),
        ("b.xlsx", b"PK\x03\x04garbage", "not a valid .xlsx"),
        ("b.xls", b"\xd0\xcf\x11\xe0", "Legacy .xls"),
        ("b.csv", b"x" * (65 * 1024), "larger than"),
    ],
)
def test_bad_uploads_are_rejected_with_a_helpful_message(env, auth, filename, content, message):
    r = env.http.post("/api/v1/jobs/upload", headers=auth, files={"file": (filename, content)})
    assert r.status_code == 422 and message in r.json()["detail"]


def test_upload_with_invalid_settings_json_is_422(env, auth):
    files = {"file": ("b.csv", b"eSSL\n")}
    assert (
        env.http.post(
            "/api/v1/jobs/upload", headers=auth, files=files, data={"settings": "{not json"}
        ).status_code
        == 422
    )
    assert (
        env.http.post(
            "/api/v1/jobs/upload",
            headers=auth,
            files=files,
            data={"settings": '{"max_asins_per_brand": 99}'},
        ).status_code
        == 422
    )


# ---- reading results -------------------------------------------------------------------------


def run_demo(env, auth):
    job = create(env, auth, ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"])
    env.drain()
    return job["id"]


def test_progress_and_listing_after_the_job_completes(env, auth):
    job_id = run_demo(env, auth)
    job = env.http.get(f"/api/v1/jobs/{job_id}", headers=auth).json()
    assert job["status"] == "COMPLETED" and job["finished_at"]
    p = job["progress"]
    assert (p["brands_completed"], p["percent_complete"]) == (5, 100.0)
    assert (p["verified_listable"], p["restricted"], p["needs_manual_check"]) == (6, 6, 0)
    assert p["by_status"]["BRAND_APPROVAL_REQUIRED"] == 3
    assert env.http.get("/api/v1/jobs", headers=auth).json()["total"] == 1


def test_brand_filters_and_search(env, auth):
    job_id = run_demo(env, auth)
    base = f"/api/v1/jobs/{job_id}/brands"
    brands = env.http.get(base, headers=auth).json()
    assert brands["total"] == 5 and [b["display_name"] for b in brands["items"]][:2] == [
        "eSSL",
        "Havells",
    ]

    got = env.http.get(base, params={"brand_status": "NO_PRODUCT_FOUND"}, headers=auth).json()
    assert [b["display_name"] for b in got["items"]] == ["Oddy"]
    got = env.http.get(base, params={"brand_status": "MIXED_RESULTS"}, headers=auth).json()
    assert got["items"][0]["brand_summary"]["status_counts"]["VERIFIED_LISTABLE"] == 2
    assert env.http.get(base, params={"q": "hav"}, headers=auth).json()["total"] == 1
    assert env.http.get(base, params={"status": "COMPLETED"}, headers=auth).json()["total"] == 5


def test_check_filters_search_and_pagination(env, auth):
    job_id = run_demo(env, auth)
    base = f"/api/v1/jobs/{job_id}/checks"
    everything = env.http.get(base, params={"limit": 500}, headers=auth).json()
    assert everything["total"] == 12

    listable = env.http.get(base, params={"status": "VERIFIED_LISTABLE"}, headers=auth).json()
    assert listable["total"] == 6 and {c["state"] for c in listable["items"]} == {"DONE"}
    assert {c["brand"] for c in listable["items"]} == {"eSSL", "Kangaro"}

    page1 = env.http.get(base, params={"limit": 5, "offset": 0}, headers=auth).json()
    page2 = env.http.get(base, params={"limit": 5, "offset": 5}, headers=auth).json()
    assert page1["total"] == 12 and len(page1["items"]) == len(page2["items"]) == 5
    assert {c["id"] for c in page1["items"]}.isdisjoint({c["id"] for c in page2["items"]})

    by_asin = env.http.get(base, params={"q": "b08y9c5h38"}, headers=auth).json()
    assert [c["asin"] for c in by_asin["items"]] == ["B08Y9C5H38"]
    by_title = env.http.get(base, params={"q": "hair dryer"}, headers=auth).json()
    assert by_title["total"] == 1


def test_search_text_is_literal_not_a_pattern(env, auth):
    job_id = run_demo(env, auth)
    for wildcard in ("%", "_", "%%"):
        r = env.http.get(f"/api/v1/jobs/{job_id}/checks", params={"q": wildcard}, headers=auth)
        assert r.json()["total"] == 0, wildcard


def test_check_detail_exposes_the_evidence_trail(env, auth):
    job_id = run_demo(env, auth)
    checks = env.http.get(
        f"/api/v1/jobs/{job_id}/checks", params={"q": "B08Y9C5H38"}, headers=auth
    ).json()
    detail = env.http.get(
        f"/api/v1/jobs/{job_id}/checks/{checks['items'][0]['id']}", headers=auth
    ).json()
    assert detail["status"] == "BRAND_APPROVAL_REQUIRED" and detail["rules_version"]
    assert detail["messages"] == ["You need approval to list in this brand."]
    assert detail["approval_links"][0]["title"] == "Apply to sell"
    assert detail["candidate"]["brand_attribute"] == "Havells"
    (obs,) = detail["observations"]
    assert obs["source"] == "SP_API_RESTRICTIONS" and obs["outcome"] == "OK"
    assert obs["raw"]["restrictions"][0]["reasons"][0]["reasonCode"] == "APPROVAL_REQUIRED"
    assert obs["evidence_ref"].startswith("db:check_observations/")


def test_candidates_include_rejected_products_with_the_reason(env, auth):
    job_id = run_demo(env, auth)
    brand = env.http.get(
        f"/api/v1/jobs/{job_id}/brands", params={"q": "essl"}, headers=auth
    ).json()["items"][0]
    cands = env.http.get(
        f"/api/v1/jobs/{job_id}/brands/{brand['id']}/candidates", headers=auth
    ).json()
    assert len(cands) == 5
    rejected = next(c for c in cands if c["asin"] == "B0MOCKWR01")
    assert rejected["rejection_reason"] == "BRAND_ATTRIBUTE_MISMATCH" and not rejected["selected"]
    assert [c["rank"] for c in cands][:4] == [1, 2, 3, 4]  # ranked first, rejected last


def test_audit_events_are_exposed(env, auth):
    job_id = run_demo(env, auth)
    events = env.http.get(f"/api/v1/jobs/{job_id}/events", headers=auth).json()
    types = {e["event_type"] for e in events}
    assert {"JOB_CREATED", "BRAND_COMPLETED", "JOB_COMPLETED"} <= types


# ---- lifecycle -------------------------------------------------------------------------------


def test_pause_resume_cancel_and_conflicts(env, auth):
    job = create(env, auth, ["eSSL", "Havells", "Kangaro"])
    jid = job["id"]
    post = lambda action: env.http.post(f"/api/v1/jobs/{jid}/{action}", headers=auth)  # noqa: E731

    assert post("pause").json()["status"] == "PAUSED"
    assert post("pause").status_code == 200  # idempotent
    env.drain()  # queued messages arrive while paused and do nothing
    assert (
        env.http.get(f"/api/v1/jobs/{jid}", headers=auth).json()["progress"]["brands_completed"]
        == 0
    )

    resumed = post("resume").json()
    assert resumed["status"] == "RUNNING" and resumed["pause_reason"] is None
    env.drain()
    assert env.http.get(f"/api/v1/jobs/{jid}", headers=auth).json()["status"] == "COMPLETED"
    assert post("pause").status_code == 409 and post("cancel").status_code == 409

    other = create(env, auth, ["Taparia"])["id"]
    assert (
        env.http.post(f"/api/v1/jobs/{other}/cancel", headers=auth).json()["status"] == "CANCELLED"
    )
    assert env.http.post(f"/api/v1/jobs/{other}/resume", headers=auth).status_code == 409


# ---- export ----------------------------------------------------------------------------------


def test_export_downloads_a_four_sheet_workbook(env, auth):
    job_id = run_demo(env, auth)
    r = env.http.get(f"/api/v1/jobs/{job_id}/export.xlsx", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
    assert f"eligibility-{job_id[:8]}.xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == [
        "Verified Listable Products",
        "Restricted Products",
        "Manual Review",
        "Full Audit History",
    ]
    assert wb["Verified Listable Products"].max_row == 7  # header + 6 listable products


# ---- isolation -------------------------------------------------------------------------------


def test_one_seller_cannot_see_or_touch_another_sellers_job(env):
    _, a = env.new_seller("A")
    _, b = env.new_seller("B")
    job_id = run_demo(env, a)
    check_id = env.http.get(f"/api/v1/jobs/{job_id}/checks", headers=a).json()["items"][0]["id"]

    probes = [
        ("get", f"/api/v1/jobs/{job_id}"),
        ("get", f"/api/v1/jobs/{job_id}/brands"),
        ("get", f"/api/v1/jobs/{job_id}/checks"),
        ("get", f"/api/v1/jobs/{job_id}/checks/{check_id}"),
        ("get", f"/api/v1/jobs/{job_id}/events"),
        ("get", f"/api/v1/jobs/{job_id}/export.xlsx"),
        ("post", f"/api/v1/jobs/{job_id}/pause"),
        ("post", f"/api/v1/jobs/{job_id}/resume"),
        ("post", f"/api/v1/jobs/{job_id}/cancel"),
    ]
    for method, url in probes:
        r = getattr(env.http, method)(url, headers=b)
        assert r.status_code == 404, (method, url, r.status_code)
        assert job_id not in r.text.replace(url, "")  # nothing about the job leaks into the error

    assert env.http.get("/api/v1/jobs", headers=b).json() == {
        "items": [],
        "total": 0,
        "limit": 25,
        "offset": 0,
    }
    # ... and the owner is unaffected.
    assert env.http.get(f"/api/v1/jobs/{job_id}", headers=a).json()["status"] == "COMPLETED"


def test_a_checks_id_from_another_job_of_the_same_seller_is_not_found(env, auth):
    first = run_demo(env, auth)
    second = create(env, auth, ["Havells"])["id"]
    check_id = env.http.get(f"/api/v1/jobs/{first}/checks", headers=auth).json()["items"][0]["id"]
    assert env.http.get(f"/api/v1/jobs/{second}/checks/{check_id}", headers=auth).status_code == 404


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"status": "BOGUS"}, {"state": "NOPE"}],
)
def test_query_parameters_are_validated(env, auth, params):
    job_id = run_demo(env, auth)
    assert (
        env.http.get(f"/api/v1/jobs/{job_id}/checks", params=params, headers=auth).status_code
        == 422
    )


def test_malformed_ids_are_422_not_500(env, auth):
    assert env.http.get("/api/v1/jobs/not-a-uuid", headers=auth).status_code == 422
