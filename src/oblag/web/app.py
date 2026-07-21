from __future__ import annotations

from fastapi import FastAPI

from oblag import __version__
from oblag.db.session import init_db, session_scope


def _sync_catalog() -> None:
    """Ship the obligation catalog on first boot, and re-upsert it when the deployed
    code carries catalog rows or fields the database hasn't seen (new slugs, or
    current_version introduced in v0.4.2). Two scalar probes keep cold starts cheap;
    in-place VALUE edits to existing fields still need /api/internal/seed."""
    from sqlalchemy import func

    from oblag.catalog import CATALOG, seed_obligations
    from oblag.db.models import Obligation

    with session_scope() as session:
        db_slugs = session.query(func.count(Obligation.id)).scalar() or 0
        db_versioned = (
            session.query(func.count(Obligation.id))
            .filter(Obligation.current_version.isnot(None))
            .scalar()
            or 0
        )
        want_versioned = sum(1 for e in CATALOG if e.get("current_version"))
        if db_slugs < len(CATALOG) or db_versioned < want_versioned:
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
    _sync_catalog()
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
