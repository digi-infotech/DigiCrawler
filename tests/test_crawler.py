"""End-to-end crawls against simulated websites of many kinds (no internet needed)."""
from __future__ import annotations

import httpx
import pytest
import respx

from contact_scraper import fetcher as fetcher_module
from contact_scraper.models import Settings
from contact_scraper.runner import run

HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(fetcher_module, "RETRY_BACKOFF", 0)


class FakeWeb:
    """Serves pages per host+path; anything unknown is a 404. Records every request."""

    def __init__(self, mock: respx.MockRouter):
        self.sites: dict[str, dict[str, object]] = {}
        self.requests: list[str] = []
        mock.route().mock(side_effect=self._handle)

    def site(self, origin: str, pages: dict[str, object]) -> None:
        self.sites[origin] = pages

    def _handle(self, request: httpx.Request):
        url = str(request.url)
        self.requests.append(url)
        origin = f"{request.url.scheme}://{request.url.host}"
        page = self.sites.get(origin, {}).get(request.url.path, httpx.Response(404))
        if callable(page):
            page = page(request)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, str):
            return httpx.Response(200, text=page, headers=HTML)
        return page

    def fetched(self, origin: str) -> list[str]:
        return [u.replace(origin, "") or "/" for u in self.requests if u.startswith(origin) and "robots.txt" not in u]


@pytest.fixture
def web():
    with respx.mock(assert_all_called=False) as mock:
        yield FakeWeb(mock)


async def scrape(url: str, region: str = "IN", **kw):
    settings = Settings(region=region, concurrency=5, **kw)
    return (await run([url], settings))[0]


def page(body: str, title: str = "Acme") -> str:
    filler = "<p>" + "We provide quality services to our customers. " * 15 + "</p>"
    return f"<html><head><title>{title}</title></head><body>{body}{filler}</body></html>"


# ----------------------------------------------------------------- site types

async def test_contacts_only_in_homepage_footer(web):
    """Small business site: one page, contact details in plain footer text."""
    web.site("https://footer.in", {"/": page(
        "<footer><p>Call: 98450 43210 / 98450 43211</p><p>Email: info@footer.in</p>"
        "<p>Landline: 022-2345 6789</p></footer>")})
    r = await scrape("https://footer.in")
    assert r.status == "ok"
    assert r.emails == ["info@footer.in"]
    assert r.mobiles == ["+91 98450 43210", "+91 98450 43211"]
    assert r.phones == ["+91 98450 43210", "+91 98450 43211", "+91 22 2345 6789"]
    assert r.pages == ["https://footer.in"]


async def test_contact_page_not_linked_is_still_found(web):
    """Menu is built by JS (no links in HTML) but /contact-us exists with plain-text details."""
    web.site("https://hidden.in", {
        "/": page("<h1>Welcome</h1>"),
        "/contact-us": page("<p>Reach our sales team at sales@hidden.in or 9845043210.</p>"),
    })
    r = await scrape("https://hidden.in")
    assert r.emails == ["sales@hidden.in"]
    assert r.mobiles == ["+91 98450 43210"]
    assert r.pages == ["https://hidden.in/contact-us"]
    assert set(web.fetched("https://hidden.in")) == {"/", "/contact", "/about", "/services", "/contact-us", "/about-us"}
    assert (r.state, r.country) == ("", "")  # no address anywhere, and the .in ending alone doesn't count


async def test_state_and_country_from_contact_page_address(web):
    web.site("https://addr.in", {
        "/": page('<a href="/contact">Contact</a><p>Toll free 1800-266-1234</p>'),
        "/contact": page("<address>4th Floor, Baner Road,<br>Pune, Maharashtra 411045<br>India</address>"
                         "<p>Mobile: 98450 43210</p>"),
    })
    r = await scrape("https://addr.in")
    assert (r.state, r.country) == ("Maharashtra", "India")
    assert r.phones == ["+91 98450 43210"]  # the toll-free number is left out


