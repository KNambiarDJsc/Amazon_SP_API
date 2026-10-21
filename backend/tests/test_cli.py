import io

import pytest
from openpyxl import load_workbook
from support import brands_of, submit

from app import cli
from app.auth import authenticate
from app.config import get_settings
from app.db import session_scope


def test_demo_runs_offline_and_writes_a_labelled_workbook(engine, tmp_path, capsys):
    out = tmp_path / "report.xlsx"

    assert cli.main(["demo", "--out", str(out), "--database-url", "sqlite://"]) == 0

    printed = capsys.readouterr().out
    assert "DEMO DATA" in printed
    assert "Havells      BRAND_APPROVAL_REQUIRED" in printed
    assert "Oddy         NO_PRODUCT_FOUND" in printed
    wb = load_workbook(io.BytesIO(out.read_bytes()))
    assert wb["Verified Listable Products"]["A1"].value.startswith("DEMO DATA")
    assert wb["Verified Listable Products"].max_row == 2 + 6


def test_demo_accepts_custom_brands(engine, tmp_path, capsys):
    out = tmp_path / "r.xlsx"
    assert cli.main(["demo", "--out", str(out), "Havells", "Nobody"]) == 0
    printed = capsys.readouterr().out
    assert "Havells" in printed and "Nobody       NO_PRODUCT_FOUND" in printed


def test_create_seller_prints_a_working_key_exactly_once(engine, capsys):
    assert cli.main(["create-seller", "Acme Traders", "--amazon-seller-id", "A1ACME"]) == 0
    printed = capsys.readouterr().out
    key = next(line.split(":", 1)[1].strip() for line in printed.splitlines() if "api key" in line)
    assert key.startswith("spk_") and len(key) > 30
    assert "cannot be shown again" in printed

    with session_scope() as s:
        account = authenticate(s, key)
        assert account is not None and account.amazon_seller_id == "A1ACME"
        assert key not in (account.api_key_hash, account.api_key_prefix)  # never stored
        assert account.api_key_prefix == key[:12]


def test_drain_refuses_live_mode_instead_of_faking_results(engine, monkeypatch, capsys):
    monkeypatch.setenv("AMAZON_MODE", "live")
    get_settings.cache_clear()
    try:
        assert cli.main(["drain"]) == 2
    finally:
        get_settings.cache_clear()
    assert "Phase 3" in capsys.readouterr().err


def test_drain_runs_open_jobs(engine, seller, clock, capsys, monkeypatch):
    job_id = submit(seller, ["Havells"], clock)
    monkeypatch.setenv("RATE_LIMITER", "memory")
    get_settings.cache_clear()
    try:
        assert cli.main(["drain"]) == 0
    finally:
        get_settings.cache_clear()
    assert "ran 1 brand(s)" in capsys.readouterr().out
    assert brands_of(job_id)["Havells"].brand_status.value == "BRAND_APPROVAL_REQUIRED"


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        cli.main([])
