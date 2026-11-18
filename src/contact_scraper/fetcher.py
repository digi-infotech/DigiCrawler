"""Downloads pages: plain HTTP first, then a real headless browser when a site refuses plain
HTTP or only shows its content with JavaScript."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .models import Settings

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BYTES = 3_000_000
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_BACKOFF = 1.5  # seconds; grows with each attempt
BLOCK_STATUS = {401, 403, 429, 503}  # answers that usually mean "you look like a bot"


class FetchError(Exception):
    """A page could not be downloaded. The message is short and human readable.

    `status` is the HTTP status (e.g. 403) when the server answered; `kind` is "dns" when the
    domain doesn't exist (no point trying a browser then).
    """

    def __init__(self, message: str, status: int | None = None, kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.kind = kind

    @property
    def blocked(self) -> bool:
        """The server refused us, so a real browser is worth a try."""
        return self.status in BLOCK_STATUS


def status_message(code: int) -> str:
    if code in (401, 403):
        return f"website blocked automated visits ({code})"
    if code == 429:
        return "website limited automated visits (429)"
    if code == 404:
        return "page not found (404)"
    if code >= 500:
        return f"website is having problems ({code})"
    return f"website returned error {code}"


class Fetcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = httpx.Timeout(settings.timeout, connect=min(settings.timeout, 10))
        limits = httpx.Limits(max_connections=settings.concurrency * 2, max_keepalive_connections=settings.concurrency)
        common = dict(headers=HEADERS, follow_redirects=True, timeout=timeout, limits=limits, max_redirects=8)
        self.client = httpx.AsyncClient(**common)
        # Many small-business sites have expired/misconfigured certificates. We only read
        # public pages, so we retry those once without certificate checks.
        self.insecure_client = httpx.AsyncClient(verify=False, **common)
        self._robots: dict[str, asyncio.Future[RobotFileParser | None]] = {}
        self.browser = BrowserRenderer(settings) if settings.use_browser else None

    async def close(self) -> None:
        await self.client.aclose()
        await self.insecure_client.aclose()
        if self.browser:
            await self.browser.close()

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------ robots.txt

    async def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:  # one robots.txt download per site, shared by all callers
            self._robots[origin] = asyncio.ensure_future(self._load_robots(origin))
        parser = await self._robots[origin]
        return True if parser is None else parser.can_fetch("*", url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        try:
            resp = await self.insecure_client.get(f"{origin}/robots.txt", timeout=8)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or "text/html" in resp.headers.get("content-type", ""):
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

    # ------------------------------------------------------------ fetching

    async def fetch(self, url: str) -> tuple[str, str]:
        """Return (final_url, html). Raises FetchError with a friendly message."""
        last_error: FetchError | None = None
        for attempt in range(3):
            try:
                return await self._fetch_once(self.client, url)
            except httpx.ConnectError as exc:
                if _is_ssl_error(exc):
                    try:
                        return await self._fetch_once(self.insecure_client, url)
                    except httpx.HTTPError as exc2:
                        last_error = FetchError(_describe(exc2))
                        break
                last_error = FetchError(_describe(exc), kind="dns" if _is_dns_error(exc) else None)
                if last_error.kind == "dns":
                    break  # domain does not exist; retrying won't help
            except _RetryableStatus as exc:
                last_error = FetchError(status_message(exc.status), status=exc.status)
            except FetchError:
                raise
            except httpx.HTTPError as exc:
                last_error = FetchError(_describe(exc))
            if attempt < 2:
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def _fetch_once(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        async with client.stream("GET", url) as resp:
            if resp.status_code in RETRY_STATUS:
                raise _RetryableStatus(resp.status_code)
            if resp.status_code >= 400:
                raise FetchError(status_message(resp.status_code), status=resp.status_code)
            ctype = resp.headers.get("content-type", "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
                raise FetchError("not a web page")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    break
            del body[MAX_BYTES:]  # a single chunk can be bigger than the limit
            encoding = resp.encoding or "utf-8"
            try:
                text = body.decode(encoding, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return str(resp.url), text


class _RetryableStatus(Exception):
    def __init__(self, status: int):
        self.status = status


def _is_ssl_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "ssl" in msg or "certificate" in msg or "tls" in msg


def _is_dns_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in ("name or service not known", "getaddrinfo", "nodename nor servname",
                                  "name resolution", "no address associated"))


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "website took too long to respond"
    if _is_dns_error(exc):
        return "website address not found"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too many redirects"
    if _is_ssl_error(exc):
        return "secure connection failed"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect to website"
    return "could not load website"


# ---------------------------------------------------------------- headless browser

class BrowserRenderer:
    """Headless Chromium via Playwright, started only when first needed.

    Each website gets its own BrowserSession (one browser context), so cookies the site sets,
    such as a consent choice or a passed "checking your browser" step, carry over from the
    homepage to its contact pages, exactly as for a person clicking around.
    """

    CHALLENGE_WAIT = 15  # seconds a "Just a moment..." page gets to finish on its own

    def __init__(self, settings: Settings):
        self.settings = settings
        self.slots = asyncio.Semaphore(int(os.getenv("BROWSER_CONCURRENCY", "3")))
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self.available = True

    async def ensure(self) -> bool:
        async with self._lock:
            if self._browser or not self.available:
                return self.available
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                try:  # full Chromium in headless mode renders exactly like the normal browser
                    self._browser = await self._pw.chromium.launch(headless=True, channel="chromium")
                except Exception:
                    self._browser = await self._pw.chromium.launch(headless=True)
            except Exception:  # Playwright not installed or Chromium missing
                log.warning("Browser fallback unavailable (build the Docker image with WITH_BROWSER=true)")
                self.available = False
            return self.available

    def session(self) -> "BrowserSession":
        return BrowserSession(self)

    async def render(self, url: str) -> tuple[str, str] | None:
        """One-off render in a fresh session."""
        async with self.session() as session:
            return await session.render(url)

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


class BrowserSession:
    """One website's pages, rendered one after another in the same browser context."""

    def __init__(self, renderer: BrowserRenderer):
        self.renderer = renderer
        self._context = None
        self._slot_held = False

    async def __aenter__(self) -> "BrowserSession":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> bool:
        """Wait for a free browser slot and open a context. False if no browser is available."""
        if self._context:
            return True
        if not await self.renderer.ensure():
            return False
        await self.renderer.slots.acquire()
        self._slot_held = True
        try:
            self._context = await self.renderer._browser.new_context(
                user_agent=USER_AGENT, locale="en-US", viewport={"width": 1366, "height": 900},
                ignore_https_errors=True)
        except Exception:
            log.warning("Could not open a browser context", exc_info=True)
            await self.close()
            return False

        async def skip_heavy(route):  # pictures, videos and fonts never hold contact text
            if route.request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await self._context.route("**/*", skip_heavy)
        return True

    async def render(self, url: str) -> tuple[str, str] | None:
        """Open the page the way a visitor would; returns (final_url, html) or None."""
        from .extract import looks_blocked

        try:
            if not await self.start():
                return None
            page = await self._context.new_page()
        except Exception:
            log.warning("Could not open a browser page for %s", url, exc_info=True)
            return None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.renderer.settings.timeout * 1000)
            await _settle(page)
            html = await page.content()
            # "Checking your browser..." pages usually finish by themselves within seconds.
            deadline = time.monotonic() + self.renderer.CHALLENGE_WAIT
            while looks_blocked(html) and time.monotonic() < deadline:
                await page.wait_for_timeout(1500)
                html = await page.content()
            # Footers and contact blocks are often loaded only once scrolled into view.
            await page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
            await _settle(page, 2500)
            return page.url, await page.content()
        except Exception:
            return None
        finally:
            await page.close()

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._slot_held:
            self.renderer.slots.release()
            self._slot_held = False


async def _settle(page, ms: int = 5000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass
