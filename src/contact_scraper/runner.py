"""Runs the crawler over every row, in parallel, reporting progress."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .crawler import crawl_site
from .fetcher import Fetcher
from .models import Settings, SiteResult
from .names import name_from_address

DOMAIN_NOT_FOUND = "error: website address not found"  # no such website, so no name either

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, SiteResult], None]


async def run(urls: list[str], settings: Settings, on_progress: ProgressCallback | None = None) -> list[SiteResult]:
    """Scrape each URL (blank = skipped). Returns one result per input, same order."""
    unique = list(dict.fromkeys(u for u in urls if u))
    total = len(unique)
    done = 0
    results: dict[str, SiteResult] = {}
    sem = asyncio.Semaphore(settings.concurrency)

    async with Fetcher(settings) as fetcher:

        async def worker(url: str) -> None:
            nonlocal done
            async with sem:
                try:
                    # crawl_site keeps its own time limits (one for plain HTTP, one for the browser).
                    res = await crawl_site(url, fetcher, settings)
                except Exception as exc:  # never let one site break the whole run
                    log.exception("Unexpected error for %s", url)
                    res = SiteResult(url=url, status=f"error: {type(exc).__name__}")
            if not res.business_name and res.status != DOMAIN_NOT_FOUND:  # unreachable: use the web address
                res.business_name = name_from_address(res.final_url or res.url)
            results[url] = res
            done += 1
            if on_progress:
                on_progress(done, total, res)

        await asyncio.gather(*(worker(u) for u in unique))

    return [results[u] if u else SiteResult(url="", status="skipped: no valid website address") for u in urls]