async def test_wordpress_style_site_collects_from_all_key_pages(web):
    """Contacts spread over /about-us, /services and /contact, with repeats across pages."""
    web.site("https://www.multi.co.in", {
        "/": page('<nav><a href="/about-us/">About Us</a><a href="/services/">Our Services</a>'
                  '<a href="/contact/">Contact</a><a href="/blog/">Blog</a></nav>'
                  "<footer>info@multi.co.in | +91 98450 00001</footer>"),
        "/about-us/": page("<p>Founder: Ravi, ravi@multi.co.in, mobile 98450 00002.</p>"),
        "/services/": page("<p>For service bookings call 98450 00003 or (080) 4123 4567.</p>"
                           "<footer>info@multi.co.in | +91 98450 00001</footer>"),
        "/contact/": page("<p>Head office: 080-4123-4568</p><p>Fax: 080-4123-4569</p>"
                          "<p>Write to sales [at] multi [dot] co [dot] in</p>"
                          "<p>Partner desk: partners@agency.com</p>"),
        "/blog/": page("<p>blog@multi.co.in 98450 99999</p>"),
    })
    r = await scrape("multi.co.in".join(["https://www.", ""]), region="US")  # .co.in -> India rules
    assert r.status == "ok"
    assert r.emails == ["info@multi.co.in", "sales@multi.co.in", "ravi@multi.co.in", "partners@agency.com"]
    assert r.mobiles == ["+91 98450 00001", "+91 98450 00002", "+91 98450 00003"]  # page order: home, contact, about, services
    assert set(r.phones) == {"+91 98450 00001", "+91 98450 00002", "+91 98450 00003",
                             "+91 80 4123 4567", "+91 80 4123 4568"}           # fax left out
    assert "blog" not in " ".join(web.fetched("https://www.multi.co.in"))     # blog never opened
    assert all("98450 99999" not in p for p in r.phones)


async def test_every_path_returns_homepage(web):
    """Some sites answer every address with the homepage. Don't double count or loop."""
    home = page("<p>Call 98450 43210, mail hi@spa.in</p>")
    web.site("https://spa.in", {p: home for p in ["/", "/contact", "/contact-us", "/about", "/about-us", "/services"]})
    r = await scrape("https://spa.in")
    assert r.phones == ["+91 98450 43210"] and r.emails == ["hi@spa.in"]
    assert r.pages == ["https://spa.in"]


async def test_soft_404_pages_are_ignored(web):
    """Guessed pages that show 'Page not found' (with status 200) are not used."""
    not_found = page("<h1>Oops! Page not found</h1><p>Call 98450 11111</p>", title="Page not found")
    web.site("https://soft.in", {
        "/": page("<p>Call 98450 43210</p>"),
        **{p: not_found for p in ["/contact", "/contact-us", "/about", "/about-us", "/services"]},
    })
    r = await scrape("https://soft.in")
    assert r.phones == ["+91 98450 43210"]


async def test_redirect_to_new_domain_uses_new_domain_and_country(web):
    """old.com redirects to www.new-brand.co.uk; links and phone rules follow the new site."""
    web.site("https://old.com", {"/": httpx.Response(301, headers={"location": "https://www.new-brand.co.uk/"})})
    web.site("https://www.new-brand.co.uk", {
        "/": page('<a href="/contact">Contact</a>'),
        "/contact": page("<p>Tel: 020 7946 0018</p><p>Mobile: 07911 123456</p><p>hello@new-brand.co.uk</p>"),
    })
    r = await scrape("https://old.com", region="IN")
    assert r.final_url == "https://www.new-brand.co.uk/"
    assert r.phones == ["+44 20 7946 0018", "+44 7911 123456"]
    assert r.mobiles == ["+44 7911 123456"]
    assert r.emails == ["hello@new-brand.co.uk"]


async def test_us_business_site(web):
    web.site("https://joes-pizza.com", {
        "/": page('<a href="/contact-us">Contact Us</a><p>Order: (212) 736-5000</p>'),
        "/contact-us": page("<p>Owner cell: 646-464-0123</p><p>Catering: orders@joes-pizza.com</p>"),
    })
    r = await scrape("https://joes-pizza.com", region="US")
    assert r.phones == ["+1 212-736-5000", "+1 646-464-0123"]
    assert r.mobiles == ["+1 646-464-0123"]
    assert r.emails == ["orders@joes-pizza.com"]


async def test_french_site_latin1_encoding(web):
    body = page("<p>Téléphone : 01 42 68 53 00 - contact@boulangerie.fr</p>").encode("latin-1")
    web.site("https://boulangerie.fr", {"/": httpx.Response(200, content=body, headers={"content-type": "text/html; charset=iso-8859-1"})})
    r = await scrape("https://boulangerie.fr", region="IN")
    assert r.phones == ["+33 1 42 68 53 00"]
    assert r.emails == ["contact@boulangerie.fr"]


async def test_javascript_only_site_without_browser(web):
    web.site("https://spa-app.in", {"/": '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'})
    r = await scrape("https://spa-app.in", use_browser=False)
    assert r.status == "no_contacts"


async def test_other_websites_are_not_followed(web):
    web.site("https://local.in", {"/": page(
        '<a href="https://facebook.com/local/contact">Contact on Facebook</a>'
        '<a href="https://maps.google.com/?q=local">Locate us</a>')})
    await scrape("https://local.in")
    assert not any("facebook" in u or "google" in u for u in web.requests)


