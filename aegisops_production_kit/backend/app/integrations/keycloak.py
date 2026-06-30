"""Real Keycloak OIDC client.

Implements OIDC discovery, JWKS-based JWT validation, the Authorization-Code + PKCE
flow (SSO button), the Resource-Owner-Password flow (the design's email/password form,
via Keycloak Direct Access Grants), token refresh, and logout. No tokens are fabricated;
every call hits the real Keycloak in `docker-compose`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from tenacity import retry, stop_after_attempt, wait_exponential

from ..logging_conf import get_logger
from ..settings import Settings

log = get_logger(__name__)


class AuthError(Exception):
    """Raised on any authentication/validation failure."""


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int
    obtained_at: float

    @property
    def expires_at(self) -> float:
        return self.obtained_at + self.expires_in

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=int(data.get("expires_in", 300)),
            obtained_at=time.time(),
        )


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge[S256])."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class KeycloakOIDC:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0

    # ── discovery / keys ──
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            url = f"{self.s.keycloak_realm_url}/.well-known/openid-configuration"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                self._discovery = resp.json()
        return self._discovery

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4))
    async def jwks(self) -> dict[str, Any]:
        # Refresh keys at most every 10 minutes (covers key rotation).
        if self._jwks is None or (time.time() - self._jwks_fetched_at) > 600:
            disc = await self.discovery()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(disc["jwks_uri"])
                resp.raise_for_status()
                self._jwks = resp.json()
                self._jwks_fetched_at = time.time()
        return self._jwks

    async def _signing_key(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        jwks = await self.jwks()
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return RSAAlgorithm.from_jwk(json.dumps(key))
        # Key not found — force a refresh once (rotation may have just happened).
        self._jwks = None
        jwks = await self.jwks()
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return RSAAlgorithm.from_jwk(json.dumps(key))
        raise AuthError("No matching JWKS key for token")

    async def validate(self, access_token: str) -> dict[str, Any]:
        """Verify signature, issuer, and expiry; return claims."""
        try:
            key = await self._signing_key(access_token)
            claims = jwt.decode(
                access_token,
                key,
                algorithms=["RS256"],
                issuer=self.s.keycloak_realm_url,
                options={"verify_aud": False, "require": ["exp", "iat", "iss"]},
            )
            return claims
        except jwt.PyJWTError as exc:
            raise AuthError(f"Invalid token: {exc}") from exc

    # ── token acquisition ──
    async def _token_request(self, data: dict[str, str]) -> TokenSet:
        disc = await self.discovery()
        data.setdefault("client_id", self.s.keycloak_client_id)
        if self.s.keycloak_client_secret:
            data.setdefault("client_secret", self.s.keycloak_client_secret)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(disc["token_endpoint"], data=data)
        if resp.status_code != 200:
            detail = resp.json().get("error_description", resp.text) if resp.content else resp.text
            raise AuthError(f"Token request failed: {detail}")
        return TokenSet.from_response(resp.json())

    async def password_grant(self, username: str, password: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": "openid profile email roles",
            }
        )

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def build_auth_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        disc = await self.discovery()
        from urllib.parse import urlencode

        params = {
            "client_id": self.s.keycloak_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email roles",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{disc['authorization_endpoint']}?{urlencode(params)}"

    async def logout(self, refresh_token: str) -> None:
        disc = await self.discovery()
        end_session = disc.get("end_session_endpoint")
        if not end_session:
            return
        data = {"client_id": self.s.keycloak_client_id, "refresh_token": refresh_token}
        if self.s.keycloak_client_secret:
            data["client_secret"] = self.s.keycloak_client_secret
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(end_session, data=data)
            except httpx.HTTPError as exc:
                log.warning("keycloak.logout_failed", error=str(exc))


_client: KeycloakOIDC | None = None


def get_oidc(settings: Settings) -> KeycloakOIDC:
    global _client
    if _client is None:
        _client = KeycloakOIDC(settings)
    return _client
