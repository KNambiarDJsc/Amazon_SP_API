import io

from openpyxl import load_workbook
from support import checks_of, job_of, make_runtime, submit

from app.amazon import errors as err
from app.amazon.mock import MockAmazonClient, catalog_item, demo_client
from app.db import session_scope
from app.enums import DataSource, JobStatus
from app.models import Job
from app.services.jobs import pause_job
from app.services.reports import (
    SHEET_AUDIT,
    SHEET_BRANDS,
    SHEET_LISTABLE,
    SHEET_MANUAL,
    SHEET_RESTRICTED,
    build_workbook,
    report_filename,
    safe_text,
    sheet_names,
)
from app.services.tick import run_until_idle

DEMO_BRANDS = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"]


def export(job_id, clock):
    with session_scope() as s:
        data = build_workbook(s, s.get(Job, job_id), generated=clock.now())
    return load_workbook(io.BytesIO(data))


def table(wb, sheet):
    """Rows below the banner and header, as dicts keyed by header text."""
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[1]
    return [dict(zip(header, r, strict=True)) for r in rows[2:] if any(v is not None for v in r)]


def banner(wb, sheet=SHEET_LISTABLE):
    return wb[sheet]["A1"].value


def run_demo(seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, DEMO_BRANDS, clock)
    run_until_idle(rt)
    return job_id


# ---- structure -----------------------------------------------------------------------------


def test_workbook_has_the_four_contracted_sheets_first_in_order(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    assert wb.sheetnames[:4] == [SHEET_LISTABLE, SHEET_RESTRICTED, SHEET_MANUAL, SHEET_AUDIT]
    assert list(sheet_names()) == wb.sheetnames


def test_check_sheets_carry_every_column_the_spec_asks_for(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    header = [c.value for c in wb[SHEET_LISTABLE][2]]
    for required in (
        "Brand",
        "ASIN",
        "Product Title",
        "Marketplace",
        "Condition",
        "Status",
        "Approval Reason",
        "Verification Method",
        "Seller Account Ref",
        "Evidence Ref",
        "Checked At (UTC)",
    ):
        assert required in header


def test_sheets_have_frozen_header_and_filter(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    ws = wb[SHEET_LISTABLE]
    assert ws.freeze_panes == "A3"
    assert ws.auto_filter.ref.startswith("A2:")


# ---- content -------------------------------------------------------------------------------


def test_listable_sheet_contains_only_verified_listable_products(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    rows = table(wb, SHEET_LISTABLE)
    assert len(rows) == 6  # 4 eSSL + 2 Kangaro
    assert {r["Status"] for r in rows} == {"VERIFIED_LISTABLE"}
    assert {r["Brand"] for r in rows} == {"eSSL", "Kangaro"}
    essl = next(r for r in rows if r["ASIN"] == "B08PDL14VR")
    assert essl["Product Title"] == "ESSL IDENTIX K30 PRO BIOMETRIC Machine"
    assert essl["Marketplace"] == "A21TJRUUN4KGV"
    assert essl["Condition"] == "new_new"
    assert essl["Verification Method"] == "SP_API_RESTRICTIONS"
    assert essl["Evidence Ref"].startswith("observation:")
    assert essl["Checked At (UTC)"] is not None


def test_restricted_sheet_has_the_approval_message_and_link(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    rows = table(wb, SHEET_RESTRICTED)
    assert len(rows) == 6  # 2 Havells + 1 Kangaro + 3 Taparia
    assert {r["Status"] for r in rows} == {"BRAND_APPROVAL_REQUIRED", "OTHER_APPROVAL_REQUIRED"}
    havells = next(r for r in rows if r["ASIN"] == "B08Y9C5H38")
    assert havells["Approval Reason"] == "You need approval to list in this brand."
    assert "approvalrequest" in havells["Approval Link"]
    # Nothing restricted leaks into the listable sheet.
    listable_asins = {r["ASIN"] for r in table(wb, SHEET_LISTABLE)}
    assert not listable_asins & {r["ASIN"] for r in rows}


def test_manual_review_lists_brands_with_nothing_to_check(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    rows = table(wb, SHEET_MANUAL)
    assert [(r["Brand"], r["Status"], r["ASIN"]) for r in rows] == [
        ("Oddy", "NO_PRODUCT_FOUND", None)
    ]


def test_wrong_brand_candidates_appear_in_no_sheet(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    for name in (SHEET_LISTABLE, SHEET_RESTRICTED, SHEET_MANUAL, SHEET_AUDIT):
        assert "B0MOCKWR01" not in {r.get("ASIN") for r in table(wb, name)}


def test_brand_summary_gives_a_plain_result_per_brand(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    rows = {r["Searched As"]: r for r in table(wb, SHEET_BRANDS)}
    assert rows["eSSL"]["Brand-Level Result"] == "VERIFIED_LISTABLE"
    assert rows["Havells"]["Brand-Level Result"] == "BRAND_APPROVAL_REQUIRED"
    assert rows["Kangaro"]["Brand-Level Result"] == "MIXED_RESULTS"
    assert rows["Kangaro"]["Listable ASINs"] == "B0MOCKKG01, B0MOCKKG02"
    assert rows["Oddy"]["Brand-Level Result"] == "NO_PRODUCT_FOUND"
    assert "Says nothing about ASINs that were not checked" in rows["eSSL"]["What It Means"]
    assert len(rows) == 5


def test_audit_history_keeps_failed_attempts_alongside_the_answer(engine, seller, clock):
    client = demo_client()
    client.restrictions["B08Y9C5H38"] = [
        err.AmazonTimeout("read timed out"),
        client.restrictions["B08Y9C5H38"],
    ]
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(rt)

    rows = [r for r in table(export(job_id, clock), SHEET_AUDIT) if r["ASIN"] == "B08Y9C5H38"]
    assert [r["Observation Outcome"] for r in rows] == ["TIMEOUT", "OK"]
    assert rows[0]["Error Detail"] == "read timed out"
    assert rows[1]["Reason Codes"] == "APPROVAL_REQUIRED"
    assert {r["Check Status (current)"] for r in rows} == {"BRAND_APPROVAL_REQUIRED"}


# ---- honesty about what the data is --------------------------------------------------------


def test_mock_exports_are_stamped_as_demo_data_on_every_sheet(engine, seller, clock):
    wb = export(run_demo(seller, clock), clock)
    for name in wb.sheetnames:
        assert banner(wb, name).startswith("DEMO DATA")
        assert "NOT results from Amazon" in banner(wb, name)


def test_live_export_is_not_labelled_demo(engine, seller, clock):
    client = demo_client()
    client.data_source = DataSource.LIVE
    seller_id = seller.id
    with session_scope() as s:
        s.get(type(seller), seller_id).amazon_seller_id = "A1SELLER"
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock, data_source=DataSource.LIVE)
    run_until_idle(rt)

    wb = export(job_id, clock)
    assert "DEMO" not in banner(wb)
    assert "A1SELLER" in banner(wb)
    assert "PARTIAL" not in banner(wb)  # the job finished
    assert table(wb, SHEET_RESTRICTED)[0]["Seller Account Ref"] == "A1SELLER"


def test_export_of_an_unfinished_job_says_it_is_partial_and_omits_unchecked_asins(
    engine, seller, clock
):
    client = demo_client()
    original = client.get_listings_restrictions
    calls = {"n": 0}

    def pause_after_two(**kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            with session_scope() as s:
                pause_job(s, s.get(Job, job_id), reason="stop", now=clock.now())
        return original(**kwargs)

    client.get_listings_restrictions = pause_after_two
    client.data_source = DataSource.LIVE
    with session_scope() as s:
        s.get(type(seller), seller.id).amazon_seller_id = "A1SELLER"
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    run_until_idle(rt)
    assert job_of(job_id).status == JobStatus.PAUSED

    wb = export(job_id, clock)
    assert "PARTIAL" in banner(wb)
    listed = {r["ASIN"] for r in table(wb, SHEET_LISTABLE)}
    unchecked = {a for (a, _), c in checks_of(job_id).items() if c.status is None}
    assert unchecked and not listed & unchecked  # unchecked is never presented as listable
    audit_rows = {r["ASIN"]: r for r in table(wb, SHEET_AUDIT)}
    assert all(audit_rows[a]["Check Status (current)"] == "NOT CHECKED" for a in unchecked)


# ---- hostile input -------------------------------------------------------------------------


def test_formula_injection_is_neutralised_in_every_text_column(engine, seller, clock):
    evil = '=HYPERLINK("http://evil.example","click")'
    client = MockAmazonClient(
        catalog={evil: [catalog_item("B0EVIL0001", "+cmd|' /C calc'!A0", evil)]},
        restrictions={"B0EVIL0001": {"restrictions": []}},
    )
    rt = make_runtime(client, clock)
    job_id = submit(seller, [evil], clock, min_sample_size=1)
    run_until_idle(rt)

    wb = export(job_id, clock)
    for sheet in (SHEET_LISTABLE, SHEET_AUDIT, SHEET_BRANDS):
        for row in wb[sheet].iter_rows(min_row=3):
            for cell in row:
                if isinstance(cell.value, str) and cell.value.lstrip("'")[:1] in "=+-@":
                    assert cell.value.startswith("'"), (sheet, cell.coordinate, cell.value)
                    assert cell.data_type == "s"
    listable = table(wb, SHEET_LISTABLE)[0]
    assert listable["Brand"].startswith("'=")
    assert listable["Product Title"].startswith("'+cmd")


def test_safe_text_only_touches_dangerous_prefixes():
    assert safe_text("eSSL") == "eSSL"
    assert safe_text("=1+1") == "'=1+1"
    assert safe_text("@SUM(A1)") == "'@SUM(A1)"
    assert safe_text("\tcmd") == "'\tcmd"
    assert safe_text("x" * 40_000) == "x" * 32_000


def test_report_filename_cannot_escape_the_download_directory(engine, seller, clock):
    job_id = submit(seller, ["eSSL"], clock)
    job = job_of(job_id)
    job.name = "../../etc/passwd \u0000 Q4/brands"
    name = report_filename(job)
    assert name.endswith(".xlsx") and "/" not in name and ".." not in name and "\x00" not in name
    job.name = None
    assert report_filename(job).startswith("brand-check-")


# ---- scale ---------------------------------------------------------------------------------


def test_a_few_hundred_brands_export_correctly(engine, seller, clock):
    brands = [f"Brand{n:04d}" for n in range(150)]
    catalog = {
        b: [catalog_item(f"B0{n:04d}{i}", f"{b} item {i}", b) for i in range(3)]
        for n, b in enumerate(brands)
    }
    restrictions = {i["asin"]: {"restrictions": []} for items in catalog.values() for i in items}
    rt = make_runtime(
        MockAmazonClient(catalog=catalog, restrictions=restrictions),
        clock,
        seller_max_concurrent_brands=10,
        rate_restrictions_per_sec=1000,
        rate_restrictions_burst=1000,
        rate_search_catalog_per_sec=1000,
        rate_search_catalog_burst=1000,
    )
    job_id = submit(seller, brands, clock)
    run_until_idle(rt)

    wb = export(job_id, clock)
    assert len(table(wb, SHEET_LISTABLE)) == 450
    assert len(table(wb, SHEET_BRANDS)) == 150
    assert len(table(wb, SHEET_AUDIT)) == 450
    assert table(wb, SHEET_RESTRICTED) == [] and table(wb, SHEET_MANUAL) == []
