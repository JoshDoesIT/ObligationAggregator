from __future__ import annotations

from fastapi import FastAPI

from oblag import __version__
from oblag.db.session import init_db, session_scope


def _sync_catalog() -> None:
    """Keep the database's obligation catalog matching the shipped one: the catalog in
    code is authoritative, so any drift — new slugs, new fields, or edited values (e.g.
    a current_version bump when a standards body publishes) — re-upserts on boot. One
    50-row SELECT per cold start; the upsert itself only runs when something changed."""
    from oblag.catalog import CATALOG, seed_obligations
    from oblag.db.models import Obligation

    with session_scope() as session:
        rows = {o.slug: o for o in session.query(Obligation).all()}
        drift = any(
            (row := rows.get(entry["slug"])) is None
            or any(getattr(row, field) != value for field, value in entry.items())
            for entry in CATALOG
        )
        if drift:
            # A catalog current_version edit is the always-wins override for an
            # auto-detected version: clear the auto value so the corrected baseline
            # takes effect (otherwise effective = max(baseline, auto) would keep a
            # wrong auto value). Only for obligations whose baseline actually changed.
            for entry in CATALOG:
                row = rows.get(entry["slug"])
                if row is not None and row.current_version != entry.get("current_version"):
                    row.confirmed_version = None
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
