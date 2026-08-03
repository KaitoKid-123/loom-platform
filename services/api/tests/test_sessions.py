import pytest
from itsdangerous import BadSignature

from loom_api.sessions import CookieSigner, generate_pkce_pair

SECRET = "unit-test-secret"


def test_pkce_verifier_and_challenge_differ() -> None:
    verifier, challenge = generate_pkce_pair()
    assert verifier != challenge
    assert len(verifier) >= 43
    assert "=" not in challenge  # base64url không đệm


def test_pkce_pair_is_random_each_time() -> None:
    assert generate_pkce_pair()[0] != generate_pkce_pair()[0]


def test_challenge_is_stable_for_a_given_verifier() -> None:
    from loom_api.sessions import challenge_for

    verifier, challenge = generate_pkce_pair()
    assert challenge_for(verifier) == challenge


def test_signer_roundtrip() -> None:
    signer = CookieSigner(SECRET, salt="tx")
    token = signer.dumps({"state": "abc", "verifier": "xyz"})
    assert signer.loads(token, max_age=600) == {"state": "abc", "verifier": "xyz"}


def test_signer_rejects_tampered_value() -> None:
    signer = CookieSigner(SECRET, salt="tx")
    token = signer.dumps({"state": "abc"})
    with pytest.raises(BadSignature):
        signer.loads(token[:-2] + "xx", max_age=600)


def test_signer_rejects_other_secret() -> None:
    token = CookieSigner(SECRET, salt="tx").dumps({"state": "abc"})
    with pytest.raises(BadSignature):
        CookieSigner("different-secret", salt="tx").loads(token, max_age=600)


def test_signer_salts_are_isolated() -> None:
    token = CookieSigner(SECRET, salt="tx").dumps({"state": "abc"})
    with pytest.raises(BadSignature):
        CookieSigner(SECRET, salt="session").loads(token, max_age=600)
