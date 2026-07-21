from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import respx
from httpx import Response

from oblag.adapters.base import NormalizedDate, NormalizedItem
from oblag.core.reducer import reduce_item
from oblag.db.models import Confidence, DateType, NotificationLog, Watchlist
from oblag.notify import dispatch_pending


@pytest.fixture(autouse=True)
def _allow_example_webhooks(monkeypatch):
    # these tests mock the HTTP layer with respx; the SSRF guard's real DNS lookup of
    # example.com is not what's under test here, so neutralize it.
    import oblag.netguard as ng

    monkeypatch.setattr(ng, "assert_safe_url", lambda url: None)


@pytest.fixture()
def circia_item(db):
    res = reduce_item(
        db,
        NormalizedItem(
            source_system="federal_register",
            external_key=("fr_doc_number", "2024-06526"),
            jurisdiction="US-Federal",
            title="CIRCIA Reporting Requirements",
            native_status="PRORULE",
            track="proposed",
            dates=[
                NormalizedDate(DateType.comment_close, date(2099, 6, 3), Confidence.published_firm)
            ],
        ),
        today=date(2024, 5, 1),
    )
    db.commit()
    return res.item


def _watchlist(db, channel="webhook", target="https://hooks.example.com/x", **filters):
    wl = Watchlist(
        name="test",
        channel=channel,
        target=target,
        filters=filters,
        active=True,
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add(wl)
    db.commit()
    return wl


def test_webhook_dispatch_and_at_most_once(db, circia_item):
    _watchlist(db, event_types=["item_created", "date_changed", "state_changed"])
    with respx.mock() as mock:
        route = mock.post("https://hooks.example.com/x").mock(return_value=Response(200))
        n = dispatch_pending(db)
        assert n == 2  # item_created + state_changed
        assert route.call_count == 1  # batched into one POST
        # second run: nothing new
        assert dispatch_pending(db) == 0
        assert route.call_count == 1


def test_failed_delivery_is_retried_next_run(db, circia_item):
    _watchlist(db)
    with respx.mock() as mock:
        mock.post("https://hooks.example.com/x").mock(return_value=Response(500))
        assert dispatch_pending(db) == 0
        assert db.query(NotificationLog).count() == 0  # not logged → retried
    with respx.mock() as mock:
        mock.post("https://hooks.example.com/x").mock(return_value=Response(200))
        assert dispatch_pending(db) == 2


def test_filters_scope_delivery(db, circia_item):
    _watchlist(db, source_systems=["nist_csrc"])  # wrong source → no match
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://hooks.example.com/x").mock(return_value=Response(200))
        assert dispatch_pending(db) == 0
        assert route.call_count == 0


def test_email_without_smtp_is_not_fatal(db, circia_item):
    _watchlist(db, channel="email", target="grc@example.com")
    assert dispatch_pending(db) == 0  # smtp unconfigured → retry later, no crash


def test_email_delivery(db, circia_item, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, msg):
            sent["subject"] = msg["Subject"]
            sent["to"] = msg["To"]
            sent["body"] = msg.get_content()

    monkeypatch.setenv("OBLAG_SMTP_HOST", "smtp.example.com")
    from oblag.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    _watchlist(db, channel="email", target="grc@example.com")
    assert dispatch_pending(db) == 2
    assert sent["to"] == "grc@example.com"
    assert "CIRCIA" in sent["body"]
    assert "2 change event(s)" in sent["subject"]
