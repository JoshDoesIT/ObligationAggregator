"""Where this instance lives, for links that leave the page.

A URL we hand to someone — an RSS feed to paste into a reader, a social card, a
canonical link — has to be reachable from outside. `base_url` defaults to
http://localhost:8000, which is right for `oblag serve` and wrong for every deployment
that never sets OBLAG_BASE_URL. Vercel is one of those, and its watchlist feeds were
being shown as localhost URLs.

So: the configured base_url wins when it has been set to something real, and otherwise
we use the origin the request actually arrived on, which is by definition reachable.
"""

from __future__ import annotations

from fastapi import Request


def site_base(request: Request | None = None) -> str:
    """Absolute origin for links handed to a user. Falls back to the request's own
    origin when base_url is still the localhost default."""
    from oblag.config import get_settings

    base = get_settings().base_url.rstrip("/")
    if base and "localhost" not in base and "127.0.0.1" not in base:
        return base
    if request is not None:
        return str(request.base_url).rstrip("/")
    return base
