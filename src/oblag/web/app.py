from __future__ import annotations

from fastapi import FastAPI

from oblag import __version__
from oblag.db.session import init_db


def create_app() -> FastAPI:
    app = FastAPI(
        title="ObligationAggregator",
        version=__version__,
        description="Open-source regulatory & framework change-intelligence for GRC engineers.",
    )
    init_db()

    from oblag.web import api, html, internal, watchlists

    app.include_router(api.router)
    app.include_router(internal.router)
    app.include_router(watchlists.router)
    app.include_router(watchlists.rss_router)
    app.include_router(html.router)
    return app
