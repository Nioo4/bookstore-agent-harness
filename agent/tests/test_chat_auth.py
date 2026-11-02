from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.api.auth import AccessTokenVerifier


def verifier_and_key(tmp_path: Path) -> tuple[AccessTokenVerifier, Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path = tmp_path / "jwt-public.pem"
    path.write_bytes(public_pem)
    return AccessTokenVerifier(str(path), "bookstore-auth"), private_key


def token(private_key: Ed25519PrivateKey, **overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "bookstore-auth",
        "sub": "42",
        "iat": now,
        "exp": now + timedelta(hours=1),
        "email": "reader@example.com",
        "email_verified": True,
        **overrides,
    }
    return jwt.encode(claims, private_key, algorithm="EdDSA")


def test_access_token_verifier_accepts_go_compatible_eddsa_claims(tmp_path: Path) -> None:
    verifier, private_key = verifier_and_key(tmp_path)

    raw, user = verifier.verify_authorization("Bearer " + token(private_key))

    assert raw is not None
    assert user is not None
    assert user.user_id == 42
    assert user.email_verified is True


def test_access_token_verifier_allows_tourists_but_rejects_bad_tokens(tmp_path: Path) -> None:
    verifier, private_key = verifier_and_key(tmp_path)

    assert verifier.verify_authorization(None) == (None, None)
    with pytest.raises(ValueError, match="invalid authorization"):
        verifier.verify_authorization("Basic credentials")
    with pytest.raises(ValueError, match="invalid access token"):
        verifier.verify_authorization(
            "Bearer " + token(private_key, exp=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(ValueError, match="invalid access token"):
        verifier.verify_authorization(
            "Bearer "
            + jwt.encode(
                {
                    "iss": "bookstore-auth",
                    "sub": "42",
                    "iat": datetime.now(UTC),
                    "exp": datetime.now(UTC) + timedelta(hours=1),
                },
                "hmac-secret-that-is-at-least-32-bytes",
                algorithm="HS256",
            )
        )
