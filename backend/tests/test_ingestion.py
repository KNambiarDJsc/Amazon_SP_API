import io

import pytest
from openpyxl import Workbook

from app.config import get_settings
from app.services import ingestion
from app.services.ingestion import IngestionError, ingest_brands


def xlsx_bytes(rows, *, sheet_title="Sheet1") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def names(result):
    return [b.display_name for b in result.accepted]


# ---- normalisation, duplicates, empties ----------------------------------------------------


def test_pasted_text_normalises_and_preserves_original():
    result = ingest_brands(ingestion.rows_from_text("  eSSL  \nKangaro\n\nTaparia​\n"))
    assert names(result) == ["eSSL", "Kangaro", "Taparia"]
    assert result.accepted[0].original_name == "  eSSL  "
    assert result.report.empty_rows == [3]
    assert [b.position for b in result.accepted] == [1, 2, 3]


def test_case_insensitive_duplicates_keep_first_spelling_and_are_reported():
    result = ingest_brands(ingestion.rows_from_text("eSSL\nESSL\nessl \nOddy"))
    assert names(result) == ["eSSL", "Oddy"]
    dupes = result.report.duplicates
    assert [(d.row, d.duplicate_of_row) for d in dupes] == [(2, 1), (3, 1)]


def test_internal_whitespace_collapses_before_duplicate_check():
    result = ingest_brands(ingestion.rows_from_text("Johnson   Controls\njohnson controls"))
    assert names(result) == ["Johnson Controls"]
    assert len(result.report.duplicates) == 1


def test_punctuation_only_and_overlong_rows_are_invalid_not_silently_dropped():
    long_name = "x" * (get_settings().max_brand_length + 1)
    result = ingest_brands(ingestion.rows_from_text(f"---\n{long_name}\nOddy"))
    assert names(result) == ["Oddy"]
    reasons = {i.row: i.reason for i in result.report.invalid}
    assert "no letters or digits" in reasons[1]
    assert "longer than" in reasons[2]


def test_report_counts_reconcile():
    result = ingest_brands(ingestion.rows_from_text("A\n\nA\n---\nB"))
    r = result.report
    assert r.total_rows == 5
    assert r.accepted + len(r.empty_rows) + len(r.duplicates) + len(r.invalid) == r.total_rows


def test_too_many_brands_rejected_rather_than_truncated(monkeypatch):
    monkeypatch.setenv("MAX_BRANDS_PER_JOB", "3")
    get_settings.cache_clear()
    try:
        with pytest.raises(IngestionError, match="exceeds the limit"):
            ingest_brands(ingestion.rows_from_text("a\nb\nc\nd"))
    finally:
        get_settings.cache_clear()


# ---- CSV -----------------------------------------------------------------------------------


def test_csv_with_brand_name_header_in_a_later_column():
    data = b"Status,Brand Name,ASIN\nPending,eSSL,\nPending,Kangaro,\n"
    result = ingest_brands(ingestion.rows_from_csv(data))
    assert names(result) == ["eSSL", "Kangaro"]
    assert result.accepted[0].row == 2  # spreadsheet row, header is row 1


def test_csv_without_header_keeps_first_row():
    result = ingest_brands(ingestion.rows_from_csv(b"eSSL\nKangaro\n"))
    assert names(result) == ["eSSL", "Kangaro"]


def test_csv_utf8_bom_and_legacy_cp1252():
    assert names(ingest_brands(ingestion.rows_from_csv("﻿Brand\nBosch\n".encode()))) == ["Bosch"]
    assert names(ingest_brands(ingestion.rows_from_csv("Brand\nNestlé\n".encode("cp1252")))) == [
        "Nestlé"
    ]


def test_csv_quoted_commas_are_one_brand():
    result = ingest_brands(ingestion.rows_from_csv(b'Brand\n"Procter, Gamble"\n'))
    assert names(result) == ["Procter, Gamble"]


# ---- XLSX ----------------------------------------------------------------------------------


def test_xlsx_matching_the_clients_example_workbook():
    rows = [
        ["Brand Name", "Status", "Product Name", "ASIN", "Notes"],
        ["eSSL", "Pending", None, None, None],
        ["Kangaro", "Pending"],
        ["Taparia", "Pending"],
        [None, "Pending"],
        ["Plantex", "Pending"],
    ]
    result = ingest_brands(ingestion.rows_from_xlsx(xlsx_bytes(rows)))
    assert names(result) == ["eSSL", "Kangaro", "Taparia", "Plantex"]
    assert result.report.empty_rows == [5]


def test_xlsx_numeric_brand_cells_become_text():
    result = ingest_brands(ingestion.rows_from_xlsx(xlsx_bytes([["Brand"], [3], [7.5]])))
    assert names(result) == ["3", "7.5"]


def test_xlsx_formula_text_is_kept_as_text():
    # Stored verbatim; the report writer neutralises it on the way out.
    result = ingest_brands(ingestion.rows_from_csv(b"Brand\n=HYPERLINK(1)\n"))
    assert names(result) == ["=HYPERLINK(1)"]


def test_non_xlsx_bytes_rejected():
    with pytest.raises(IngestionError, match="not a valid .xlsx"):
        ingestion.rows_from_xlsx(b"this is not a zip")


def test_oversized_decompressed_workbook_rejected(monkeypatch):
    monkeypatch.setattr(ingestion, "_MAX_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(IngestionError, match="too large"):
        ingestion.rows_from_xlsx(xlsx_bytes([["Brand"], ["eSSL"]]))


def test_upload_dispatch_by_extension():
    assert names(ingest_brands(ingestion.rows_from_upload("b.CSV", b"Brand\nA\n"))) == ["A"]
    with pytest.raises(IngestionError, match="unsupported"):
        ingestion.rows_from_upload("b.exe", b"x")
