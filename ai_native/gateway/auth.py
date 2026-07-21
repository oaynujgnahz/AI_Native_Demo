from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


class AuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    user_id: str
    company_id: str
    role_id: str
    locale: str = "ja"


class JwtAuthenticator:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: Optional[str] = None,
        allow_audiences: Optional[list[str]] = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.allow_audiences = allow_audiences or []
        self.jwks_url = jwks_url or f"{self.issuer}/protocol/openid-connect/certs"
        self._jwks_client = None

    def authenticate(self, token: str) -> Principal:
        try:
            import jwt

            if self._jwks_client is None:
                self._jwks_client = jwt.PyJWKClient(self.jwks_url, cache_jwk_set=True)
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=[self.audience, *self.allow_audiences],
                options={"require": ["exp", "iat", "sub"]},
            )
        except Exception as exc:
            raise AuthenticationError("invalid_or_expired_token") from exc
        return _principal_from_claims(payload)


class ExplicitDemoAuthenticator:
    """Development-only authenticator enabled by an explicit environment flag."""

    def authenticate(self, token: str) -> Principal:
        expected = os.getenv("CMPF_AGENT_DEMO_TOKEN")
        if not expected:
            raise AuthenticationError("demo_token_not_configured")
        if token != expected:
            raise AuthenticationError("invalid_demo_token")
        return Principal(
            subject="demo-subject",
            user_id="demo-user",
            company_id=os.getenv("CMPF_AGENT_DEMO_COMPANY_ID", "cmpf-demo"),
            role_id="demo-readonly",
            locale=os.getenv("CMPF_AGENT_DEMO_LOCALE", "ja"),
        )


def build_authenticator_from_env():
    if os.getenv("CMPF_AGENT_DEMO_MODE", "false").lower() == "true":
        return ExplicitDemoAuthenticator()
    issuer = os.getenv("CMPF_KEYCLOAK_ISSUER", "")
    audience = os.getenv("CMPF_KEYCLOAK_AUDIENCE", "cmpf-agent-gateway")
    if not issuer:
        return _UnconfiguredAuthenticator()
    local_audiences = [
        value.strip()
        for value in os.getenv("CMPF_KEYCLOAK_ALLOWED_AUDIENCES", "CaM-app").split(",")
        if value.strip()
    ]
    return JwtAuthenticator(issuer, audience, allow_audiences=local_audiences)


class _UnconfiguredAuthenticator:
    def authenticate(self, token: str) -> Principal:
        raise AuthenticationError("keycloak_not_configured")


def _principal_from_claims(payload: Dict[str, Any]) -> Principal:
    try:
        return Principal(
            subject=str(payload["sub"]),
            user_id=str(payload["userId"]),
            company_id=str(payload["companyId"]),
            role_id=str(payload.get("roleId", "")),
            locale=_normalize_locale(payload.get("locale") or payload.get("lang")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("required_claim_missing") from exc


def _normalize_locale(value: Any) -> str:
    text = str(value or "ja").lower()
    return "en" if text.startswith("en") or text == "1" else "ja"
