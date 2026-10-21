"""Operator account management: rotating a leaked key, deactivating a seller, listing sellers."""

import uuid

import pytest
from apienv import Env
from sqlalchemy import select

from app.cli import main
from app.db import session_scope
from app.models import AuditEvent, Job


@pytest.fixture
def env(engine):
    return Env()


def status(env, headers):
    return env.http.get("/api/v1/me", headers=headers).status_code


def new_key(capsys):
    out = capsys.readouterr().out
    return next(w for w in out.split() if w.startswith("spk_"))


def test_rotating_a_key_replaces_it_at_once_and_touches_nobody_else(env, capsys):
    seller_id, old = env.new_seller("A")
    _, other = env.new_seller("B")
    assert status(env, old) == status(env, other) == 200

    assert main(["rotate-key", "--seller-account", str(seller_id)]) == 0
    fresh = {"Authorization": f"Bearer {new_key(capsys)}"}

    assert status(env, old) == 401  # the leaked key is dead immediately
    assert status(env, fresh) == 200
    assert status(env, other) == 200
    with session_scope() as s:
        events = s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "API_KEY_ROTATED")
        ).all()
        assert [e.seller_account_id for e in events] == [seller_id]
        # only 8-character prefixes are audited, never a key
        assert set(events[0].payload) == {"previous_prefix", "new_prefix"}
        assert all(len(v) == 8 for v in events[0].payload.values())


def test_the_old_keys_hash_is_gone_after_rotation(env, capsys):
    seller_id, old = env.new_seller("A")
    old_key = old["Authorization"].split()[1]
    main(["rotate-key", "--seller-account", str(seller_id)])
    capsys.readouterr()
    with session_scope() as s:
        from app.services.accounts import hash_api_key, list_accounts

        assert hash_api_key(old_key) not in {a.api_key_hash for a in list_accounts(s)}


def test_deactivating_revokes_access_and_pauses_running_jobs_until_the_seller_resumes(env, capsys):
    seller_id, headers = env.new_seller("A")
    job = env.http.post(
        "/api/v1/jobs", json={"brands": ["eSSL", "Havells"]}, headers=headers
    ).json()
    with session_scope() as s:  # a worker has picked it up
        from app.enums import JobStatus

        s.get(Job, uuid.UUID(job["id"])).status = JobStatus.RUNNING

    assert main(["deactivate-seller", "--seller-account", str(seller_id)]) == 0
    assert "1 running job(s) paused" in capsys.readouterr().out
    assert status(env, headers) == 401
    with session_scope() as s:
        row = s.get(Job, uuid.UUID(job["id"]))
        assert (row.status.value, row.pause_reason) == ("PAUSED", "ACCOUNT_DEACTIVATED")

    assert main(["activate-seller", "--seller-account", str(seller_id)]) == 0
    assert status(env, headers) == 200
    again = env.http.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()
    assert again["status"] == "PAUSED"  # the seller decides when to resume

    with session_scope() as s:
        kinds = [e.event_type for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
        assert "SELLER_DEACTIVATED" in kinds and "SELLER_ACTIVATED" in kinds


def test_listing_shows_state_and_key_prefix_but_never_a_key(env, capsys):
    a, key_a = env.new_seller("Acme Traders")
    b, _ = env.new_seller("Beta Co")
    main(["deactivate-seller", "--seller-account", str(b)])
    capsys.readouterr()

    assert main(["list-sellers"]) == 0
    out = capsys.readouterr().out
    assert str(a) in out and "Acme Traders" in out and "Beta Co" in out
    assert "INACTIVE" in out and "active" in out
    assert key_a["Authorization"].split()[1] not in out  # only the 8-character prefix is shown


@pytest.mark.parametrize("command", ["rotate-key", "deactivate-seller", "activate-seller"])
def test_bad_ids_fail_clearly_instead_of_with_a_traceback(env, capsys, command):
    assert main([command, "--seller-account", "not-a-uuid"]) == 1
    assert "UUID" in capsys.readouterr().err
    assert main([command, "--seller-account", str(uuid.uuid4())]) == 1
    assert "No such seller account" in capsys.readouterr().err
