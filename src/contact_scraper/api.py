"""Small web API used by the upload page (web/index.html).

Flow: POST /api/uploads (file -> column preview)  ->  POST /api/jobs (start)
      -> GET /api/jobs/{id}?since=N (progress + live feed)  ->  GET /api/jobs/{id}/results
      -> GET /api/jobs/{id}/download.   POST /api/jobs/{id}/cancel stops early and keeps results.
      POST /api/jobs/{id}/retry checks failed and unchecked websites again. GET /api/jobs lists recent checks.

The disk is the source of truth (see jobstore): every result is saved before the page hears about
it, and when the app starts it reloads every job and carries on with the unfinished ones. Memory
only holds a copy for quick answers.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import phonenumbers
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from openpyxl import Workbook
from starlette.background import BackgroundTask

from . import jobstore
from .excel_io import (InputError, Sheet, describe_columns, detect_url_column, label_column, load_rows, read_sheet,
                       write_results)
from .models import Settings, SiteResult
from .runner import run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("contact_scraper.api")

DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
MAX_ROWS = int(os.getenv("MAX_ROWS", "5000"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
KEEP_JOBS_DAYS = 7
FEED_PAGE = 200
SAVE_EVERY = 25       # also refresh job.json after this many websites (counters and the clock)
RECENT_JOBS = 20
SHUTDOWN_WAIT = 10    # seconds to let running jobs save their state when the app stops
STOPPED = "skipped: stopped before this website was checked"
NOT_YET = "skipped: not checked yet"
NO_URL = "skipped: no valid website address"
FAILED = "Something went wrong while checking. Click “Check again” to carry on — nothing found so far is lost."
DISK_FAILED = ("Couldn't save results on this computer. Is the disk full? Free up some space, then click "
               "“Check again” to carry on — nothing found so far is lost.")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass
class Upload:
    id: str
    filename: str
    path: Path


@dataclass
class Job:
    id: str
    filename: str
    total: int
    input_name: str = "input.xlsx"
    analytics: bool = False  # off: quick check, simple file (only rows with an email or mobile)
    region: str = ""
    url_column: int = 0
    status: str = "running"  # running | done | cancelled | error
    offline: bool = False    # paused because the internet connection is down
    done: int = 0
    with_contacts: int = 0
    errors: int = 0
    emails: int = 0
    mobiles: int = 0
    phones: int = 0
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float | None = None
    active: float = 0.0             # seconds spent checking before `started` (e.g. before a restart)
    started: float | None = None    # when this run of the app started or resumed checking
    loaded: bool = True             # False: only job.json is in memory so far; the rest is read on first use
    stop_requested: bool = False    # "Stop" was clicked (a shutdown also cancels the task, but must not stop the job)
    task: asyncio.Task | None = None
    urls: list[str] = field(default_factory=list)   # one per input row, "" when the row has no website
    names: list[str] = field(default_factory=list)  # label column, one per input row
    name_of: dict[str, str] = field(default_factory=dict)
    feed: list[dict] = field(default_factory=list)
    partial: dict[str, SiteResult] = field(default_factory=dict)  # finished websites (a copy of results.jsonl)
    rows: list[dict] | None = None  # final results, in input order

    def __post_init__(self) -> None:
        self.files = jobstore.JobFiles(JOBS_DIR / self.id)

    @property
    def dir(self) -> Path:
        return self.files.dir

    @property
    def elapsed(self) -> float:
        return self.active + (time.time() - self.started if self.started else 0)

    def meta(self) -> dict:
        return {
            "version": 1, "id": self.id, "filename": self.filename, "input": self.input_name, "total": self.total,
            "analytics": self.analytics, "region": self.region, "url_column": self.url_column,
            "status": self.status, "error": self.error, "created": self.created, "finished": self.finished,
            "active": round(self.elapsed, 1), "done": self.done, "with_contacts": self.with_contacts,
            "errors": self.errors, "emails": self.emails, "mobiles": self.mobiles, "phones": self.phones,
        }

    @classmethod
    def from_meta(cls, m: dict) -> "Job":
        return cls(id=m["id"], filename=m["filename"], total=m["total"], input_name=m.get("input", "input.xlsx"),
                   analytics=m.get("analytics", False), region=m.get("region", ""),
                   url_column=m.get("url_column", 0), status=m.get("status", "running"), error=m.get("error", ""),
                   created=m.get("created", time.time()), finished=m.get("finished"), active=m.get("active", 0.0),
                   done=m.get("done", 0), with_contacts=m.get("with_contacts", 0), errors=m.get("errors", 0),
                   emails=m.get("emails", 0), mobiles=m.get("mobiles", 0), phones=m.get("phones", 0), loaded=False)

    def public(self, since: int | None = None) -> dict:
        data = {
            "id": self.id, "filename": self.filename, "analytics": self.analytics, "status": self.status,
            "offline": self.offline, "done": self.done,
            "total": self.total, "with_contacts": self.with_contacts, "errors": self.errors,
            "emails": self.emails, "mobiles": self.mobiles, "phones": self.phones, "error": self.error,
            "to_retry": self.errors + max(0, self.total - self.done),  # failed + not checked yet
            "created": self.created, "elapsed": round(self.elapsed, 1),
        }
        if since is not None:
            data["feed"] = self.feed[since:since + FEED_PAGE]
            data["feed_total"] = len(self.feed)
        return data


UPLOADS: dict[str, Upload] = {}
JOBS: dict[str, Job] = {}


def summary(res: SiteResult, name: str = "") -> dict:
    """One result row as the page shows it."""
    return {
        "name": name or res.business_name, "url": res.url, "site": res.final_url or res.url, "emails": res.emails,
        "mobiles": res.mobiles, "phones": res.landlines, "address": res.address, "state": res.state,
        "country": res.country,
        "pages": res.pages, "status": res.status, "method": res.method,
    }


# ------------------------------------------------------------------ job state (memory copy of the disk)

def _count(job: Job, res: SiteResult, sign: int) -> None:
    job.with_contacts += sign * (res.status == "ok")
    job.errors += sign * res.status.startswith("error")
    job.emails += sign * len(res.emails)
    job.mobiles += sign * len(res.mobiles)
    job.phones += sign * len(res.phones)


def _apply(job: Job, res: SiteResult) -> None:
    """Add one finished website to the memory copy (it is already on disk)."""
    old = job.partial.pop(res.url, None)
    if old:
        _count(job, old, -1)
    job.partial[res.url] = res
    _count(job, res, 1)
    job.done = len(job.partial)
    job.feed.append({"i": len(job.feed), **summary(res, job.name_of.get(res.url, ""))})


def _set_results(job: Job, results: dict[str, SiteResult]) -> None:
    job.partial, job.feed = {}, []
    job.done = job.with_contacts = job.errors = job.emails = job.mobiles = job.phones = 0
    for res in results.values():
        _apply(job, res)


def _final_results(job: Job, results: dict[str, SiteResult], missing: str) -> list[SiteResult]:
    """One result per input row; websites without a saved result get the `missing` status."""
    return [results.get(u) or SiteResult(u, status=missing) if u else SiteResult("", status=NO_URL) for u in job.urls]


def _load_sync(job: Job) -> None:
    """Fill a job from its files: website list, every saved result, and the result rows when finished."""
    plan = jobstore.read_json(job.files.plan)
    if not plan:
        raise ValueError(f"plan.json missing for job {job.id}")
    job.urls, job.names = plan["urls"], plan["names"]
    job.name_of = {}
    for u, n in zip(job.urls, job.names):
        job.name_of.setdefault(u, n)
    results = job.files.load_results()
    _set_results(job, results)
    if job.status in ("done", "cancelled"):
        job.rows = [summary(r, n) for r, n in zip(_final_results(job, results, STOPPED), job.names)]
    job.loaded = True


async def _ensure_loaded(job: Job) -> None:
    if job.loaded:
        return
    try:
        await asyncio.to_thread(_load_sync, job)  # loading twice at once is harmless: same files, same result
    except Exception:
        log.exception("Could not read job %s", job.id)
        raise HTTPException(500, "This check's saved files couldn't be read. Please upload the file again.")


async def _save(job: Job) -> None:
    await asyncio.to_thread(jobstore.write_json, job.files.meta, job.meta())


def _stop_clock(job: Job) -> None:
    job.active = job.elapsed
    job.started = None


def _write_output(job: Job, results: list[SiteResult], dest: Path, sheet: Sheet | None = None) -> None:
    """Write the Excel file next to `dest`, then move it into place, so `dest` is never half written."""
    if sheet is None:
        headers, rows = load_rows(job.dir / job.input_name)
        sheet = Sheet(headers=headers, rows=rows, url_column=job.url_column)
    tmp = dest.with_name(f".{uuid.uuid4().hex}.xlsx")
    try:
        write_results(tmp, sheet, results, job.analytics)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _is_disk_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and not isinstance(exc, ConnectionError)


# ------------------------------------------------------------------ running a job

async def _record(job: Job, res: SiteResult) -> None:
    await asyncio.to_thread(job.files.append_result, res)  # on disk first...
    _apply(job, res)                                        # ...then in memory
    if job.done % SAVE_EVERY == 0:
        await _save(job)


def _network(job: Job, online: bool) -> None:
    job.offline = not online
    log.warning("Job %s %s", job.id, "carrying on: internet is back" if online else "paused: no internet connection")


async def _finish(job: Job, status: str, sheet: Sheet | None) -> None:
    results = await asyncio.to_thread(job.files.load_results)  # the disk is the source of truth
    _set_results(job, results)
    final = _final_results(job, results, STOPPED)
    await asyncio.to_thread(_write_output, job, final, job.files.output, sheet)
    job.rows = [summary(r, n) for r, n in zip(final, job.names)]
    job.status, job.offline, job.finished = status, False, time.time()
    _stop_clock(job)
    await _save(job)
    log.info("Job %s %s: %d/%d sites, %d with contacts", job.id, status, job.done, job.total, job.with_contacts)


async def _process(job: Job, sheet: Sheet | None = None) -> None:
    settings = Settings.from_env()
    settings.analytics = job.analytics
    settings.region = job.region or settings.region
    job.started, job.offline = time.time(), False
    try:
        try:
            todo = [u for u in job.urls if u and u not in job.partial]
            if todo:
                await run(todo, settings, lambda _d, _t, res: _record(job, res), lambda ok: _network(job, ok))
            status = "done"
        except asyncio.CancelledError:
            if not job.stop_requested:
                raise
            status = "cancelled"
        await _finish(job, status, sheet)
    except asyncio.CancelledError:
        # The app is shutting down, not "Stop": leave the job running so it carries on at the next start.
        job.offline = False
        try:
            jobstore.write_json(job.files.meta, job.meta())
        except OSError:
            log.exception("Could not save job %s while shutting down", job.id)
        log.info("Job %s paused at %d/%d; it will carry on when the app starts again", job.id, job.done, job.total)
        raise
    except Exception as exc:
        log.exception("Job %s failed", job.id)
        job.status, job.offline, job.finished = "error", False, time.time()
        job.error = DISK_FAILED if _is_disk_error(exc) else FAILED
        _stop_clock(job)
        try:
            await _save(job)
        except OSError:
            log.exception("Could not save job %s", job.id)


def _start(job: Job, sheet: Sheet | None = None) -> None:
    job.stop_requested = False
    job.task = asyncio.create_task(_process(job, sheet))


# ------------------------------------------------------------------ startup: reload everything from disk

def _scan() -> list[Job | Upload]:
    found: list[Job | Upload] = []
    if not JOBS_DIR.exists():
        return found
    for folder in sorted(JOBS_DIR.iterdir()):
        if not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{12}", folder.name):
            continue
        try:
            files = jobstore.JobFiles(folder)
            meta = jobstore.read_json(files.meta)
            if meta:
                job = Job.from_meta(meta)
                if job.status == "running":
                    _load_sync(job)  # unfinished jobs need everything now, to carry on
                found.append(job)
                continue
            info = jobstore.read_json(files.upload)
            inputs = list(folder.glob("input.*"))
            if info and inputs:
                found.append(Upload(folder.name, info.get("filename", inputs[0].name), inputs[0]))
        except Exception:  # one damaged folder must not stop the app from starting
            log.exception("Skipping unreadable job folder %s", folder)
    return found


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(_cleanup_old_jobs)
    for item in await asyncio.to_thread(_scan):
        if isinstance(item, Upload):
            UPLOADS.setdefault(item.id, item)
        elif item.id not in JOBS:
            JOBS[item.id] = item
            if item.status == "running":
                log.info("Carrying on with job %s (%s): %d of %d websites already checked",
                         item.id, item.filename, item.done, item.total)
                _start(item)
    yield
    running = [j.task for j in JOBS.values() if j.task and not j.task.done()]
    for task in running:
        task.cancel()  # not "Stop": _process saves the job as still running
    if running:
        await asyncio.wait(running, timeout=SHUTDOWN_WAIT)


app = FastAPI(title="DigiCrawler", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)


def _cleanup_old_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - KEEP_JOBS_DAYS * 86400
    for d in JOBS_DIR.iterdir():
        job = JOBS.get(d.name)
        if job and job.status == "running":
            continue
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            JOBS.pop(d.name, None)
            UPLOADS.pop(d.name, None)


async def _save_upload(file: UploadFile) -> Upload:
    name = Path(file.filename or "upload.xlsx").name
    if not name.lower().endswith((".xlsx", ".xlsm", ".xls")):
        raise HTTPException(400, "Please upload an Excel file (.xlsx).")
    await run_in_threadpool(_cleanup_old_jobs)
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "The file is too large (limit 20 MB).")
    upload_id = uuid.uuid4().hex[:12]
    folder = JOBS_DIR / upload_id
    path = folder / ("input" + Path(name).suffix.lower())

    def save() -> None:
        folder.mkdir(parents=True, exist_ok=True)
        jobstore.write_atomic(path, content)
        jobstore.write_json(folder / "upload.json", {"filename": name})

    try:
        await run_in_threadpool(save)
    except OSError:
        log.exception("Could not save upload")
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(507, "Couldn't save the file on this computer. Is the disk full?")
    return Upload(upload_id, name, path)


def _discard(upload: Upload) -> None:
    shutil.rmtree(upload.path.parent, ignore_errors=True)
    UPLOADS.pop(upload.id, None)


# ------------------------------------------------------------------ simple endpoints

@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
async def config() -> dict:
    return {"region": Settings.from_env().region, "max_rows": MAX_ROWS}


@app.get("/api/sample")
async def sample() -> Response:
    wb = Workbook()
    ws = wb.active
    ws.title = "Websites"
    ws.append(["Company", "Website", "City"])
    for row in [("Acme Plumbing", "acmeplumbing.com", "Austin"), ("Sharma Sweets", "www.sharmasweets.in", "Pune"),
                ("Beta Dental", "https://www.betadental.co.uk", "London")]:
        ws.append(list(row))
    for col, width in zip("ABC", (24, 36, 16)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), headers={"Content-Disposition": 'attachment; filename="contact-finder-sample.xlsx"'},
                    media_type=XLSX)


# ------------------------------------------------------------------ upload + preview

@app.post("/api/uploads")
async def create_upload(file: UploadFile = File(...)) -> dict:
    """Save the file and describe it, so the page can confirm the website column before starting."""
    upload = await _save_upload(file)
    try:
        headers, rows = await run_in_threadpool(load_rows, upload.path, MAX_ROWS)
    except InputError as exc:
        _discard(upload)
        raise HTTPException(400, str(exc))
    columns = describe_columns(headers, rows)
    if not any(c["valid"] for c in columns):
        _discard(upload)
        raise HTTPException(400, "Couldn't find any website addresses in this file. "
                                 "Put them in one column (like acme.com) and try again.")
    try:
        detected = detect_url_column(headers, rows)
    except InputError:
        detected = max(range(len(columns)), key=lambda i: columns[i]["valid"])
    UPLOADS[upload.id] = upload
    return {"upload_id": upload.id, "filename": upload.filename, "size": upload.path.stat().st_size,
            "rows": len(rows), "columns": columns, "detected": detected}


# ------------------------------------------------------------------ jobs

@app.post("/api/jobs")
async def create_job(
    file: UploadFile | None = File(None),
    upload_id: str = Form(""),
    url_column: str = Form(""),
    region: str = Form(""),
    analytics: bool = Form(False),
) -> dict:
    region = (region or Settings.from_env().region).upper()
    if region not in phonenumbers.SUPPORTED_REGIONS:
        raise HTTPException(400, "Please choose a valid country.")

    if upload_id:
        upload = UPLOADS.get(upload_id)
        if not upload or not upload.path.exists():
            raise HTTPException(404, "That file is no longer here. Please choose it again.")
    elif file is not None:
        upload = await _save_upload(file)
    else:
        raise HTTPException(400, "Please choose an Excel file first.")

    try:
        sheet: Sheet = await run_in_threadpool(read_sheet, upload.path, url_column or None, MAX_ROWS)
    except InputError as exc:
        if not upload_id:
            _discard(upload)
        raise HTTPException(400, str(exc))

    urls = sheet.urls
    if not any(urls):
        if not upload_id:
            _discard(upload)
        raise HTTPException(400, f"No website addresses found in column '{sheet.headers[sheet.url_column]}'.")

    label = label_column(sheet.headers, sheet.rows, sheet.url_column)
    names = [str(r[label]).strip() if label is not None and r[label] not in (None, "") else "" for r in sheet.rows]
    job = Job(id=upload.id, filename=upload.filename, total=len(set(u for u in urls if u)),
              input_name=upload.path.name, analytics=analytics, region=region, url_column=sheet.url_column,
              urls=urls, names=names)
    for u, n in zip(urls, names):
        job.name_of.setdefault(u, n)

    def create_files() -> None:
        jobstore.write_json(job.files.plan, {"urls": urls, "names": names})
        jobstore.write_json(job.files.meta, job.meta())  # written last: its presence means "a complete job"

    try:
        await run_in_threadpool(create_files)
    except OSError:
        log.exception("Could not save job %s", job.id)
        raise HTTPException(507, "Couldn't save the check on this computer. Is the disk full?")
    UPLOADS.pop(upload.id, None)
    JOBS[job.id] = job
    _start(job, sheet)
    log.info("Job %s started: %s (%d websites, region %s, %s)", job.id, job.filename, job.total, region,
             "analytics" if analytics else "quick")
    return job.public(since=0)


@app.get("/api/jobs")
async def list_jobs() -> dict:
    """Recent checks, newest first, so nothing is lost even if the page forgot which one was open."""
    jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)[:RECENT_JOBS]
    return {"jobs": [j.public() for j in jobs]}


async def _get_job(job_id: str) -> Job:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id) or job_id not in JOBS:
        raise HTTPException(404, f"This check was not found. Checks are kept for {KEEP_JOBS_DAYS} days — "
                                 "please upload the file again.")
    job = JOBS[job_id]
    await _ensure_loaded(job)
    return job


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, since: int | None = None) -> dict:
    return (await _get_job(job_id)).public(since=since)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = await _get_job(job_id)
    if job.status == "running" and job.task and not job.task.done():
        job.stop_requested = True
        job.task.cancel()
        await asyncio.wait([job.task], timeout=15)
    return job.public()


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    """Check again the websites that failed or were never checked (after Stop, an error, ...)."""
    job = await _get_job(job_id)
    if job.status == "running":
        raise HTTPException(409, "This check is still running.")
    again = [u for u in dict.fromkeys(job.urls)
             if u and (u not in job.partial or job.partial[u].status.startswith("error"))]
    if not again:
        raise HTTPException(400, "Every website has been checked already. There is nothing to check again.")
    redo = [u for u in again if u in job.partial]
    try:
        if redo:
            await asyncio.to_thread(job.files.append_reset, redo)
        job.status, job.error, job.finished, job.rows = "running", "", None, None
        await _save(job)
    except OSError:
        log.exception("Could not save job %s", job.id)
        raise HTTPException(507, "Couldn't save on this computer. Is the disk full?")
    results = dict(job.partial)
    for u in redo:
        results.pop(u, None)
    _set_results(job, results)
    _start(job)
    log.info("Job %s: checking %d websites again", job.id, len(again))
    return job.public(since=0)


@app.get("/api/jobs/{job_id}/results")
async def job_results(job_id: str) -> dict:
    job = await _get_job(job_id)
    if job.status == "error":  # stopped by a problem: show what was saved; "Check again" carries on
        return {"rows": [summary(r, n) for r, n in zip(_final_results(job, job.partial, NOT_YET), job.names)]}
    if job.rows is None:
        raise HTTPException(409, "Results are not ready yet.")
    return {"rows": job.rows}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str) -> FileResponse:
    """The job's Excel file: simple (analytics off) or every row and column (analytics on).
    While a check is running (or after an error), a file with everything saved so far."""
    job = await _get_job(job_id)
    stem = f"{Path(job.filename).stem}_{'analytics' if job.analytics else 'contacts'}"
    out = job.files.output
    try:
        if job.status in ("done", "cancelled"):
            if not out.exists():  # e.g. deleted by hand: rebuild it from the saved results
                results = await asyncio.to_thread(job.files.load_results)
                await asyncio.to_thread(_write_output, job, _final_results(job, results, STOPPED), out)
            return FileResponse(out, filename=f"{stem}.xlsx", media_type=XLSX)
        results = await asyncio.to_thread(job.files.load_results)
        tmp = job.dir / f".so-far-{uuid.uuid4().hex}.xlsx"
        await asyncio.to_thread(_write_output, job, _final_results(job, results, NOT_YET), tmp)
    except (OSError, InputError):
        log.exception("Could not build the file for job %s", job.id)
        raise HTTPException(500, "Couldn't create the Excel file. Please try again in a moment.")
    return FileResponse(tmp, filename=f"{stem}_so_far.xlsx", media_type=XLSX,
                        background=BackgroundTask(tmp.unlink, missing_ok=True))
