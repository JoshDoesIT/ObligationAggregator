"""Headless-browser fetch tier (spec 06 addendum): last-resort rendering for sources
with no feed, API, or static payload (EBA, AICPA). Feed/XHR-first remains the rule —
an adapter may only use this after probing establishes no plainer mechanism exists.

Optional dependency: `pip install 'oblag[browser]'` (+ `playwright install chromium`
outside environments that pre-provision it). Adapters gate `enabled()` on
`browser_available()` so a missing browser is a clean self-disable, never an error."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from oblag.adapters.base import RawDocument

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
# Pre-provisioned Chromium locations (checked when playwright's own registry is empty)
_FALLBACK_CHROMIUM = ("/opt/pw-browsers/chromium",)


class BrowserUnavailable(RuntimeError):
    pass


@lru_cache
def browser_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _cdp_url() -> str | None:
    from oblag.config import get_settings

    return get_settings().browser_cdp_url


def _chromium_executable() -> str | None:
    """Explicit executable when playwright's registry lacks a download (e.g. the
    pre-provisioned /opt/pw-browsers layout)."""
    for root in _FALLBACK_CHROMIUM:
        path = Path(root)
        if path.is_file():
            return str(path)
        if path.is_dir():
            for candidate in sorted(path.rglob("chrome")) + sorted(path.rglob("chromium")):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


def render_page(
    url: str,
    *,
    wait_selector: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RawDocument:
    """Render a page in headless Chromium and return the serialized DOM.

    The returned RawDocument carries meta={"rendered": "true"} so snapshots record
    that the content is a DOM serialization, not raw response bytes."""
    if not browser_available():
        raise BrowserUnavailable("playwright is not installed (pip install 'oblag[browser]')")
    from playwright.sync_api import sync_playwright

    timeout_ms = timeout_s * 1000
    cdp = _cdp_url()
    if cdp:
        # Remote browser (serverless platforms): the remote side owns egress/TLS.
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp, timeout=timeout_ms)
            try:
                context = browser.new_context()
                page = context.new_page()
                response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=timeout_ms)
                else:
                    import contextlib

                    with contextlib.suppress(Exception):  # busy pages never go idle
                        page.wait_for_load_state("networkidle", timeout=timeout_ms)
                content = page.content()
                status = response.status if response else None
            finally:
                browser.close()
        return RawDocument(
            url=url,
            content=content.encode("utf-8"),
            content_type="text/html",
            fetched_at=datetime.now(UTC),
            http_status=status,
            http_headers={},
            meta={"rendered": "true", "via": "cdp"},
        )
    with sync_playwright() as pw:
        args: list[str] = []
        if os.geteuid() == 0:  # containers commonly run as root; sandbox needs userns
            args.append("--no-sandbox")
        launch_kwargs: dict = {"headless": True}
        # Chromium ignores proxy env vars; pass any configured egress proxy explicitly
        # (trust for a TLS-intercepting proxy comes from the system/NSS CA store).
        proxy_server = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy_server:
            proxy_conf: dict = {"server": proxy_server}
            no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
            if no_proxy:
                proxy_conf["bypass"] = no_proxy
            launch_kwargs["proxy"] = proxy_conf
            # TLS-intercepting egress proxies commonly reset Chromium's TLS 1.3
            # post-quantum ClientHello (diagnosed via netlog: SSL_HANDSHAKE_ERROR on
            # the tunneled hello; TLS 1.2 completes). This caps only the client→proxy
            # leg — the proxy re-originates TLS upstream.
            args.append("--ssl-version-max=tls1.2")
        if args:
            launch_kwargs["args"] = args
        try:
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception:
            executable = _chromium_executable()
            if executable is None:
                raise
            browser = pw.chromium.launch(executable_path=executable, **launch_kwargs)
        try:
            page = browser.new_page(
                user_agent="ObligationAggregator/0.1 "
                "(+https://github.com/JoshDoesIT/ObligationAggregator)"
            )
            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            else:
                try:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except Exception:  # noqa: BLE001 — busy pages never go idle; DOM is enough
                    log.debug("networkidle timeout for %s; using current DOM", url)
            content = page.content()
            status = response.status if response else None
        finally:
            browser.close()
    return RawDocument(
        url=url,
        content=content.encode("utf-8"),
        content_type="text/html",
        fetched_at=datetime.now(UTC),
        http_status=status,
        http_headers={},
        meta={"rendered": "true"},
    )
