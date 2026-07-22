from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from oblag import __version__
from oblag.db.session import init_db, session_scope

log = logging.getLogger(__name__)

_BOOT_STAMP_KEY = "boot_version"


def _preview_env() -> bool:
    """A Vercel PREVIEW deployment. Previews may point at the production database, so
    they must not run the mutating boot steps unless explicitly allowed (a preview with
    its own Neon branch sets OBLAG_ALLOW_PREVIEW_BOOT_WRITES=true). Guards against a
    branch's boot code silently editing prod data (observed live)."""
    from oblag.config import get_settings

    if os.environ.get("VERCEL_ENV") != "preview":
        return False
    return not get_settings().allow_preview_boot_writes


def _boot_done_for_this_version() -> bool:
    """True when this deployment version already ran its boot work — the fast path for
    warm cold starts: a single SELECT instead of ~30 migration/sync round trips.
    Missing table / different version / any error → run the full boot."""

    from oblag.db.models import KVMeta

    try:
        with session_scope() as session:
            row = session.get(KVMeta, _BOOT_STAMP_KEY)
            return row is not None and row.value == __version__
    except Exception:  # noqa: BLE001 — table absent on a fresh DB, or DB unreachable
        return False


def _stamp_boot() -> None:
    from oblag.db.models import KVMeta, utcnow

    with session_scope() as session:
        row = session.get(KVMeta, _BOOT_STAMP_KEY)
        if row is None:
            session.add(KVMeta(key=_BOOT_STAMP_KEY, value=__version__))
        else:
            row.value = __version__
            row.updated_at = utcnow()


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


def _repair_data() -> None:
    """Purge rows produced by since-fixed parser defects (oblag.maintenance) so live
    deployments heal on deploy. Idempotent; no-op once the rows are gone."""
    from oblag.maintenance import complete_concluded_consultations, purge_known_bad

    with session_scope() as session:
        purge_known_bad(session)
        complete_concluded_consultations(session)


def _seed_milestones() -> None:
    """Curated milestone timelines (act application dates no feed carries). Runs
    through the reducer, so re-seeding is idempotent and edits supersede cleanly."""
    from oblag.milestones import seed_milestones

    with session_scope() as session:
        seed_milestones(session)


def create_app() -> FastAPI:
    app = FastAPI(
        title="ObligationAggregator",
        version=__version__,
        description="Open-source regulatory & framework change-intelligence for GRC engineers.",
    )
    # Boot work is idempotent but costs ~30 DB round trips (migrations, catalog sync,
    # tenancy, repairs, milestones). Run it once per deployment version; every other
    # warm cold-start takes the 1-query fast path. init_db (create_all + additive
    # migrations) is safe and cheap-ish, but still gated so warm starts skip reflection.
    if not _boot_done_for_this_version():
        init_db()
        if _preview_env():
            log.info("preview deployment: skipping mutating boot steps")
        else:
            _sync_catalog()
            _provision_tenancy()
            _repair_data()
            _seed_milestones()
            _stamp_boot()

    from oblag.web import api, auth_routes, byol_routes, html, internal, watchlists

    app.include_router(api.router)
    app.include_router(internal.router)
    app.include_router(watchlists.router)
    app.include_router(watchlists.rss_router)
    app.include_router(auth_routes.router)
    app.include_router(byol_routes.router)
    app.include_router(html.router)
    return app
