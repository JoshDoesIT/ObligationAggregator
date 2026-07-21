from __future__ import annotations

from fastapi import FastAPI

from oblag import __version__
from oblag.db.session import init_db, session_scope


def _seed_if_empty() -> None:
    """First boot on a fresh database ships the obligation catalog automatically."""
    from oblag.catalog import seed_obligations
    from oblag.db.models import Obligation

    with session_scope() as session:
        if session.query(Obligation.id).first() is None:
            seed_obligations(session)


def _provision_tenancy() -> None:
    """Ensure a default org exists and adopt any orphan (pre-tenancy) watchlists into
    it, so single-org deployments and upgrades keep working transparently (spec 07 §8)."""
    from oblag import auth
    from oblag.db.models import PrivateDocument, Watchlist

    with session_scope() as session:
        org = auth.get_default_org(session)
        session.query(Watchlist).filter(Watchlist.org_id.is_(None)).update(
            {Watchlist.org_id: org.id}, synchronize_session=False
        )
        session.query(PrivateDocument).filter(PrivateDocument.org_id.is_(None)).update(
            {PrivateDocument.org_id: org.id}, synchronize_session=False
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="ObligationAggregator",
        version=__version__,
        description="Open-source regulatory & framework change-intelligence for GRC engineers.",
    )
    init_db()
    _seed_if_empty()
    _provision_tenancy()

    from oblag.web import api, auth_routes, byol_routes, html, internal, watchlists

    app.include_router(api.router)
    app.include_router(internal.router)
    app.include_router(watchlists.router)
    app.include_router(watchlists.rss_router)
    app.include_router(auth_routes.router)
    app.include_router(byol_routes.router)
    app.include_router(html.router)
    return app
