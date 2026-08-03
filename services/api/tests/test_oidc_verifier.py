import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from loom_api.oidc import InvalidIdToken, OIDCVerifier

ISSUER = "http://loom.localhost/dex"
CLIENT_ID = "loom"


def make_keypair(kid: str = "test-key") -> tuple[RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private_key, {"keys": [jwk]}


def sign(private_key: RSAPrivateKey, kid: str = "test-key", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "CgRsb25n",
        "email": "long@loom.local",
        "name": "Long",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def verifier_for(jwks: dict[str, Any]) -> OIDCVerifier:
    async def fetch() -> dict[str, Any]:
        return jwks

    return OIDCVerifier(issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch)


async def test_valid_token_returns_claims() -> None:
    key, jwks = make_keypair()
    claims = await verifier_for(jwks).verify(sign(key))
    assert claims.subject == "CgRsb25n"
    assert claims.email == "long@loom.local"
    assert claims.display_name == "Long"


async def test_expired_token_is_rejected() -> None:
    key, jwks = make_keypair()
    now = int(time.time())
    token = sign(key, iat=now - 600, exp=now - 300)
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(token)


async def test_wrong_audience_is_rejected() -> None:
    key, jwks = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(key, aud="someone-else"))


async def test_wrong_issuer_is_rejected() -> None:
    key, jwks = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(key, iss="http://evil.example/dex"))


async def test_unknown_kid_is_rejected() -> None:
    key, jwks = make_keypair(kid="real-key")
    token = sign(key, kid="forged-key")
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(token)


async def test_token_signed_by_other_key_is_rejected() -> None:
    _, jwks = make_keypair()
    attacker_key, _ = make_keypair()
    with pytest.raises(InvalidIdToken):
        await verifier_for(jwks).verify(sign(attacker_key))


async def test_jwks_is_fetched_once_then_cached() -> None:
    key, jwks = make_keypair()
    calls = 0

    async def fetch() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return jwks

    verifier = OIDCVerifier(issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch)
    await verifier.verify(sign(key))
    await verifier.verify(sign(key))
    assert calls == 1


async def test_cache_is_refreshed_once_on_unknown_kid() -> None:
    """Dex xoay khoá — lần đầu gặp kid lạ phải nạp lại JWKS, không được fail ngay."""
    old_key, old_jwks = make_keypair(kid="old")
    new_key, new_jwks = make_keypair(kid="new")
    responses = [old_jwks, new_jwks]

    async def fetch() -> dict[str, Any]:
        return responses.pop(0) if len(responses) > 1 else responses[0]

    verifier = OIDCVerifier(issuer=ISSUER, client_id=CLIENT_ID, fetch_jwks=fetch)
    await verifier.verify(sign(old_key, kid="old"))
    claims = await verifier.verify(sign(new_key, kid="new"))
    assert claims.subject == "CgRsb25n"
