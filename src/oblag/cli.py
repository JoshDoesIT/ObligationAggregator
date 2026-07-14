from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import typer

from oblag.db.session import init_db, session_scope

app = typer.Typer(
    help="ObligationAggregator — regulatory change intelligence.", no_args_is_help=True
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@app.command("init-db")
def init_db_cmd() -> None:
    """Create all database tables (idempotent)."""
    init_db()
    typer.echo("database initialized")


@app.command()
def adapters() -> None:
    """List available source adapters and whether they are enabled."""
    from oblag.adapters import available_adapters, get_adapter

    for name in available_adapters():
        enabled = get_adapter(name).enabled()
        typer.echo(f"{name:20s} {'enabled' if enabled else 'DISABLED (missing credentials)'}")


def _print_stats(stats) -> None:
    typer.echo(
        f"{stats.adapter}: pages={stats.pages} items={stats.items} "
        f"created={stats.created} events={len(stats.events)} errors={len(stats.errors)}"
    )
    for err in stats.errors:
        typer.secho(f"  error: {err}", fg="red")


@app.command("fetch-once")
def fetch_once(
    adapter: str,
    since_days: int = typer.Option(3, help="incremental lookback window in days"),
) -> None:
    """Run one adapter now (incremental)."""
    from oblag.core.runner import run_adapter

    init_db()
    with session_scope() as session:
        stats = run_adapter(session, adapter, since=datetime.now(UTC) - timedelta(days=since_days))
    _print_stats(stats)
    _dispatch()


@app.command()
def backfill(
    adapter: str,
    from_date: str = typer.Option(..., "--from", help="YYYY-MM-DD"),
    to_date: str = typer.Option(None, "--to", help="YYYY-MM-DD (default: today)"),
    agency: list[str] = typer.Option(None, help="agency slug filter (federal_register)"),
    window_days: int = typer.Option(365, help="split the range into windows of this size"),
) -> None:
    """Bounded historical backfill, windowed to respect source result caps."""
    from oblag.core.runner import run_adapter

    init_db()
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date) if to_date else datetime.now(UTC).date()
    params = {"agencies": agency} if agency else {}
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=window_days - 1), end)
        with session_scope() as session:
            stats = run_adapter(session, adapter, window=(cursor, stop), params=params)
        typer.echo(f"[{cursor} … {stop}]")
        _print_stats(stats)
        cursor = stop + timedelta(days=1)


@app.command()
def tick() -> None:
    """Apply time-based state transitions from stored dates (no fetch)."""
    from oblag.core.reducer import tick as run_tick

    init_db()
    with session_scope() as session:
        events = run_tick(session)
    typer.echo(f"{len(events)} time-based transition(s)")
    _dispatch()


@app.command("assert-date")
def assert_date_cmd(
    item_id: int,
    date_type: str,
    value: str,
    confidence: str = typer.Option("agency_estimate"),
    label: str = typer.Option(None),
    note: str = typer.Option(None, help="why this assertion is being made / source citation"),
) -> None:
    """Curated date assertion (e.g. a Unified Agenda projected-final date)."""
    from oblag.core.assertions import assert_date
    from oblag.db.models import Confidence, DateType

    init_db()
    with session_scope() as session:
        ev = assert_date(
            session,
            item_id,
            DateType(date_type),
            date.fromisoformat(value),
            Confidence(confidence),
            label=label,
            note=note,
        )
    typer.echo("no-op (same value)" if ev is None else f"date_changed event {ev.payload}")
    _dispatch()


@app.command()
def seed() -> None:
    """Load the shipped obligation catalog (idempotent upsert by slug)."""
    from oblag.catalog import seed_obligations

    init_db()
    with session_scope() as session:
        n = seed_obligations(session)
    typer.echo(f"{n} obligations upserted")


byol_app = typer.Typer(help="BYOL: local analysis of licensed standards you own.")
app.add_typer(byol_app, name="byol")


