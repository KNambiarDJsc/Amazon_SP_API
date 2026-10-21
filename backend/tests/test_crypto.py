import uuid

import pytest
from support import make_settings

from app.crypto import (
    CredentialBox,
    CredentialDecryptError,
    CredentialKeyError,
    generate_key,
)

SELLER = uuid.uuid4()
OTHER = uuid.uuid4()


def test_round_trip_and_ciphertext_does_not_contain_the_secret():
    box = CredentialBox([generate_key()])
    sealed = box.seal(SELLER, "Atzr|secret-token")
    assert "Atzr" not in sealed and "secret-token" not in sealed
    assert box.open(SELLER, sealed) == "Atzr|secret-token"


def test_a_ciphertext_moved_to_another_seller_does_not_open():
    box = CredentialBox([generate_key()])
    sealed = box.seal(SELLER, "token")
    with pytest.raises(CredentialDecryptError, match="different seller"):
        box.open(OTHER, sealed)


def test_tampering_is_detected():
    box = CredentialBox([generate_key()])
    sealed = box.seal(SELLER, "token")
    tampered = sealed[:-6] + ("A" if sealed[-6] != "A" else "B") + sealed[-5:]
    with pytest.raises(CredentialDecryptError):
        box.open(SELLER, tampered)


def test_wrong_key_cannot_open():
    sealed = CredentialBox([generate_key()]).seal(SELLER, "token")
    with pytest.raises(CredentialDecryptError):
        CredentialBox([generate_key()]).open(SELLER, sealed)


def test_key_rotation_old_tokens_still_open_and_rotate_moves_them_to_the_new_key():
    old, new = generate_key(), generate_key()
    sealed_old = CredentialBox([old]).seal(SELLER, "token")
    rotating = CredentialBox([new, old])
    assert rotating.open(SELLER, sealed_old) == "token"

    rotated = rotating.rotate(sealed_old)
    assert CredentialBox([new]).open(SELLER, rotated) == "token"  # old key no longer needed


@pytest.mark.parametrize("raw", [None, "", " , ", "not-a-fernet-key"])
def test_missing_or_malformed_keys_are_refused(raw):
    with pytest.raises(CredentialKeyError):
        CredentialBox.from_settings(make_settings(credentials_encryption_keys=raw))


def test_settings_accept_comma_separated_keys():
    a, b = generate_key(), generate_key()
    box = CredentialBox.from_settings(make_settings(credentials_encryption_keys=f"{a}, {b}"))
    assert CredentialBox([a]).open(SELLER, box.seal(SELLER, "x")) == "x"  # first key encrypts