async def test_page_limit_is_respected(web):
    links = "".join(f'<a href="/contact-{i}">Contact {i}</a>' for i in range(20))
    web.site("https://many.in", {"/": page(links)})
    await scrape("https://many.in", max_pages=4)
    assert len(web.fetched("https://many.in")) == 4  # homepage + 3


# ----------------------------------------------------------------- problems

async def test_blocked_403(web):
    web.site("https://blocked.in", {"/": httpx.Response(403)})
    web.site("http://blocked.in", {"/": httpx.Response(403)})
    assert (await scrape("https://blocked.in")).status == "error: website blocked automated visits (403)"


async def test_domain_does_not_exist(web):
    web.site("https://nope-nope.in", {"/": httpx.ConnectError("[Errno 11001] getaddrinfo failed")})
    assert (await scrape("https://nope-nope.in")).status == "error: website address not found"


async def test_timeout(web):
    for origin in ["https://slow.in", "http://slow.in"]:
        web.site(origin, {"/": httpx.ReadTimeout("timed out")})
    assert (await scrape("https://slow.in")).status == "error: website took too long to respond"


async def test_server_error_then_recovers(web):
    calls = {"n": 0}

    def flaky(_request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, text=page("<p>98450 43210</p>"), headers=HTML)

    web.site("https://flaky.in", {"/": flaky})
    r = await scrape("https://flaky.in")
    assert r.status == "ok" and calls["n"] == 2


async def test_bad_certificate_falls_back(web):
    calls = {"n": 0}

    def cert(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
        return httpx.Response(200, text=page("<p>98450 43210</p>"), headers=HTML)

    web.site("https://oldcert.in", {"/": cert})
    assert (await scrape("https://oldcert.in")).phones == ["+91 98450 43210"]


async def test_http_only_site(web):
    web.site("https://httponly.in", {"/": httpx.ConnectError("connection refused")})
    web.site("http://httponly.in", {"/": page("<p>98450 43210</p>")})
    r = await scrape("https://httponly.in")
    assert r.status == "ok" and r.final_url == "http://httponly.in"


async def test_not_a_web_page(web):
    for origin in ["https://pdf.in", "http://pdf.in"]:
        web.site(origin, {"/": httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})})
    assert (await scrape("https://pdf.in")).status == "error: not a web page"


async def test_robots_txt_respected(web):
    web.site("https://private.in", {
        "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /contact", headers={"content-type": "text/plain"}),
        "/": page('<a href="/contact">Contact</a><p>98450 43210</p>'),
        "/contact": page("<p>98450 00000</p>"),
    })
    r = await scrape("https://private.in")
    assert r.phones == ["+91 98450 43210"]
    assert "/contact" not in web.fetched("https://private.in")

    web.site("https://closed.in", {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /",
                                                                  headers={"content-type": "text/plain"})})
    assert "robots" in (await scrape("https://closed.in")).status


async def test_very_large_page_is_cut_not_crashed(web, monkeypatch):
    monkeypatch.setattr(fetcher_module, "MAX_BYTES", 200_000)  # real limit is 3 MB; smaller keeps the test fast
    huge = page("<p>98450 43210</p>" + "<p>lorem ipsum dolor</p>" * 300_000 + "<p>98450 99999</p>")
    web.site("https://huge.in", {"/": huge})
    assert (await scrape("https://huge.in")).phones == ["+91 98450 43210"]  # text after the cut is ignored


async def test_many_rows_blank_and_duplicates(web):
    web.site("https://a.in", {"/": page("<p>98450 43210</p>")})
    web.site("https://b.in", {"/": page("<p>b@b.in</p>")})
    urls = ["https://a.in", "", "https://b.in", "https://a.in"]
    progress = []
    results = await run(urls, Settings(region="IN"), lambda d, t, r: progress.append((d, t)))
    assert [r.status for r in results] == ["ok", "skipped: no valid website address", "ok", "ok"]
    assert results[0] is results[3]
    assert progress[-1] == (2, 2)
    assert sum(1 for u in web.requests if u.rstrip("/") == "https://a.in") == 1  # fetched once


async def test_same_email_in_text_and_schema_listed_once(web):
    web.site("https://dup.in", {"/": page(
        '<script type="application/ld+json">{"email":"info@dup.in","telephone":"+91 98450 43210"}</script>'
        "<p>info@dup.in</p><p>INFO@DUP.IN</p><p>+91 98450 43210</p>")})
    r = await scrape("https://dup.in")
    assert r.emails == ["info@dup.in"]
    assert r.phones == ["+91 98450 43210"]


