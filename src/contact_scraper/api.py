"""Small web API used by the upload page (web/index.html).

Flow: POST /api/uploads (file -> column preview)  ->  POST /api/jobs (start)
      -> GET /api/jobs/{id}?since=N (progress + live feed)  ->  GET /api/jobs/{id}/results
      -> GET /api/jobs/{id}/download.   POST /api/jobs/{id}/cancel stops early and keeps results.
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
from dataclasses import dataclass, field
from pathlib import Path

import phonenumbers
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from openpyxl import Workbook

from .excel_io import InputError, Sheet, describe_columns, detect_url_column, label_column, load_rows, read_sheet, write_results
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
STOPPED = "skipped: stopped before this website was checked"
NO_URL = "skipped: no valid website address"

app = FastAPI(title="DigiCrawler", docs_url="/api/docs", openapi_url="/api/openapi.json")


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
    status: str = "running"  # running | done | cancelled | error
    done: int = 0
    with_contacts: int = 0
    errors: int = 0
    emails: int = 0
    mobiles: int = 0
    phones: int = 0
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task | None = None
    feed: list[dict] = field(default_factory=list)
    partial: dict[str, SiteResult] = field(default_factory=dict)
    rows: list[dict] | None = None  # final results, in input order

    @property
    def dir(self) -> Path:
        return JOBS_DIR / self.id

    def public(self, since: int | None = None) -> dict:
        data = {
            "id": self.id, "filename": self.filename, "status": self.status, "done": self.done,
            "total": self.total, "with_contacts": self.with_contacts, "errors": self.errors,
            "emails": self.emails, "mobiles": self.mobiles, "phones": self.phones, "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.created, 1),
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


def _cleanup_old_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - KEEP_JOBS_DAYS * 86400
    for d in JOBS_DIR.iterdir():
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
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("input" + Path(name).suffix.lower())
    path.write_bytes(content)
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
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
) -> dict:
    settings = Settings.from_env()
    region = (region or settings.region).upper()
    if region not in phonenumbers.SUPPORTED_REGIONS:
        raise HTTPException(400, "Please choose a valid country.")
    settings.region = region

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
    UPLOADS.pop(upload.id, None)

    label = label_column(sheet.headers, sheet.rows, sheet.url_column)
    names = [str(r[label]).strip() if label is not None and r[label] not in (None, "") else "" for r in sheet.rows]
    name_of = {}
    for u, n in zip(urls, names):
        name_of.setdefault(u, n)

    job = Job(id=upload.id, filename=upload.filename, total=len(set(u for u in urls if u)))
    JOBS[job.id] = job

    def progress(done: int, _total: int, res: SiteResult) -> None:
        job.done = done
        job.partial[res.url] = res
        job.with_contacts += res.status == "ok"
        job.errors += res.status.startswith("error")
        job.emails += len(res.emails)
        job.mobiles += len(res.mobiles)
        job.phones += len(res.phones)
        job.feed.append({"i": len(job.feed), **summary(res, name_of.get(res.url, ""))})

    async def process() -> None:
        try:
            try:
                results = await run(urls, settings, progress)
                status = "done"
            except asyncio.CancelledError:
                results = [job.partial.get(u) or SiteResult(u, status=STOPPED if u else NO_URL) for u in urls]
                status = "cancelled"
            await run_in_threadpool(write_results, job.dir / "output.xlsx", sheet, results)
            job.rows = [summary(r, n) for r, n in zip(results, names)]
            job.status = status
            log.info("Job %s %s: %d/%d sites, %d with contacts", job.id, status, job.done, job.total, job.with_contacts)
        except Exception:
            log.exception("Job %s failed", job.id)
            job.status = "error"
            job.error = "Something went wrong while processing. Please try again, or contact the developer."
        finally:
            job.finished = time.time()

    job.task = asyncio.create_task(process())
    log.info("Job %s started: %s (%d websites, region %s)", job.id, job.filename, job.total, region)
    return job.public(since=0)


def _get_job(job_id: str) -> Job:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id) or job_id not in JOBS:
        raise HTTPException(404, "This job was not found. The app may have been restarted — please upload the file again.")
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, since: int | None = None) -> dict:
    return _get_job(job_id).public(since=since)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = _get_job(job_id)
    if job.status == "running" and job.task and not job.task.done():
        job.task.cancel()
        await asyncio.wait([job.task], timeout=15)
    return job.public()


@app.get("/api/jobs/{job_id}/results")
async def job_results(job_id: str) -> dict:
    job = _get_job(job_id)
    if job.rows is None:
        raise HTTPException(409, "Results are not ready yet.")
    return {"rows": job.rows}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    out = job.dir / "output.xlsx"
    if job.status not in ("done", "cancelled") or not out.exists():
        raise HTTPException(409, "Results are not ready yet.")
    return FileResponse(
        out,
        filename=f"{Path(job.filename).stem}_contacts.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
