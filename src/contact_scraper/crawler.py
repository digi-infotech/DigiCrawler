"""Visits one website: the homepage plus its contact / about / services pages.

Plain HTTP first (fast, light). A real headless browser takes over automatically when:
  * the site refuses plain HTTP (403/401/429, "Access denied", "Checking your browser..."),
  * nothing is found (contacts drawn by JavaScript or loaded on scroll), or
  * contacts were found but some contact pages refused plain HTTP.

Every site gets the same full check whatever the Analytics mode: the mode only decides what goes
into the Excel file, so no email, phone or address is missed in the simple file.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import address, extract, names
from .fetcher import BrowserSession, Fetcher, FetchError
from .models import Settings, SiteResult

PAGES_IN_PARALLEL = 3          # per website, to stay gentle on small servers
HTTP_BUDGET = 8                # x settings.timeout: time allowed for the plain-HTTP pass
BROWSER_BUDGET = 12            # x settings.timeout: time allowed for the browser pass (starts once a browser is free)
BLOCKED_EVEN_IN_BROWSER = "error: website blocked automated visits, even in a browser"
TOO_SLOW = "error: website took too long to respond"


class _SitePages:
    """The useful pages of one site (duplicates and 'not found' pages left out)."""

    def __init__(self) -> None:
        self.pages: list[tuple[str, extract.HTMLParser, str]] = []
        self._seen: set[str] = set()

    def add(self, url: str, html: str, guessed: bool = False) -> tuple[extract.HTMLParser, str] | None:
        if extract.looks_blocked(html):
            return None  # bot-protection page, not the real content
        tree = extract.parse(html)
        text = extract.visible_text(tree)
        keys = {url.rstrip("/").lower(), hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()}
        if keys & self._seen:
            return None  # same address or same content as a page we already have
        if guessed and extract.looks_not_found(tree):
            return None  # e.g. /about-us doesn't exist but the site shows a "not found" page
        self._seen |= keys
        self.pages.append((url, tree, text))
        return tree, text


def _plan(tree: extract.HTMLParser, base_url: str, settings: Settings) -> list[tuple[str, bool]]:
    """Pages to open after the homepage: contact, about and services pages."""
    return extract.plan_pages(tree, base_url, max(settings.max_pages - 1, 0))


def collect_contacts(result: SiteResult, site: _SitePages, site_url: str, default_region: str) -> SiteResult:
    """Pull emails and phone numbers out of all pages, without duplicates."""
    region = extract.detect_region(site_url, [(tree, text) for _, tree, text in site.pages], default_region)
    emails: dict[str, None] = {}
    phones: dict[str, bool] = {}
    result.pages = []
    for url, tree, text in site.pages:
        ld_emails, ld_phones = extract.extract_jsonld(tree, region)
        page_emails = extract.extract_emails(tree, text) + ld_emails
        page_phones = extract.extract_phones(tree, text, region) + ld_phones
        new = any(e not in emails for e in page_emails) or any(p.number not in phones for p in page_phones)
        for email in page_emails:
            emails.setdefault(email, None)
        for phone in page_phones:
            phones[phone.number] = phones.get(phone.number, False) or phone.mobile
        if new and url not in result.pages:
            result.pages.append(url)

    result.emails = extract.sort_emails_for_site(list(emails), site_url)
    result.phones = list(phones)
    result.mobiles = [n for n, mobile in phones.items() if mobile]
    result.region = region
    result.business_name = names.business_name(site.pages, site_url)
    pages = [(tree, text) for _, tree, text in site.pages]
    place = address.detect_place(pages)
    result.state, result.country, result.city, result.pin_code = place.state, place.country, place.city, place.postcode
    result.address = address.detect_address(pages, result.country)
    result.status = "pending"
    return result.finish()


@dataclass
class _HttpOutcome:
    result: SiteResult  # best result so far; kept if the browser can't do better
    browser_step: Callable[[BrowserSession], Awaitable[SiteResult]] | None = None


# ------------------------------------------------------------------ entry point

async def crawl_site(url: str, fetcher: Fetcher, settings: Settings) -> SiteResult:
    if not await fetcher.allowed(url):
        return SiteResult(url=url, status="skipped: website does not allow automated visits (robots.txt)")

    try:
        outcome = await asyncio.wait_for(_http_pass(url, fetcher, settings), timeout=settings.timeout * HTTP_BUDGET)
    except asyncio.TimeoutError:
        outcome = _HttpOutcome(SiteResult(url=url, status=TOO_SLOW))
        if fetcher.browser:
            outcome.browser_step = lambda session: _browser_from_start(url, session, fetcher, settings, outcome.result)

    if not outcome.browser_step or not fetcher.browser:
        return outcome.result

    async with fetcher.browser.session() as session:
        if not await session.start():  # waits for a free browser; the time budget starts after this
            return outcome.result
        try:
            return await asyncio.wait_for(outcome.browser_step(session), timeout=settings.timeout * BROWSER_BUDGET)
        except asyncio.TimeoutError:
            return outcome.result


# ------------------------------------------------------------------ plain HTTP pass

async def _http_pass(url: str, fetcher: Fetcher, settings: Settings) -> _HttpOutcome:

    # 1) Homepage.
    home: tuple[str, str] | None = None
    error: FetchError | None = None
    try:
        home = await _fetch_home(url, fetcher)
        if extract.looks_blocked(home[1]):  # an "Access denied" / "Checking your browser" page
            home, error = None, FetchError("website blocked automated visits", status=403)
    except FetchError as exc:
        error = exc

    if home is None:
        failed = SiteResult(url=url, status=f"error: {error}")
        if error.kind == "dns":
            return _HttpOutcome(failed)  # the domain doesn't exist; a browser can't help
        return _HttpOutcome(failed, lambda session: _browser_from_start(url, session, fetcher, settings, failed,
                                                                         blocked=error.blocked))

    # 2) Contact / about / services pages, a few at a time.
    final_url, html = home
    result = SiteResult(url=url, final_url=final_url)
    site = _SitePages()
    # Parsing is CPU work; a worker thread keeps other sites downloading meanwhile.
    home_tree, _ = await asyncio.to_thread(site.add, final_url, html)
    sem = asyncio.Semaphore(PAGES_IN_PARALLEL)

    def collect() -> Awaitable[SiteResult]:
        return asyncio.to_thread(collect_contacts, result, site, final_url, settings.region)

    async def visit(link: str) -> tuple[str, str] | FetchError | None:
        async with sem:
            if not await fetcher.allowed(link):
                return None
            try:
                return await fetcher.fetch(link)
            except FetchError as exc:
                return exc

    refused: list[tuple[str, bool]] = []
    missing: set[str] = set()
    plan = _plan(home_tree, final_url, settings)
    pages = await asyncio.gather(*(visit(link) for link, _ in plan))
    for (link, guessed), page in zip(plan, pages):
        if isinstance(page, FetchError):
            if page.blocked:
                refused.append((link, guessed))
            elif page.status == 404:
                missing.add(link)
        elif page:
            if extract.looks_blocked(page[1]):
                refused.append((link, guessed))
            else:
                await asyncio.to_thread(site.add, page[0], page[1], guessed)
    result = await collect()

    # 3) What the browser should do, if anything.
    if result.status == "no_contacts":
        # Nothing found: details may be drawn by JavaScript, loaded on scroll, or on pages that
        # refused plain HTTP. Look at the whole site again the way a visitor sees it.
        async def look_again(session: BrowserSession) -> SiteResult:
            rendered = await session.render(final_url)
            if not rendered or extract.looks_blocked(rendered[1]):
                return result
            return await _browser_crawl(SiteResult(url=url), session, fetcher, rendered, settings, skip=missing)
        return _HttpOutcome(result, look_again)

    if refused:
        # Contacts found, but some contact pages refused plain HTTP: open just those in the browser.
        async def open_refused(session: BrowserSession) -> SiteResult:
            added = False
            for link, guessed in refused:
                page = await session.render(link)
                if page and await asyncio.to_thread(site.add, page[0], page[1], guessed):
                    added = True
            if not added:
                return result
            better = dataclasses.replace(result, method="http+browser")
            return await asyncio.to_thread(collect_contacts, better, site, final_url, settings.region)
        return _HttpOutcome(result, open_refused)

    return _HttpOutcome(result)


# ------------------------------------------------------------------ browser pass

async def _browser_from_start(url: str, session: BrowserSession, fetcher: Fetcher, settings: Settings,
                              failed: SiteResult, blocked: bool = True) -> SiteResult:
    """The homepage failed over plain HTTP: open everything in the browser."""
    rendered = await session.render(url)
    if rendered and not extract.looks_blocked(rendered[1]):
        return await _browser_crawl(SiteResult(url=url), session, fetcher, rendered, settings)
    if rendered or blocked:
        return dataclasses.replace(failed, status=BLOCKED_EVEN_IN_BROWSER)
    return failed


async def _browser_crawl(result: SiteResult, session: BrowserSession, fetcher: Fetcher,
                         rendered: tuple[str, str], settings: Settings, skip: set[str] = frozenset()) -> SiteResult:
    """Homepage + contact/about/services pages, all opened in the headless browser."""
    result.method = "browser"
    final_url = rendered[0]
    result.final_url = final_url
    site = _SitePages()
    parsed = await asyncio.to_thread(site.add, *rendered)

    if parsed:
        for link, guessed in _plan(parsed[0], final_url, settings):
            if link in skip or not await fetcher.allowed(link):
                continue
            page = await session.render(link)
            if page:
                await asyncio.to_thread(site.add, page[0], page[1], guessed)
    return await asyncio.to_thread(collect_contacts, result, site, final_url, settings.region)


async def _fetch_home(url: str, fetcher: Fetcher) -> tuple[str, str]:
    try:
        return await fetcher.fetch(url)
    except FetchError as exc:
        # Some older sites only work over plain http://
        if url.startswith("https://") and exc.kind != "dns" and not exc.blocked:
            try:
                return await fetcher.fetch("http://" + url[len("https://"):])
            except FetchError:
                pass
        raise