async def test_us_site_with_india_selected(web):
    """The manager picked India, but this .com site is clearly in New York."""
    web.site("https://deli-nyc.com", {"/": page(
        "<p>205 E Houston St, New York, NY 10002</p><p>Call (212) 254-2246</p><p>Cell 917-464-3769</p>")})
    r = await scrape("https://deli-nyc.com", region="IN")
    assert r.region == "US"
    assert r.phones == ["+1 212-254-2246", "+1 917-464-3769"]
    assert r.mobiles == ["+1 917-464-3769"]


async def test_indian_site_on_dotcom_with_us_selected(web):
    web.site("https://masala-shop.com", {
        "/": page('<a href="/contact-us">Contact Us</a>'),
        "/contact-us": page("<p>Shop 4, MG Road, Pune 411001, India</p><p>Mob: 98450 12345</p><p>020 2612 3456</p>"),
    })
    r = await scrape("https://masala-shop.com", region="US")
    assert r.region == "IN"
    assert r.mobiles == ["+91 98450 12345"]
    assert r.phones == ["+91 98450 12345", "+91 20 2612 3456"]


async def test_placeholder_number_in_schema_is_ignored(web):
    web.site("https://brand.in", {"/": page(
        '<script type="application/ld+json">{"telephone":"+91-9876543210"}</script><p>support@brand.in</p>')})
    r = await scrape("https://brand.in")
    assert r.phones == [] and r.emails == ["support@brand.in"]


async def test_bot_protection_page_is_an_error_not_contacts(web):
    akamai = ("<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>"
              "You don't have permission to access \"http://www.hotel.in/\" on this server.<p>"
              "Reference #18.394c3917.1794386023.9f7af13e</body></html>")
    web.site("https://www.hotel.in", {"/": akamai})
    r = await scrape("https://www.hotel.in")
    assert r.status == "error: website blocked automated visits"
    assert r.phones == []


async def test_cloudflare_challenge_on_contact_page_is_skipped(web):
    web.site("https://cf.in", {
        "/": page('<a href="/contact">Contact</a><p>98450 43210</p>'),
        "/contact": "<html><head><title>Just a moment...</title></head><body>Checking your browser 98450 11111</body></html>",
    })
    r = await scrape("https://cf.in")
    assert r.phones == ["+91 98450 43210"]


# ----------------------------------------------------------------- quick mode (analytics off)

MENU = '<nav><a href="/contact">Contact</a> <a href="/about">About</a> <a href="/services">Services</a></nav>'


async def test_quick_mode_stops_at_homepage_with_email_and_mobile(web):
    nav = MENU
    web.site("https://quick.in", {"/": page(nav + "<p>info@quick.in · 98450 43210</p>"),
                                  "/contact": page("<p>sales@quick.in</p>"), "/about": page("<p>About</p>")})
    r = await scrape("https://quick.in", analytics=False)
    assert web.fetched("https://quick.in") == ["/"]
    assert (r.emails, r.mobiles, r.status) == (["info@quick.in"], ["+91 98450 43210"], "ok")


async def test_quick_mode_opens_contact_page_but_not_about(web):
    nav = MENU
    web.site("https://quick2.in", {"/": page(nav + "<p>info@quick2.in</p>"),
                                   "/contact": page("<p>Mobile: 98450 43210</p>"),
                                   "/about": page("<p>hr@quick2.in</p>"), "/services": page("<p>x</p>")})
    r = await scrape("https://quick2.in", analytics=False)
    assert sorted(web.fetched("https://quick2.in")) == ["/", "/contact"]
    assert (r.emails, r.mobiles) == (["info@quick2.in"], ["+91 98450 43210"])
    assert r.address == ""  # not worked out in quick mode


async def test_quick_mode_falls_back_to_about_pages_when_nothing_found(web):
    nav = MENU
    web.site("https://quick3.in", {"/": page(nav), "/contact": page("<p>Use the form below.</p>"),
                                   "/about": page("<p>Write to hello@quick3.in</p>"), "/services": page("<p>x</p>")})
    r = await scrape("https://quick3.in", analytics=False)
    assert {"/about", "/services"} <= set(web.fetched("https://quick3.in"))
    assert r.emails == ["hello@quick3.in"]


async def test_analytics_mode_opens_every_planned_page(web):
    nav = MENU
    web.site("https://full.in", {"/": page(nav + "<p>info@full.in · 98450 43210</p>"),
                                 "/contact": page("<p>sales@full.in</p>"), "/about": page("<p>About</p>"),
                                 "/services": page("<p>x</p>")})
    r = await scrape("https://full.in")
    assert sorted(web.fetched("https://full.in")) == ["/", "/about", "/contact", "/services"]
    assert r.emails == ["info@full.in", "sales@full.in"]
