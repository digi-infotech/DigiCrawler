"""Tells "the internet connection is down" apart from "this website is down".

Without it, every website checked while the computer is offline (or waking up from sleep) would be
saved as "website address not found" or "could not connect", and look like a dead business.

The guard connects to a few big, always-up hosts. A finished website's result is accepted only once
a check that *started after that website finished* has succeeded. If that check fails, the run
pauses (``wait_online``) and the website is checked again once the connection is back. Checks run
only while results are waiting for one, or every few seconds while offline, so an idle or healthy
run makes about one small connection per second at most.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_HOSTS = "www.google.com:443,one.one.one.one:443,www.microsoft.com:443"
CHECK_TIMEOUT = 5.0
BATCH_DELAY = 0.5      # wait this long after a website finishes, so one check covers several of them
OFFLINE_INTERVAL = 5.0  # how often to look again while offline

Check = Callable[[], Awaitable[bool]]


def check_hosts() -> list[tuple[str, int]]:
    out = []
    for item in os.getenv("ONLINE_CHECK_HOSTS", DEFAULT_HOSTS).split(","):
        host, _, port = item.strip().rpartition(":")
        if host and port.isdigit():
            out.append((host, int(port)))
    return out


async def internet_ok(hosts: list[tuple[str, int]] | None = None, timeout: float = CHECK_TIMEOUT) -> bool:
    """True when any of the hosts can be looked up (DNS) and connected to (TCP)."""

    async def one(host: str, port: int) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    tasks = [asyncio.ensure_future(one(h, p)) for h, p in (hosts or check_hosts())]
    try:
        for fut in asyncio.as_completed(tasks):
            if await fut:
                return True
        return False
    finally:
        for t in tasks:
            t.cancel()


class NetworkGuard:
    def __init__(self, check: Check | None = None, on_change: Callable[[bool], None] | None = None,
                 batch_delay: float = BATCH_DELAY, offline_interval: float = OFFLINE_INTERVAL):
        self._check = check or internet_ok
        self.on_change = on_change
        self.batch_delay = batch_delay
        self.offline_interval = offline_interval
        self._online = asyncio.Event()
        self._online.set()
        self._wanted = asyncio.Event()
        self._started = 0  # checks started so far
        self._waiters: list[tuple[int, asyncio.Future[bool]]] = []
        self._task: asyncio.Task | None = None
        self.offline_since: float | None = None

    @property
    def online(self) -> bool:
        return self._online.is_set()

    async def __aenter__(self) -> "NetworkGuard":
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for _, fut in self._waiters:
            fut.cancel()
        self._waiters.clear()

    async def wait_online(self) -> None:
        await self._online.wait()

    async def confirm(self) -> bool:
        """Call when a website has finished. True: the connection was fine, keep the result.
        False: the connection was down, so check the website again (after ``wait_online``)."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((self._started, fut))
        self._wanted.set()
        return await fut

    async def _loop(self) -> None:
        while True:
            if self.online:
                await self._wanted.wait()
                await asyncio.sleep(self.batch_delay)
            self._wanted.clear()
            self._started += 1
            number = self._started
            try:
                ok = await self._check()
            except Exception:
                log.exception("Internet check failed")
                ok = False
            waiting = []
            for after, fut in self._waiters:
                if number > after:  # this check started after that website finished
                    if not fut.done():
                        fut.set_result(ok)
                else:
                    waiting.append((after, fut))
            self._waiters = waiting
            if waiting:
                self._wanted.set()
            self._set_online(ok)
            if not ok:
                await asyncio.sleep(self.offline_interval)

    def _set_online(self, ok: bool) -> None:
        if ok == self.online:
            return
        if ok:
            log.warning("Internet connection is back after %.0f s; carrying on", time.monotonic() - (self.offline_since or 0))
            self.offline_since = None
            self._online.set()
        else:
            log.warning("Internet connection lost; pausing until it is back")
            self.offline_since = time.monotonic()
            self._online.clear()
        if self.on_change:
            try:
                self.on_change(ok)
            except Exception:
                log.exception("on_change callback failed")


class NoGuard:
    """Used when the internet check is switched off (and in tests): every result is accepted."""

    online = True

    async def __aenter__(self) -> "NoGuard":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def wait_online(self) -> None:
        return None

    async def confirm(self) -> bool:
        return True
