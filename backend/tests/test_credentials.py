import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from app.models import SellerAccount, SellerCredential
from app.services import credentials
from app.services.credentials import CredentialCipher, CredentialError, generate_key

TOKEN = "Atzr|super-secret-refresh-token-0123456789"


def other_seller(session):
    other = SellerAccount(
        id=uuid.uuid4(), name="O", api_key_hash=uuid.uuid4().hex * 2, api_key_prefix="o"
    )
    session.add(other)
    session.commit()
    return other


def test_round_trip_and_ciphertext_is_not_the_token():
    cipher, sid = CredentialCipher([generate_key()]), uuid.uuid4()
    blob = cipher.encrypt(sid, TOKEN)
    assert TOKEN.encode() not in blob and b"super-secret" not in blob
    assert cipher.decrypt(sid, blob) == TOKEN


def test_each_encryption_is_different_but_both_decrypt():
    cipher, sid = CredentialCipher([generate_key()]), uuid.uuid4()
    a, b = cipher.encrypt(sid, TOKEN), cipher.encrypt(sid, TOKEN)
    assert a != b and cipher.decrypt(sid, a) == cipher.decrypt(sid, b) == TOKEN


def test_wrong_key_and_tampering_are_refused():
    sid = uuid.uuid4()
    blob = CredentialCipher([generate_key()]).encrypt(sid, TOKEN)
    with pytest.raises(CredentialError):
        CredentialCipher([generate_key()]).decrypt(sid, blob)
    cipher = CredentialCipher([generate_key()])
    good = cipher.encrypt(sid, TOKEN)
    with pytest.raises(CredentialError):
        cipher.decrypt(sid, good[:-4] + b"AAAA")


def test_ciphertext_cannot_be_moved_to_another_sellers_row():
    cipher = CredentialCipher([generate_key()])
    a, b = uuid.uuid4(), uuid.uuid4()
    blob = cipher.encrypt(a, TOKEN)
    with pytest.raises(CredentialError, match="does not belong"):
        cipher.decrypt(b, blob)


def test_key_rotation_new_key_encrypts_old_key_still_decrypts():
    old, new, sid = generate_key(), generate_key(), uuid.uuid4()
    legacy = CredentialCipher([old]).encrypt(sid, TOKEN)
    rotating = CredentialCipher([new, old])
    assert rotating.decrypt(sid, legacy) == TOKEN  # old data still readable
    fresh = rotating.rotate(legacy)
    assert CredentialCipher([new]).decrypt(sid, fresh) == TOKEN  # old key can now be dropped
    with pytest.raises(CredentialError):
        CredentialCipher([new]).decrypt(sid, legacy)


@pytest.mark.parametrize("value", ["", "  ,  ", "not-a-fernet-key"])
def test_missing_or_invalid_keys_fail_loudly_with_guidance(value):
    with pytest.raises(CredentialError) as info:
        CredentialCipher.from_setting(value)
    assert "CREDENTIAL_ENCRYPTION_KEYS" in str(info.value)
    assert TOKEN not in str(info.value)


def test_store_load_and_nothing_plaintext_in_the_database(session, seller):
    cipher = CredentialCipher([generate_key()])
    credentials.save_refresh_token(session, cipher, seller.id, TOKEN, authorization_type="SELF")
    session.commit()
    raw = session.execute(text("select refresh_token_encrypted from seller_credentials")).scalar()
    assert TOKEN.encode() not in bytes(raw)
    token, row = credentials.load_refresh_token(session, cipher, seller.id)
    assert token == TOKEN and row.status == "ACTIVE" and row.authorization_type == "SELF"


def test_saving_again_replaces_the_token_and_reactivates(session, seller):
    cipher = CredentialCipher([generate_key()])
    credentials.save_refresh_token(session, cipher, seller.id, TOKEN, authorization_type="SELF")
    credentials.mark_revoked(session, seller.id, "invalid_grant")
    with pytest.raises(CredentialError, match="revoked or expired"):
        credentials.load_refresh_token(session, cipher, seller.id)
    credentials.save_refresh_token(
        session, cipher, seller.id, TOKEN + "2", authorization_type="OAUTH"
    )
    token, row = credentials.load_refresh_token(session, cipher, seller.id)
    assert token == TOKEN + "2" and row.status == "ACTIVE" and row.last_error is None
    assert session.scalars(select(SellerCredential)).all().__len__() == 1


def test_unconnected_seller_and_delete(session, seller):
    cipher = CredentialCipher([Fernet.generate_key().decode()])
    with pytest.raises(CredentialError, match="not connected"):
        credentials.load_refresh_token(session, cipher, seller.id)
    credentials.save_refresh_token(session, cipher, seller.id, TOKEN, authorization_type="SELF")
    assert credentials.delete_credential(session, seller.id) is True
    assert credentials.delete_credential(session, seller.id) is False


def test_a_row_swapped_between_sellers_is_refused_end_to_end(session, seller):
    cipher = CredentialCipher([generate_key()])
    other = other_seller(session)
    credentials.save_refresh_token(session, cipher, seller.id, TOKEN, authorization_type="SELF")
    row = session.get(SellerCredential, seller.id)
    session.add(
        SellerCredential(
            seller_account_id=other.id,
            authorization_type="SELF",
            refresh_token_encrypted=row.refresh_token_encrypted,
        )
    )
    session.commit()
    with pytest.raises(CredentialError, match="does not belong"):
        credentials.load_refresh_token(session, cipher, other.id)
