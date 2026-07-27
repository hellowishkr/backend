from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import cards
from app.core.config import settings


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


async def _allow(_: str) -> bool:
    return True


def _client(monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(cards.router, prefix="/api")
    monkeypatch.setattr(cards._cards_rate_limiter, "allow", _allow)
    return TestClient(app)


def test_cards_sso_token_requires_sdk_config(monkeypatch):
    monkeypatch.setattr(settings, "hubble_sdk_enabled", False)

    response = _client(monkeypatch).post(
        "/api/cards/sso-token",
        json={"name": "Karan", "email": "karan@example.com"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Gift card store is not configured"}


def test_cards_sso_token_signs_minimal_claims(monkeypatch):
    private_key = _private_key_pem()
    monkeypatch.setattr(settings, "hubble_sdk_enabled", True)
    monkeypatch.setattr(settings, "hubble_sdk_client_id", "client_staging_123")
    monkeypatch.setattr(settings, "hubble_sdk_app_secret", "secret_staging_123")
    monkeypatch.setattr(settings, "hubble_sdk_private_key", private_key)
    monkeypatch.setattr(settings, "hubble_sdk_env", "staging")
    monkeypatch.setattr(settings, "hubble_sdk_staging_base_url", "https://sdk.dev.myhubble.money")
    settings.__dict__.pop("hubble_sdk_effective_base_url", None)

    response = _client(monkeypatch).post(
        "/api/cards/sso-token",
        headers={"x-forwarded-for": "203.0.113.10"},
        json={"name": "  Karan  ", "email": "KARAN@example.com", "phone": "9876543210"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "sdkBaseUrl", "clientId"}
    assert body["sdkBaseUrl"] == "https://sdk.dev.myhubble.money"
    assert body["clientId"] == "client_staging_123"

    public_key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
    public_pem = public_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    claims = jwt.decode(
        body["token"],
        public_pem,
        algorithms=["RS256"],
        issuer="client_staging_123",
    )

    assert claims["sub"].startswith("hbl_")
    assert claims["name"] == "Karan"
    assert claims["email"] == "karan@example.com"
    assert claims["phoneNumber"] == "9876543210"
    assert claims["exp"] - claims["iat"] == 60
    assert claims["iat"] <= int(datetime.now(timezone.utc).timestamp())


def test_cards_sso_token_requires_email_or_phone(monkeypatch):
    monkeypatch.setattr(settings, "hubble_sdk_enabled", True)

    response = _client(monkeypatch).post(
        "/api/cards/sso-token",
        json={"name": "Karan"},
    )

    assert response.status_code == 422


def test_cards_sso_token_rejects_country_code_phone(monkeypatch):
    monkeypatch.setattr(settings, "hubble_sdk_enabled", True)

    response = _client(monkeypatch).post(
        "/api/cards/sso-token",
        json={"name": "Karan", "phone": "+919876543210"},
    )

    assert response.status_code == 422


def test_cards_sso_token_can_use_mock_token_without_private_key(monkeypatch):
    monkeypatch.setattr(settings, "hubble_sdk_enabled", True)
    monkeypatch.setattr(settings, "hubble_sdk_client_id", "client_staging_123")
    monkeypatch.setattr(settings, "hubble_sdk_app_secret", "secret_staging_123")
    monkeypatch.setattr(settings, "hubble_sdk_private_key", "")
    monkeypatch.setattr(settings, "hubble_sdk_mock_token", "mock-token")

    response = _client(monkeypatch).post(
        "/api/cards/sso-token",
        json={"name": "Karan", "email": "karan@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["token"] == "mock-token"
