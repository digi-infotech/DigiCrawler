"""Runs the crawler over every row, in parallel, reporting progress."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from .crawler import crawl_site
from .fetcher import Fetcher
from .models import Settings, SiteResult
from .names import name_from_address
from .netguard import NetworkGuard, NoGuard

DOMAIN_NOT_FOUND = "error: website address not found"  # no such website, so no name either
DNS_RETRY_DELAY = 2.0  # "address not found" gets one more try after this, in case it was a DNS hiccup

log = logging.getLogger(__name__)

# May return an awaitable (the API saves each result to disk); the website only counts as done after it.
ProgressCallback = Callable[[int, int, SiteResult], "Awaitable[None] | None"]


async def run(urls: list[str], settings: Settings, on_progress: ProgressCallback | None = None,
              on_network: Callable[[bool], None] | None = None) -> list[SiteResult]:
    """Scrape each URL (blank = skipped). Returns one result per input, same order.

    With ``settings.online_check``, a lost internet connection pauses the run instead of saving
    every website as failed; ``on_network(False/True)`` reports the pause and the recovery.
    """
    unique = list(dict.fromkeys(u for u in urls if u))
    total = len(unique)
    done = 0
    results: dict[str, SiteResult] = {}
    sem = asyncio.Semaphore(settings.concurrency)
    guard = NetworkGuard(on_change=on_network) if settings.online_check else NoGuard()

    async with Fetcher(settings) as fetcher, guard:

        async def check(url: str) -> SiteResult:
            second_chance = True
            while True:
                await guard.wait_online()
                async with sem:
                    try:
                        # crawl_site keeps its own time limits (one for plain HTTP, one for the browser).
                        res = await crawl_site(url, fetcher, settings)
                    except Exception as exc:  # never let one site break the whole run
                        log.exception("Unexpected error for %s", url)
                        res = SiteResult(url=url, status=f"error: {type(exc).__name__}")
                if not await guard.confirm():
                    log.info("Internet was down while checking %s; it will be checked again", url)
                    continue
                if res.status == DOMAIN_NOT_FOUND and second_chance:
                    second_chance = False  # a short DNS hiccup must not turn a real business into "not found"
                    await asyncio.sleep(DNS_RETRY_DELAY)
                    continue
                return res

        async def worker(url: str) -> None:
            nonlocal done
            res = await check(url)
            if not res.business_name and res.status != DOMAIN_NOT_FOUND:  # unreachable: use the web address
                res.business_name = name_from_address(res.final_url or res.url)
            results[url] = res
            done += 1
            if on_progress:
                saved = on_progress(done, total, res)
                if inspect.isawaitable(saved):
                    await saved

        tasks = [asyncio.create_task(worker(u)) for u in unique]
        try:
            await asyncio.gather(*tasks)
        finally:  # on an error (e.g. the disk is full) or a stop, don't leave other sites running
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return [results[u] if u else SiteResult(url="", status="skipped: no valid website address") for u in urls]
