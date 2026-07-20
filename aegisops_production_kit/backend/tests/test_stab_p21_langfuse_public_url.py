"""STAB P2-1 — Traces deep-links use a browser-resolvable Langfuse origin.

Live (screenshot 3, 2026-07-13): the Traces tab deep-link opened `langfuse:3000/...` —
the compose-internal hostname — and the browser died with DNS_PROBE_FINISHED_NXDOMAIN.
Same fix family as KEYCLOAK_PUBLIC_URL: a browser-facing origin setting that wins over
the in-network host for every link a human clicks; server-side API calls keep the
in-network host.
"""

from __future__ import annotations

from app.integrations.langfuse_client import langfuse_browser_base
from app.settings import Settings


def test_public_origin_wins_for_browser_links():
    s = Settings(_env_file=None, langfuse_host="http://langfuse:3000",
                 langfuse_public_url="http://localhost:3001")
    assert langfuse_browser_base(s) == "http://localhost:3001"


def test_falls_back_to_host_outside_compose():
    s = Settings(_env_file=None, langfuse_host="http://localhost:3001", langfuse_public_url="")
    assert langfuse_browser_base(s) == "http://localhost:3001"


def test_trailing_slashes_never_double_up():
    s = Settings(_env_file=None, langfuse_public_url="http://localhost:3001/")
    assert langfuse_browser_base(s) == "http://localhost:3001"


def test_server_side_api_calls_keep_the_in_network_host():
    """The SDK client and health checks must keep using langfuse_host — only links a
    human clicks get the public origin (the split is the whole point)."""
    s = Settings(_env_file=None, langfuse_host="http://langfuse:3000",
                 langfuse_public_url="http://localhost:3001")
    assert s.langfuse_host == "http://langfuse:3000"
    assert langfuse_browser_base(s) != s.langfuse_host
