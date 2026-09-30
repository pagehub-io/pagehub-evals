"""Step B of pagehub-auth's asymmetric access tokens (specs/asymmetric-access-tokens.md
§3.2, §3.8): pagehub-evals verifies by ``kid`` — pagehub-auth's Ed25519 key set
(``PAGEHUB_AUTH_JWKS``, EdDSA only) or, during the overlap, the legacy fleet
HS256 keys — refuses everything else with 401, boots fail-closed outside
development, reports ``jwks_kids`` on /health, and counts every legacy accept.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

from api.config import ConfigurationError, get_settings, reset_settings
from api.dependencies import _resolve_user, _verify_jwt
from api.tests._fleet_keys import (
    DEV_JWKS,
    DEV_KEY,
    DEV_KID,
    DEV_PUBLIC_X,
    OTHER_KEY,
    TEST_KEY,
    TEST_KID,
    b64url,
    jwk,
    jwks,
    public_x,
)

ISS = "http://localhost:8080"  # conftest's PAGEHUB_AUTH_ISSUER
LEGACY_KID, LEGACY_SECRET = "test-kid", "test-secret"  # conftest's JWT_SIGNING_KEYS
TOKEN_EVENT = "fleet_jwt_legacy_accept"


def _claims(**over) -> dict:
    now = int(time.time())
    claims = {
        "sub": "user-1",
        "app_slug": "pagehub-evals",
        "email": "support@pagehub.io",
        "iss": ISS,
        "iat": now,
        "exp": now + 600,
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def eddsa(key=TEST_KEY, kid=TEST_KID, **over) -> str:
    return jwt.encode(_claims(**over), key, algorithm="EdDSA", headers={"kid": kid})


def legacy(kid=LEGACY_KID, secret=LEGACY_SECRET, **over) -> str:
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(_claims(**over), secret, algorithm="HS256", headers=headers)


def hand_signed(header: dict, secret: bytes | None, **over) -> str:
    """A token PyJWT's encoder won't make (alg confusion, alg none)."""
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(_claims(**over)).encode())}"
    sig = b"" if secret is None else hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(sig)}"


def refused(token: str) -> None:
    with pytest.raises(HTTPException) as e:
        _verify_jwt(token)
    assert e.value.status_code == 401


def legacy_accepts() -> float:
    return REGISTRY.get_sample_value("fleet_jwt_legacy_accepts_total") or 0.0


def boot(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    reset_settings()
    return get_settings()


# ---- the fixtures themselves ------------------------------------------------


def test_vendored_dev_key_is_pagehub_auths():
    assert public_x(DEV_KEY) == DEV_PUBLIC_X
    assert json.loads(DEV_JWKS)["keys"][0]["x"] == DEV_PUBLIC_X


# ---- verification: the key is chosen by kid -----------------------------------


def test_eddsa_token_signed_by_a_key_in_the_set_is_accepted():
    assert _verify_jwt(eddsa())["sub"] == "user-1"


def test_eddsa_accept_is_not_counted_as_legacy(caplog):
    before = legacy_accepts()
    with caplog.at_level(logging.INFO):
        _verify_jwt(eddsa())
    assert legacy_accepts() == before
    assert not [r for r in caplog.records if TOKEN_EVENT in r.getMessage()]


def test_legacy_hs256_token_is_accepted_logged_and_counted(caplog):
    before = legacy_accepts()
    token = legacy()
    with caplog.at_level(logging.INFO):
        assert _verify_jwt(token)["sub"] == "user-1"
    assert legacy_accepts() == before + 1
    events = [r for r in caplog.records if r.getMessage().startswith(TOKEN_EVENT)]
    assert len(events) == 1 and events[0].levelno == logging.WARNING
    payload = json.loads(events[0].getMessage()[len(TOKEN_EVENT):])
    assert payload == {
        "app": "pagehub-evals",
        "stage": "test",
        "kid": LEGACY_KID,
        "iss": ISS,
        "app_slug": "pagehub-evals",
    }
    for record in caplog.records:
        text = record.getMessage()
        assert token not in text and token.split(".")[2] not in text
        assert "support@pagehub.io" not in text


def test_legacy_path_selects_the_secret_by_kid(monkeypatch):
    boot(monkeypatch, JWT_SIGNING_KEYS="k1:secret-one,k2:secret-two")
    assert _verify_jwt(legacy(kid="k2", secret="secret-two"))["sub"] == "user-1"
    # The right secret under the other kid is refused: no trying every key.
    refused(legacy(kid="k1", secret="secret-two"))


@pytest.mark.parametrize("kid", ["other-kid", "ed-test-9", "", DEV_KID])
def test_unknown_kid_is_refused(kid):
    refused(legacy(kid=kid))
    refused(eddsa(kid=kid))


def test_token_without_kid_is_refused():
    refused(legacy(kid=None))


@pytest.mark.parametrize("kid", [123, ["test-kid"], {"k": 1}, None])
def test_non_string_kid_is_refused_not_a_500(kid):
    # PyJWT's encoder won't emit these, so hand-sign them; an unhashable kid
    # used as a dict key would be a TypeError, i.e. a 500.
    refused(hand_signed({"alg": "HS256", "typ": "JWT", "kid": kid}, LEGACY_SECRET.encode()))


def _x_bytes() -> bytes:
    return TEST_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _pem() -> bytes:
    return TEST_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


@pytest.mark.parametrize(
    "secret",
    [lambda: _x_bytes(), lambda: public_x(TEST_KEY).encode(), lambda: _pem()],
    ids=["raw-x", "b64url-x", "pem"],
)
def test_alg_confusion_hs256_under_an_ed25519_kid_is_401_not_500(secret):
    # §3.2 / P3: with a mixed algorithms list PyJWT raises TypeError here,
    # which escapes PyJWTError handlers and becomes a 500.
    refused(hand_signed({"alg": "HS256", "typ": "JWT", "kid": TEST_KID}, secret()))


def test_eddsa_header_under_a_legacy_kid_is_refused():
    refused(eddsa(kid=LEGACY_KID))


@pytest.mark.parametrize("kid", [TEST_KID, LEGACY_KID])
def test_alg_none_is_refused(kid):
    refused(hand_signed({"alg": "none", "typ": "JWT", "kid": kid}, None))


@pytest.mark.parametrize("aud", ["pagehub-evals", ["pagehub-evals"], "pagehub-router"])
def test_token_carrying_aud_is_refused(aud):
    refused(eddsa(aud=aud))
    refused(legacy(aud=aud))


def test_wrong_issuer_is_refused():
    refused(eddsa(iss="https://evil.example"))
    refused(legacy(iss="https://evil.example"))


def test_eddsa_signed_by_another_key_under_a_known_kid_is_refused():
    refused(eddsa(key=OTHER_KEY))


def test_expired_token_is_refused():
    refused(eddsa(exp=int(time.time()) - 10))
    refused(legacy(exp=int(time.time()) - 10))


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "Bearer x"])
def test_garbage_is_refused(token):
    refused(token)


def test_user_resolution_keeps_slug_and_admin_rules():
    auth = asyncio.run(_resolve_user(f"Bearer {eddsa()}", None))
    assert (auth.actor_kind, auth.actor_id, auth.is_admin) == ("user", "user-1", True)
    auth = asyncio.run(_resolve_user(f"Bearer {legacy(email='someone@example.com')}", None))
    assert auth.is_admin is False
    with pytest.raises(HTTPException) as e:
        asyncio.run(_resolve_user(f"Bearer {eddsa(app_slug='prayers')}", None))
    assert e.value.status_code == 403


# ---- boot: fail-closed outside development ------------------------------------


@pytest.mark.parametrize("env", ["test", "staging", "preprod", "production", "qa"])
def test_missing_key_set_refuses_boot_outside_development(monkeypatch, env):
    with pytest.raises(ConfigurationError, match="PAGEHUB_AUTH_JWKS"):
        boot(monkeypatch, ENVIRONMENT=env, PAGEHUB_AUTH_JWKS=None)


def test_missing_key_set_is_allowed_in_development_legacy_only(monkeypatch):
    settings = boot(monkeypatch, ENVIRONMENT="development", PAGEHUB_AUTH_JWKS=None)
    assert settings.pagehub_auth_jwks == {}
    refused(eddsa())
    assert _verify_jwt(legacy())["sub"] == "user-1"


@pytest.mark.parametrize("env", ["test", "staging", "preprod", "production", "qa"])
def test_dev_key_set_refuses_boot_outside_development(monkeypatch, env):
    with pytest.raises(ConfigurationError, match="dev"):
        boot(monkeypatch, ENVIRONMENT=env, PAGEHUB_AUTH_JWKS=DEV_JWKS)


def test_dev_public_key_under_another_kid_refuses_boot(monkeypatch):
    doc = jwks({**jwk("ed-test-2", TEST_KEY), "x": DEV_PUBLIC_X})
    with pytest.raises(ConfigurationError, match="dev key"):
        boot(monkeypatch, PAGEHUB_AUTH_JWKS=doc)


def test_dev_key_set_works_in_development(monkeypatch):
    settings = boot(monkeypatch, ENVIRONMENT="development", PAGEHUB_AUTH_JWKS=DEV_JWKS)
    assert list(settings.pagehub_auth_jwks) == [DEV_KID]
    assert _verify_jwt(eddsa(key=DEV_KEY, kid=DEV_KID))["sub"] == "user-1"
    refused(eddsa())  # the test key isn't in the dev set


_GOOD = jwk("ed-test-2", OTHER_KEY)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "{}",
        '{"keys":"x"}',
        '{"keys":[]}',
        '{"keys":["x"]}',
        jwks({k: v for k, v in _GOOD.items() if k != "kid"}),
        jwks({**_GOOD, "kid": ""}),
        jwks({**_GOOD, "kty": "RSA"}),
        jwks({**_GOOD, "crv": "X25519"}),
        jwks({**_GOOD, "alg": "HS256"}),
        jwks({k: v for k, v in _GOOD.items() if k != "alg"}),
        jwks({**_GOOD, "d": "private"}),
        jwks({**_GOOD, "x": _GOOD["x"][:-1]}),
        jwks({**_GOOD, "x": _GOOD["x"] + "="}),
        jwks({**_GOOD, "x": "!" * 43}),
        jwks(_GOOD, _GOOD),
    ],
    ids=[
        "unparseable", "list", "no-keys", "keys-not-list", "empty", "key-not-object",
        "no-kid", "empty-kid", "kty", "crv", "alg", "no-alg", "private-d", "short-x",
        "padded-x", "bad-x", "duplicate-kid",
    ],
)
@pytest.mark.parametrize("env", ["test", "development"])
def test_malformed_key_set_refuses_boot_in_every_environment(monkeypatch, raw, env):
    with pytest.raises(ConfigurationError, match="PAGEHUB_AUTH_JWKS"):
        boot(monkeypatch, ENVIRONMENT=env, PAGEHUB_AUTH_JWKS=raw)


def test_key_set_kid_equal_to_a_legacy_kid_refuses_boot(monkeypatch):
    with pytest.raises(ConfigurationError, match="legacy"):
        boot(monkeypatch, PAGEHUB_AUTH_JWKS=jwks(jwk(LEGACY_KID, OTHER_KEY)))


@pytest.mark.parametrize("var", ["JWT_SIGNING_KEYS", "JWT_SIGNING_KEY"])
def test_legacy_kid_repeated_in_signing_keys_refuses_boot(monkeypatch, var):
    # The verifier picks a legacy secret by kid, so a repeated kid would drop one secret silently.
    env = {"JWT_SIGNING_KEYS": None, var: f"{LEGACY_KID}:{LEGACY_SECRET},{LEGACY_KID}:other-secret"}
    with pytest.raises(ConfigurationError, match=f"repeats kid '{LEGACY_KID}'") as e:
        boot(monkeypatch, **env)
    assert LEGACY_SECRET not in str(e.value) and "other-secret" not in str(e.value)


def test_two_keys_in_the_set_are_both_trusted(monkeypatch):
    boot(monkeypatch, PAGEHUB_AUTH_JWKS=jwks(jwk(TEST_KID, TEST_KEY), jwk("ed-test-2", OTHER_KEY)))
    assert _verify_jwt(eddsa())["sub"] == "user-1"
    assert _verify_jwt(eddsa(key=OTHER_KEY, kid="ed-test-2"))["sub"] == "user-1"
    refused(eddsa(key=OTHER_KEY, kid=TEST_KID))


# ---- /health and /metrics ----------------------------------------------------------


def test_health_reports_jwks_kids():
    from api.main import app

    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["jwks_kids"] == [TEST_KID]
    # The kid list is public; nothing else from the set is echoed.
    assert public_x(TEST_KEY) not in json.dumps(body)


def _scraped_legacy_accepts(client: TestClient) -> float:
    for family in text_string_to_metric_families(client.get("/metrics").text):
        for sample in family.samples:
            if sample.name == "fleet_jwt_legacy_accepts_total":
                return sample.value
    raise AssertionError("/metrics does not export fleet_jwt_legacy_accepts_total")


def test_metrics_counts_each_legacy_accept():
    from api.main import app

    with TestClient(app) as client:
        before = _scraped_legacy_accepts(client)
        _verify_jwt(legacy())
        assert _scraped_legacy_accepts(client) == before + 1


def test_decoded_signature_segment_helper_is_sane():
    # Guard for hand_signed(): an HS256 token it builds with the legacy secret verifies.
    token = hand_signed({"alg": "HS256", "typ": "JWT", "kid": LEGACY_KID}, LEGACY_SECRET.encode())
    assert _verify_jwt(token)["sub"] == "user-1"
