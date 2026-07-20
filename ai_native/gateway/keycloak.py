from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass(frozen=True)
class KeycloakToken:
    access_token: str
    expires_in: int
    token_type: str = "Bearer"


class KeycloakAuthenticationError(Exception):
    def __init__(self, status_code: int, error: str, description: str = "") -> None:
        self.status_code = status_code
        self.error = error
        self.description = description
        message = error
        if description:
            message = f"{error}: {description}"
        super().__init__(message)


class KeycloakTokenClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        realm: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        http_client: Optional[Any] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("CMPF_KEYCLOAK_BASE_URL")
            or "https://authdev.carbon-management.ntt.com/auth"
        ).rstrip("/")
        self.realm = realm or os.getenv("CMPF_KEYCLOAK_REALM", "")
        self.client_id = client_id or os.getenv("CMPF_KEYCLOAK_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("CMPF_KEYCLOAK_CLIENT_SECRET", "")
        self.http_client = http_client or httpx.Client(timeout=10.0)

    def password_login(self, username: str, password: str) -> KeycloakToken:
        data = {
            "grant_type": "password",
            "client_id": self.client_id,
            "username": username,
            "password": password,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret

        response = self.http_client.post(
            self._token_url(),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._authentication_error(response, exc.response.status_code) from exc
        payload = response.json()
        return KeycloakToken(
            access_token=payload["access_token"],
            expires_in=int(payload.get("expires_in", 0)),
            token_type=payload.get("token_type", "Bearer"),
        )

    def _token_url(self) -> str:
        if not self.realm:
            raise ValueError("CMPF_KEYCLOAK_REALM is required")
        if not self.client_id:
            raise ValueError("CMPF_KEYCLOAK_CLIENT_ID is required")
        return f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/token"

    def _authentication_error(
        self,
        response: Any,
        status_code: int,
    ) -> KeycloakAuthenticationError:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        return KeycloakAuthenticationError(
            status_code=status_code,
            error=payload.get("error", "keycloak_auth_failed"),
            description=payload.get("error_description", ""),
        )