@byol_app.command("add")
def byol_add(
    obligation: str,
    version: str,
    file: str,
    attest_license: bool = typer.Option(
        False,
        "--attest-license",
        help="attest that you hold a license for this document",
    ),
) -> None:
    """Add a licensed document to the private store (never shared, never attested)."""
    from pathlib import Path

    from oblag.byol import ByolError, add_document

    init_db()
    try:
        with session_scope() as session:
            doc = add_document(
                session, obligation, version, Path(file), license_attested=attest_license
            )
            typer.echo(f"stored {obligation} {version} sha256={doc.sha256[:16]}… (private)")
    except ByolError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from None


@byol_app.command("diff")
def byol_diff(obligation: str, from_version: str, to_version: str) -> None:
    """Identifier-level diff between two BYOL versions, gated by display_policy."""
    import json

    from oblag.byol import ByolError, diff_versions

    init_db()
    try:
        with session_scope() as session:
            diff = diff_versions(session, obligation, from_version, to_version)
    except ByolError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from None
    typer.echo(f"display_policy: {diff.policy.value}")
    typer.echo(f"counts: {diff.counts}")
    if diff.added is not None:
        typer.echo("added:   " + json.dumps(diff.added))
        typer.echo("removed: " + json.dumps(diff.removed))
    else:
        typer.echo("(identifier lists withheld by display_policy=events_only)")


@app.command()
def keygen() -> None:
    """Generate the instance Ed25519 signing key (enables snapshot attestations)."""
    from oblag.config import get_settings
    from oblag.provenance import generate_keypair

    settings = get_settings()
    key_path = settings.signing_key_path or settings.data_dir / "keys" / "signing.pem"
    if key_path.exists():
        typer.secho(f"refusing to overwrite existing key at {key_path}", fg="red")
        raise typer.Exit(1)
    pub = generate_keypair(key_path)
    typer.echo(f"private key: {key_path}\npublic key:  {pub}")
    typer.echo("snapshot attestations are now enabled for future fetches")


@app.command("verify-snapshot")
def verify_snapshot(sha256: str) -> None:
    """Verify a snapshot's content hash and DSSE attestation."""
    import hashlib
    import json

    from oblag.config import get_settings
    from oblag.db.models import Snapshot
    from oblag.provenance import verify_envelope
    from oblag.snapshots import SnapshotStore

    init_db()
    settings = get_settings()
    store = SnapshotStore(settings.snapshot_dir)
    with session_scope() as session:
        snap = session.query(Snapshot).filter_by(sha256=sha256).one_or_none()
        if snap is None:
            typer.secho("no snapshot with that digest", fg="red")
            raise typer.Exit(1)
        content = store.read(sha256)
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256:
            typer.secho(f"CONTENT MISMATCH: stored file hashes to {actual}", fg="red")
            raise typer.Exit(2)
        typer.echo(f"content hash OK ({len(content)} bytes from {snap.source_url})")
        if not snap.attestation_ref:
            typer.echo("no attestation recorded (snapshot pre-dates keygen)")
            return
        envelope = json.loads((store.root / snap.attestation_ref).read_text())
        key_path = settings.signing_key_path or settings.data_dir / "keys" / "signing.pem"
        statement = verify_envelope(envelope, key_path.with_suffix(".pub").read_bytes())
        subject = statement["subject"][0]
        if subject["digest"]["sha256"] != sha256:
            typer.secho("ATTESTATION MISMATCH: subject digest differs", fg="red")
            raise typer.Exit(2)
        typer.echo(f"attestation OK: signed statement for {subject['name']}")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    with_scheduler: bool = typer.Option(False, help="run the polling scheduler in-process"),
) -> None:
    """Run the web UI + API (and optionally the scheduler)."""
    import uvicorn

    from oblag.web.app import create_app

    app_ = create_app()
    if with_scheduler:
        from oblag.scheduler import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        typer.echo(f"scheduler started with {len(scheduler.get_jobs())} job(s)")
    uvicorn.run(app_, host=host, port=port)


def _dispatch() -> None:
    from oblag.notify import dispatch_pending

    with session_scope() as session:
        n = dispatch_pending(session)
    if n:
        typer.echo(f"{n} notification(s) dispatched")


if __name__ == "__main__":
    app()
