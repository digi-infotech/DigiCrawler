"""When plain HTTP fails or finds nothing, a headless browser takes over automatically.

A fake browser stands in for Chromium so these run anywhere, fast.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from contact_scraper import crawler as crawler_module
from contact_scraper import fetcher as fetcher_module
from contact_scraper.models import Settings
from contact_scraper.runner import run

from .test_crawler import FakeWeb, page  # noqa: F401  (FakeWeb used via fixture below)

ACCESS_DENIED = ("<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>"
                 "You don't have permission to access this server. Reference #18.1794386023</body></html>")


class FakeBrowser:
    """What the 'real browser' sees, per URL. None = page failed to load."""
    pages: dict[str, str | None] = {}
    rendered: list[str] = []
    available = True
    delay = 0.0

    def __init__(self, settings):
        FakeBrowser.rendered = []

    def session(self):
        return FakeSession()

    async def close(self):
        pass


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def start(self):
        return FakeBrowser.available

    async def render(self, url):
        if not FakeBrowser.available:
            return None
        FakeBrowser.rendered.append(url)
        if FakeBrowser.delay:
            await asyncio.sleep(FakeBrowser.delay)
        html = FakeBrowser.pages.get(url.rstrip("/"))
        return (url, html) if html is not None else None

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_browser(monkeypatch):
    monkeypatch.setattr(fetcher_module, "BrowserRenderer", FakeBrowser)
    monkeypatch.setattr(fetcher_module, "RETRY_BACKOFF", 0)
    FakeBrowser.pages, FakeBrowser.available, FakeBrowser.delay, FakeBrowser.rendered = {}, True, 0.0, []


@pytest.fixture
def web():
    import respx
    with respx.mock(assert_all_called=False) as mock:
        yield FakeWeb(mock)


async def scrape(url, **kw):
    return (await run([url], Settings(region="IN", use_browser=True, **kw)))[0]


async def test_403_homepage_is_opened_in_browser(web):
    web.site("https://guarded.in", {"/": httpx.Response(403)})
    FakeBrowser.pages = {
        "https://guarded.in": page('<a href="/contact-us">Contact</a><p>Call 98450 43210</p>'),
        "https://guarded.in/contact-us": page("<p>Email sales@guarded.in, landline 022 2345 6789</p>"),
    }
    r = await scrape("https://guarded.in")
    assert r.status == "ok" and r.method == "browser"
    assert r.emails == ["sales@guarded.in"]
    assert r.mobiles == ["+91 98450 43210"]
    assert r.phones == ["+91 98450 43210", "+91 22 2345 6789"]
    assert r.pages == ["https://guarded.in", "https://guarded.in/contact-us"]


async def test_access_denied_page_with_status_200_also_goes_to_browser(web):
    web.site("https://akamai.in", {"/": ACCESS_DENIED})
    FakeBrowser.pages = {"https://akamai.in": page("<p>98450 43210</p>")}
    r = await scrape("https://akamai.in")
    assert r.status == "ok" and r.method == "browser" and r.phones == ["+91 98450 43210"]


async def test_blocked_even_in_browser(web):
    web.site("https://fortress.in", {"/": httpx.Response(403)})
    FakeBrowser.pages = {"https://fortress.in": ACCESS_DENIED}
    r = await scrape("https://fortress.in")
    assert r.status == crawler_module.BLOCKED_EVEN_IN_BROWSER
    assert r.phones == []        # nothing read from the "Access denied" page


async def test_javascript_site_with_nothing_over_http(web):
    web.site("https://react-shop.in", {"/": page("<p>Welcome to our shop</p>")})
    FakeBrowser.pages = {"https://react-shop.in": page("<footer>Mob: 98450 12345 · hi@react-shop.in</footer>")}
    r = await scrape("https://react-shop.in")
    assert r.method == "browser"
    assert r.mobiles == ["+91 98450 12345"] and r.emails == ["hi@react-shop.in"]


async def test_browser_skips_pages_that_do_not_exist(web):
    web.site("https://plain.in", {"/": page("<p>Welcome</p>")})   # every guessed page is a real 404
    FakeBrowser.pages = {"https://plain.in": page("<p>Still nothing here</p>")}
    r = await scrape("https://plain.in")
    assert r.status == "no_contacts"
    assert FakeBrowser.rendered == ["https://plain.in"]  # homepage only; the 404 pages are not retried


async def test_only_refused_contact_page_is_opened_in_browser(web):
    web.site("https://mixed.in", {
        "/": page('<a href="/contact">Contact</a><p>info@mixed.in</p>'),
        "/contact": httpx.Response(403),
    })
    FakeBrowser.pages = {"https://mixed.in/contact": page("<p>Mobile 98450 43210</p>")}
    r = await scrape("https://mixed.in")
    assert r.method == "http+browser"
    assert r.emails == ["info@mixed.in"] and r.mobiles == ["+91 98450 43210"]
    assert FakeBrowser.rendered == ["https://mixed.in/contact"]


async def test_site_fine_over_http_never_opens_browser(web):
    web.site("https://easy.in", {"/": page("<p>98450 43210</p>")})
    r = await scrape("https://easy.in")
    assert r.method == "http" and FakeBrowser.rendered == []


async def test_missing_domain_never_opens_browser(web):
    web.site("https://nope-nope.in", {"/": httpx.ConnectError("[Errno 11001] getaddrinfo failed")})
    r = await scrape("https://nope-nope.in")
    assert r.status == "error: website address not found" and FakeBrowser.rendered == []


async def test_browser_not_installed_keeps_http_answer(web):
    FakeBrowser.available = False
    web.site("https://guarded.in", {"/": httpx.Response(403)})
    r = await scrape("https://guarded.in")
    assert r.status == "error: website blocked automated visits (403)"


async def test_slow_browser_keeps_what_http_found(web, monkeypatch):
    monkeypatch.setattr(crawler_module, "BROWSER_BUDGET", 0.01)   # 0.15 s with the 15 s timeout
    FakeBrowser.delay = 1
    web.site("https://mixed.in", {
        "/": page('<a href="/contact">Contact</a><p>info@mixed.in</p>'),
        "/contact": httpx.Response(403),
    })
    r = await scrape("https://mixed.in")
    assert r.status == "ok" and r.emails == ["info@mixed.in"] and r.method == "http"


async def test_browser_off_means_no_browser(web):
    web.site("https://guarded.in", {"/": httpx.Response(403)})
    r = (await run(["https://guarded.in"], Settings(region="IN", use_browser=False)))[0]
    assert r.status == "error: website blocked automated visits (403)" and FakeBrowser.rendered == []
