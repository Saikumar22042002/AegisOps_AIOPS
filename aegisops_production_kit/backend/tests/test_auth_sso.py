"""Keycloak SSO (Auth Code + PKCE) — 2026-07-06 fix regression guards.

Root causes fixed:
  1. The authorization redirect was built from discovery fetched over the CONTAINER network,
     so the browser was sent to http://keycloak:8080 — a Docker service name it cannot
     resolve. `build_auth_url` now rewrites the origin to the browser-facing Keycloak host
     (KEYCLOAK_PUBLIC_URL), keeping path/params intact.
  2. Keycloak derives a token's `iss` from the URL the auth request came through, so SSO
     tokens carry the browser origin while password-grant tokens carry the internal one.
     `validate()` accepts exactly those two known realm URLs — nothing else.

The full round-trip (login form → code → callback → session cookie → /auth/me) is covered
by `test_sso_round_trip_live`, which drives the real Keycloak + API from the HOST (both
hosts resolvable there). In-container runs skip it; run manually with:
    AEGISOPS_TEST_SSO_LIVE=1 pytest tests/test_auth_sso.py -k live   (from backend/, host)
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

import pytest

from app.integrations import keycloak as kc
from app.integrations.keycloak import AuthError, KeycloakOIDC, generate_pkce
from app.settings import Settings

_INTERNAL = "http://keycloak:8080"
_BROWSER = "http://localhost:8080"


def _oidc(public_url: str = "") -> KeycloakOIDC:
    settings = Settings(keycloak_url=_INTERNAL, keycloak_public_url=public_url,
                        keycloak_realm="aegisops", _env_file=None)
    oidc = KeycloakOIDC(settings)
    # Discovery as the API container sees it: endpoints carry the INTERNAL host.
    oidc._discovery = {
        "authorization_endpoint": f"{_INTERNAL}/realms/aegisops/protocol/openid-connect/auth",
        "token_endpoint": f"{_INTERNAL}/realms/aegisops/protocol/openid-connect/token",
    }
    return oidc


async def test_auth_url_uses_browser_reachable_host():
    oidc = _oidc(public_url=_BROWSER)
    verifier, challenge = generate_pkce()
    url = await oidc.build_auth_url("http://localhost:8000/auth/callback", "state-1", challenge)
    parts = urlsplit(url)
    assert parts.netloc == "localhost:8080", "the browser is sent here — never a docker service name"
    assert parts.path == "/realms/aegisops/protocol/openid-connect/auth"
    q = parse_qs(parts.query)
    assert q["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [challenge]
    assert q["response_type"] == ["code"]


async def test_auth_url_unchanged_without_public_url():
    """Host-run dev: keycloak_url is already browser-reachable; no rewrite happens."""
    oidc = _oidc(public_url="")
    _, challenge = generate_pkce()
    url = await oidc.build_auth_url("http://localhost:8000/auth/callback", "s", challenge)
    assert urlsplit(url).netloc == "keycloak:8080"


@pytest.mark.parametrize("iss,ok", [
    (f"{_INTERNAL}/realms/aegisops", True),    # password grant (API-side token request)
    (f"{_BROWSER}/realms/aegisops", True),     # SSO (browser-side auth request)
    ("http://evil.example/realms/aegisops", False),
    (f"{_INTERNAL}/realms/other-realm", False),
])
async def test_validate_accepts_exactly_the_two_known_issuers(monkeypatch, iss, ok):
    oidc = _oidc(public_url=_BROWSER)

    async def fake_signing_key(token):
        return "test-key"

    monkeypatch.setattr(oidc, "_signing_key", fake_signing_key)
    monkeypatch.setattr(kc.jwt, "decode", lambda *a, **k: {"iss": iss, "exp": 1, "iat": 1})
    if ok:
        claims = await oidc.validate("token")
        assert claims["iss"] == iss
    else:
        with pytest.raises(AuthError, match="issuer"):
            await oidc.validate("token")


def test_browser_url_falls_back_to_internal():
    s = Settings(keycloak_url=_INTERNAL, keycloak_public_url="", _env_file=None)
    assert s.keycloak_browser_url == _INTERNAL
    s2 = Settings(keycloak_url=_INTERNAL, keycloak_public_url=_BROWSER + "/", _env_file=None)
    assert s2.keycloak_browser_url == _BROWSER


# ── live round-trip (host-only; browser-equivalent OIDC dance) ────────────────


def test_sso_round_trip_live():
    """/auth/sso/login → Keycloak login form → POST credentials → /auth/callback →
    session cookie → /auth/me 200 → logout → 401. Stdlib-only so it runs on a bare host."""
    if os.getenv("AEGISOPS_TEST_SSO_LIVE") != "1":
        pytest.skip("host-only live SSO test: set AEGISOPS_TEST_SSO_LIVE=1 with the stack up")
    import http.cookiejar
    import json
    import re
    import urllib.error
    import urllib.parse
    import urllib.request

    api = os.getenv("AEGISOPS_TEST_API_BASE", "http://localhost:8000")
    user, password = "maya.okafor@northwind.com", "aegisops"

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    noredir = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))

    def expect_302(url, data=None):
        try:
            noredir.open(urllib.request.Request(url, data=data))
        except urllib.error.HTTPError as e:
            assert e.code == 302, f"{url} → {e.code}"
            return e.headers["Location"]
        raise AssertionError(f"expected 302 from {url}")

    auth_url = expect_302(f"{api}/auth/sso/login")
    assert "code_challenge_method=S256" in auth_url
    assert "keycloak:" not in urllib.parse.urlsplit(auth_url).netloc, "docker host leaked to browser"

    html = opener.open(auth_url).read().decode()
    action = re.search(r'action="([^"]+)"', html).group(1).replace("&amp;", "&")
    form = urllib.parse.urlencode({"username": user, "password": password, "credentialId": ""}).encode()
    callback_url = expect_302(action, data=form)
    assert callback_url.startswith(f"{api}/auth/callback") and "code=" in callback_url

    expect_302(callback_url)  # code → tokens → session cookie → 302 to the app
    me = json.loads(opener.open(f"{api}/auth/me").read().decode())
    assert me["user"]["username"], "no authenticated user after SSO"

    opener.open(urllib.request.Request(f"{api}/auth/logout", data=b"", method="POST"))
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(f"{api}/auth/me")
    assert exc.value.code == 401
