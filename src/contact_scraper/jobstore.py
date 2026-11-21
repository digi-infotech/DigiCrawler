"""Keeps each job on disk, so nothing is lost if the page is refreshed, the app restarts or the
computer switches off in the middle of a check. The disk is the source of truth; the API only keeps
a copy in memory so the page gets quick answers.

One folder per job, ``data/jobs/<id>/``:

- ``input.xlsx``    the uploaded file, untouched
- ``upload.json``   {"filename": ...}, written when the file is uploaded (lets a preview survive a restart)
- ``plan.json``     the website and name of every input row, written once when the job starts
- ``job.json``      settings, status and counters; replaced atomically on every change
- ``results.jsonl`` one line per finished website, appended and forced to disk before the page
                    hears about it. A ``{"reset": [...]}`` line forgets earlier results for those
                    websites (used by "Check again"); later lines win.
- ``output.xlsx``   the finished Excel file, replaced atomically

Writes are crash safe: whole files are written to a temporary file and renamed into place, and
``results.jsonl`` is only ever appended to. A crash can cut only its last line short; that line is
dropped (and the file repaired) the next time it is read, and that one website is simply checked again.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from pathlib import Path

from .models import SiteResult

log = logging.getLogger(__name__)

_FIELDS = {f.name for f in dataclasses.fields(SiteResult)}


def write_atomic(path: Path, data: bytes) -> None:
    """Replace `path` with `data` so that a reader (or a crash) sees the old file or the new one, never half."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: Path, data: object) -> None:
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def read_json(path: Path) -> dict | None:
    """The file's JSON, or None when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("Could not read %s", path)
        return None


def result_to_dict(res: SiteResult) -> dict:
    return dataclasses.asdict(res)


def result_from_dict(data: dict) -> SiteResult:
    return SiteResult(**{k: v for k, v in data.items() if k in _FIELDS})


class JobFiles:
    """The files of one job. Safe to call from several threads at once."""

    def __init__(self, folder: Path):
        self.dir = folder
        self._lock = threading.Lock()

    meta = property(lambda self: self.dir / "job.json")
    plan = property(lambda self: self.dir / "plan.json")
    upload = property(lambda self: self.dir / "upload.json")
    results = property(lambda self: self.dir / "results.jsonl")
    output = property(lambda self: self.dir / "output.xlsx")

    def _append(self, record: dict) -> None:
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock, open(self.results, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def append_result(self, res: SiteResult) -> None:
        self._append(result_to_dict(res))

    def append_reset(self, urls: list[str]) -> None:
        self._append({"reset": urls})

    def load_results(self) -> dict[str, SiteResult]:
        """Every saved result by website, in the order they were saved."""
        with self._lock:
            try:
                raw = self.results.read_bytes()
            except FileNotFoundError:
                return {}
            if raw and not raw.endswith(b"\n"):
                # The app stopped while writing the last line: drop it so the next append starts clean.
                cut = raw.rfind(b"\n") + 1
                log.warning("Repairing %s: dropping an unfinished last line", self.results)
                with open(self.results, "r+b") as f:
                    f.truncate(cut)
                    f.flush()
                    os.fsync(f.fileno())
                raw = raw[:cut]
        out: dict[str, SiteResult] = {}
        for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if "reset" in record:
                    for url in record["reset"]:
                        out.pop(url, None)
                    continue
                res = result_from_dict(record)
            except (ValueError, TypeError):
                log.warning("Skipping unreadable line %d in %s", n, self.results)
                continue
            out.pop(res.url, None)  # keep "order saved" accurate when a website was checked again
            out[res.url] = res
        return out
