# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DigiCrawler (package `contact_scraper`, sometimes called "Contact Finder"): upload an Excel file of business websites, get back the same rows plus `business_name`, `website_final`, `state`, `country`, numbered `email_N` / `mobile_N` / `phone_N` columns (one value per cell), `source_pages` and `status`. It is an internal tool. The primary user is a **non-technical manager** who runs it through `start.bat` and the web page at http://localhost:3003, so keep user-facing text (status strings, `HTTPException` messages, UI copy) in plain English. Developers use the CLI.

`ONBOARDING.md` is the user and developer manual. **Keep it in sync** whenever you change a command, setting, output column, status string or crawl behaviour.

## Commands

Local (Windows; the repo's `.venv` already exists):

```bash
.venv/Scripts/python -m pip install -e ".[dev]"     # use python -m pip; pip.exe is blocked on this machine
.venv/Scripts/python -m pytest                       # full suite, offline, ~80 s
.venv/Scripts/python -m pytest tests/test_extract.py::test_name -q   # single test
.venv/Scripts/python -m pytest -k browser            # by keyword
.venv/Scripts/python -X utf8 -m contact_scraper.cli examples/sample.xlsx -o data/out.xlsx --region IN   # the .exe launcher is blocked here
.venv/Scripts/python -m uvicorn contact_scraper.api:app --reload --port 8000   # API docs at /api/docs
```

Docker (the full stack, including the upload page):

```bash
docker compose up -d --build        # http://localhost:${PORT:-3003}
docker compose logs -f app
docker compose run --rm app contact-scraper /data/input.xlsx -o /data/output.xlsx --region IN
```

In Git Bash, prefix container paths with `MSYS_NO_PATHCONV=1`. There is no linter or formatter configured.

## Architecture

```
Browser -> nginx :3003 -> /       web/index.html (one static file, vanilla JS)
                       -> /api/*  FastAPI app :8000 (single uvicorn worker)
```

The pipeline runs in this order, and both the CLI and the API share everything below `runner.run`:

1. **`excel_io.py`**: `load_rows` / `read_sheet` read the workbook. `detect_url_column` picks the website column by header name or by the share of domain-like values. `normalize_url` canonicalises each cell. `write_results` appends the result columns to the original ones. `output_columns` sizes each numbered kind to the most any row has (minimum 1, capped at `MAX_PER_KIND`). `phone_N` holds `SiteResult.landlines`, meaning phones that aren't mobiles, so mobiles aren't repeated.
2. **`runner.run(urls, settings, on_progress)`**: dedupes URLs, crawls them under a `Settings.concurrency` semaphore inside one shared `Fetcher`, and returns results **in input order** (blank URL becomes a `skipped:` row). It never lets one site's exception break the run, and it falls back to `names.name_from_address` when no name was found.
3. **`crawler.crawl_site`**: one site, in two passes, each with its own time budget (`HTTP_BUDGET` and `BROWSER_BUDGET` × `settings.timeout`):
   - **HTTP pass**: homepage first, then `extract.plan_pages` chooses up to `max_pages-1` contact/about/services links, plus guessed probe paths such as `/contact-us`. `_SitePages` drops block pages, duplicates (by URL or text hash) and soft-404s on guessed paths.
   - The HTTP pass returns an `_HttpOutcome` holding the best result so far and an optional `browser_step` closure. There are three triggers: the homepage was refused or blocked (`_browser_from_start`), nothing was found (`look_again`), or contacts were found but some contact pages were refused (`open_refused`, which gives `method="http+browser"`). DNS failures never go to the browser. If the browser fails or times out, the HTTP result is kept.
   - `collect_contacts` merges all pages, detects the site's region, extracts emails and phones, picks the business name, and sets `state`/`country` via `address.detect_location`.
4. **`fetcher.py`**: `Fetcher` holds two httpx clients (normal, plus `verify=False` for broken certificates), retries, and a robots.txt cache. `FetchError` has `.status`, `.kind == "dns"` and `.blocked` (401/403/429/503). `BrowserRenderer` starts Playwright lazily on first use and is limited by `BROWSER_CONCURRENCY`. It sets `available=False` if Playwright or Chromium is missing, so the app degrades to HTTP-only. `BrowserSession` uses one context per site, so cookies persist across that site's pages.
5. **`extract.py`**: pure functions only, no I/O. They read only visible text (`visible_text` skips script/style/etc.), plus mailto/tel/WhatsApp links, Cloudflare-obfuscated emails and JSON-LD. Phones go through `phonenumbers`. Fax numbers, placeholder numbers and special-rate numbers (`SPECIAL_RATE_TYPES`: toll-free, shared-cost, premium) are dropped, so only mobile, WhatsApp and telephone numbers are kept. A number counts as mobile from the numbering plan, except in US/CA, where it needs a label nearby. `detect_region` picks the site's country from the ccTLD, then address patterns or `+CC` prefixes, then JSON-LD, and only last from the user's fallback region.
6. **`address.py`**: `detect_location` returns (state, country name) using **only postal-address clues**: schema.org `PostalAddress` (JSON-LD or microdata, weight 5) and written patterns (US/CA/AU state+postcode, UK postcode area → nation, Indian PIN with a nearby state name, weight 3). The answer is the most-voted country, then the most-voted state within it. Unlike `extract.detect_region`, which only chooses how to read phone numbers, it deliberately ignores the ccTLD and phone prefixes, so a site with no address gets blanks. Country names come from `phonenumbers`' bundled geodata (no extra dependency).
7. **`names.py`**: scores candidate business names from JSON-LD, og:site_name, the title, the logo alt text and the © line (see `SCORES`).

**API (`api.py`)**: the flow is `POST /api/uploads` (column preview) → `POST /api/jobs` (starts an `asyncio.Task`) → poll `GET /api/jobs/{id}?since=N` (counters plus an incremental feed) → `GET .../results` and `.../download`. `POST .../cancel` cancels the task, and the `CancelledError` handler still writes partial results. Job state lives **in memory** (`JOBS`, `UPLOADS` dicts), so a restart loses running jobs, while files in `data/jobs/<id>/` are kept for 7 days. That is why the app runs with `--workers 1`. Blocking work (openpyxl, parsing) goes through `run_in_threadpool` / `asyncio.to_thread`.

**Settings**: `models.Settings.from_env()` reads `.env` (see `.env.example`). Note that the dataclass default is `use_browser=False` while the env default is `WITH_BROWSER=true`, so tests that build `Settings(...)` directly run without a browser unless they ask for one. The `WITH_BROWSER` build arg also decides whether Chromium goes into the image (about 2 GB with it, about 285 MB without).

## Tests

Tests never touch the network. `tests/test_crawler.py::FakeWeb` serves simulated sites through `respx`, keyed by origin and path, and records requests (`web.fetched(origin)`). `tests/test_browser_fallback.py` monkeypatches a `FakeBrowser`/`FakeSession` in place of Playwright. `tests/test_api.py` monkeypatches `api.run` and `api.JOBS_DIR`. pytest runs with `asyncio_mode = "auto"`, so async tests need no decorator.

## Environment gotchas (this Windows machine)

- An Application Control policy blocks some compiled Python extension DLLs (selectolax failed). Prefer pure-Python dependencies. That is why `extract.HTMLParser` is an alias for BeautifulSoup with the built-in parser. If a compiled package can't be avoided, check that it imports locally, or rely on Docker.
- Python's default file encoding here is cp1252. Always pass `encoding="utf-8"` when reading or writing files in scripts; the test data contains characters like "₹".
