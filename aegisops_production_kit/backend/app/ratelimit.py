"""Shared per-IP rate limiter (slowapi).

Kept in its own module so `main.py` (which registers `SlowAPIMiddleware`) and the SSE
endpoints in `api/chat.py` (which mark themselves exempt via `@limiter.exempt`) import the
*same* `Limiter` instance without a circular import (main imports the chat router, so chat
must not import main).

O3: streaming endpoints are exempt from the default limit. An SSE connection is long-lived and
the client reconnects with Last-Event-ID after any drop; counting each (re)connection against a
per-minute request budget would throttle normal streaming and sever live runs.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from .settings import get_settings

_settings = get_settings()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{_settings.rate_limit_per_minute}/minute"],
)
