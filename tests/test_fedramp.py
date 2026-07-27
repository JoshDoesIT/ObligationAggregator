"""FedRAMP: the only obligation whose body publishes nothing but a sitemap."""

from __future__ import annotations

from datetime import date

from conftest import load_fixture
from oblag.adapters.base import RawDocument
from oblag.adapters.fedramp import FedrampAdapter
from oblag.core.statemap import compute_state
from oblag.db.models import DateType, ItemState


def _raw(since: str | None = None) -> RawDocument:
    return RawDocument(
        url="https://www.fedramp.gov/sitemap.xml",
        content=load_fixture("fedramp", "sitemap.xml"),
        content_type="application/xml",
        meta={"since": since} if since else {},
    )


def _items():
    return list(FedrampAdapter().normalize(_raw()))


def test_the_announcements_that_change_what_a_csp_must_do_are_kept():
    slugs = {i.native_meta["slug"] for i in _items()}
    for kept in (
        "rev-5-baselines-have-been-approved-and-released",
        "public-preview-consolidated-rules-2026",
        "cisa-emergency-directive-24-01",
        "fedramp-bod-23-02-guidance",
        "strengthening-the-use-of-cryptography-to-secure-federal-cloud-systems",
        "understanding-baselines-and-impact-levels",
    ):
        assert kept in slugs, kept


def test_programme_news_is_not_an_item():
    """A new leader, an RFQ, a shutdown notice and the annual survey recap are all
    things FedRAMP announced. None of them changes an obligation."""
    slugs = {i.native_meta["slug"] for i in _items()}
    for dropped in (
        "welcoming-a-new-leader-for-a-new-fedramp",
        "rfq-for-grc-solution-released",
        "fedramp-shutdown-updates",
        "fy22-annual-survey-recap",
        "fedramp-turns-10",
        "youtube-channel",
        "fedramp-authorizations-hit-300",
        "continuing-our-commitment-to-public-engagement",
    ):
        assert dropped not in slugs, dropped


def test_the_filter_is_narrow_by_design():
    """52 dated announcements in the sitemap, 17 of them obligation changes. If a
    loosened pattern ever doubles that, this is where it shows up."""
    assert len(_items()) == 17


def test_the_date_comes_from_the_slug_not_lastmod():
    """Two live entries carry a lastmod of the crawl date on a 2025 announcement, which
    would file them as this year's news. The slug's own date never drifts."""
    ed = next(i for i in _items() if i.native_meta["slug"].endswith("directive-26-01"))
    assert ed.published_at == date(2025, 10, 15)
    adopted = [d for d in ed.dates if d.date_type is DateType.adopted]
    assert adopted and adopted[0].value == date(2025, 10, 15)


def test_titles_carry_no_date_prefix_and_read_as_english():
    for item in _items():
        assert item.title.startswith("FedRAMP: ")
        assert not item.title[9:11].isdigit(), item.title
    by_slug = {i.native_meta["slug"]: i.title for i in _items()}
    assert by_slug["fedramp-bod-23-02-guidance"] == "FedRAMP: BOD 23-02 guidance"
    assert (
        by_slug["responding-to-cisa-emergency-directive-26-01"]
        == "FedRAMP: Responding to CISA emergency directive 26-01"
    )
    assert (
        by_slug["rev-5-baselines-have-been-approved-and-released"]
        == "FedRAMP: Rev 5 baselines have been approved and released"
    )


def test_mixed_case_slugs_are_still_matched():
    """Upstream casing is inconsistent, and a lowercase-only slug pattern silently
    skipped the 3PAO performance standards document."""
    slugs = {i.native_meta["slug"] for i in _items()}
    assert "updated-3pao-obligations-and-performance-standards-document" in slugs


def test_the_slug_is_the_identity_and_every_row_is_distinct():
    keys = [i.external_key for i in _items()]
    assert all(t == "fedramp_announcement" for t, _ in keys)
    assert len(set(keys)) == len(keys)
    assert all(i.obligation_slug == "fedramp" for i in _items())


def test_the_incremental_window_is_respected():
    """Without it, every announcement back to 2017 re-ingests on every run. The window
    is lastmod, not the slug date, so a 2025 page FedRAMP re-touched this week still
    comes through — and still files itself under the day it was announced."""
    recent = list(FedrampAdapter().normalize(_raw(since="2026-01-01")))
    slugs = {i.native_meta["slug"] for i in recent}
    assert "understanding-baselines-and-impact-levels" not in slugs  # lastmod 2017
    assert "responding-to-cisa-emergency-directive-26-01" in slugs  # lastmod today
    assert len(recent) < len(_items())


def test_statemap():
    today = date(2026, 7, 27)
    assert compute_state("fedramp", "announcement", {}, {}, today) is ItemState.effective
    assert compute_state("fedramp", "something-else", {}, {}, today) is None


def test_a_broken_sitemap_yields_nothing_rather_than_raising():
    for content in (b"", b"<html>nope</html>", b"<urlset></urlset>"):
        raw = RawDocument(url="t", content=content, content_type="application/xml")
        assert list(FedrampAdapter().normalize(raw)) == []
