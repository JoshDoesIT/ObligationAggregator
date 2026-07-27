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


def _settings(monkeypatch, **overrides):
    """Point get_settings at a config with these values, for every module that reads it."""
    from oblag import config, notify

    base = config.Settings(**overrides)
    monkeypatch.setattr(notify, "get_settings", lambda: base)
    return base


def test_email_is_disabled_until_a_backend_is_configured(monkeypatch):
    from oblag import notify

    _settings(monkeypatch)
    assert notify.mail_backend() is None
    assert notify.email_enabled() is False


def test_auto_prefers_resend_because_a_verified_domain_is_the_point(monkeypatch):
    from oblag import notify

    _settings(monkeypatch, resend_api_key="re_test", smtp_host="smtp.gmail.com")
    assert notify.mail_backend() == "resend"

    _settings(monkeypatch, smtp_host="smtp.gmail.com")
    assert notify.mail_backend() == "smtp"


def test_pinning_a_backend_that_is_not_configured_reports_nothing_rather_than_falling_back():
    """Silently sending from a personal mailbox when the operator asked for the verified
    domain would be the wrong kind of helpful."""
    import pytest

    from oblag import config, notify

    monkeypatch = pytest.MonkeyPatch()
    try:
        _settings(monkeypatch, mail_backend="resend", smtp_host="smtp.gmail.com")
        assert notify.mail_backend() is None
        assert notify.email_enabled() is False
        _settings(monkeypatch, mail_backend="smtp", resend_api_key="re_test")
        assert notify.mail_backend() is None
        assert config.Settings().mail_backend == "auto"
    finally:
        monkeypatch.undo()


def test_resend_posts_the_message_and_carries_the_display_name(monkeypatch):
    from oblag import notify

    _settings(monkeypatch, resend_api_key="re_test", smtp_from="alerts@example.com")
    sent = {}

    class _Resp:
        status_code = 200
        text = "{}"

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    notify._send_email(
        ["someone@example.com"], "Subject", "Body", from_name="Gazette", reply_to="me@example.com"
    )
    assert sent["url"] == notify.RESEND_API
    assert sent["headers"]["Authorization"] == "Bearer re_test"
    assert sent["json"]["from"] == "Gazette <alerts@example.com>"
    assert sent["json"]["to"] == ["someone@example.com"]
    assert sent["json"]["reply_to"] == "me@example.com"
    assert sent["json"]["text"] == "Body"


def test_a_resend_rejection_says_what_resend_said(monkeypatch):
    """Its errors name the actual problem — unverified domain, a From it does not own —
    and a generic failure would leave an operator with nothing to act on."""
    import pytest

    from oblag import notify

    _settings(monkeypatch, resend_api_key="re_test")

    class _Resp:
        status_code = 403
        text = '{"message":"The example.com domain is not verified"}'

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="not verified"):
        notify._send_email(["someone@example.com"], "s", "b")
