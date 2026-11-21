"""Nothing is lost when the page reloads, the internet drops or the app/computer restarts mid-check."""
import asyncio
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from contact_scraper import api, jobstore, netguard, runner
from contact_scraper.models import Settings, SiteResult
from tests.test_api import xlsx_bytes

FOUR = xlsx_bytes([["Name", "Website"], ["A", "one.in"], ["B", "two.in"], ["C", "three.in"], ["D", "four.in"]])


def good(u: str) -> SiteResult:
    return SiteResult(u, u, emails=[f"info@{u.split('//')[1]}"], status="ok")


# ------------------------------------------------------------------ jobstore

def test_results_survive_a_cut_off_last_line(tmp_path):
    files = jobstore.JobFiles(tmp_path)
    files.append_result(good("https://one.in"))
    files.append_result(SiteResult("https://two.in", status="error: could not connect to website"))
    with open(files.results, "ab") as f:
        f.write(b'{"url": "https://three.in", "emai')  # the computer switched off mid-write
    loaded = files.load_results()
    assert list(loaded) == ["https://one.in", "https://two.in"]
    assert loaded["https://one.in"].emails == ["info@one.in"]
    files.append_result(good("https://three.in"))  # the repaired file takes new lines cleanly
    assert list(files.load_results()) == ["https://one.in", "https://two.in", "https://three.in"]


def test_reset_lines_forget_results_and_later_lines_win(tmp_path):
    files = jobstore.JobFiles(tmp_path)
    files.append_result(SiteResult("https://one.in", status="error: x"))
    files.append_result(good("https://two.in"))
    files.append_reset(["https://one.in"])
    assert list(files.load_results()) == ["https://two.in"]
    files.append_result(good("https://one.in"))
    assert files.load_results()["https://one.in"].status == "ok"


def test_atomic_json_never_leaves_temp_files(tmp_path):
    jobstore.write_json(tmp_path / "job.json", {"a": "₹"})
    jobstore.write_json(tmp_path / "job.json", {"a": "b"})
    assert jobstore.read_json(tmp_path / "job.json") == {"a": "b"}
    assert [p.name for p in tmp_path.iterdir()] == ["job.json"]
    assert jobstore.read_json(tmp_path / "missing.json") is None


# ------------------------------------------------------------------ internet check

class Net:
    """A switchable internet connection for the guard's check."""

    def __init__(self):
        self.up = True
        self.checks = 0

    async def check(self) -> bool:
        self.checks += 1
        return self.up


@pytest.fixture
def net(monkeypatch):
    n = Net()
    real = netguard.NetworkGuard
    monkeypatch.setattr(runner, "NetworkGuard",
                        lambda on_change=None: real(n.check, on_change, batch_delay=0, offline_interval=0.02))
    return n


async def test_sites_checked_while_offline_are_checked_again(net, monkeypatch):
    calls: list[str] = []

    async def fake_crawl(url, fetcher, settings):
        calls.append(url)
        if url.endswith("down.in") and calls.count(url) == 1:
            net.up = False  # the connection drops while this site is being checked
            asyncio.get_running_loop().call_later(0.1, setattr, net, "up", True)
            return SiteResult(url, status="error: could not connect to website")
        return good(url)

    monkeypatch.setattr(runner, "crawl_site", fake_crawl)
    changes: list[bool] = []
    results = await runner.run(["https://down.in", "https://fine.in"], Settings(online_check=True, concurrency=1),
                               on_network=changes.append)
    assert [r.status for r in results] == ["ok", "ok"]
    assert calls.count("https://down.in") == 2
    assert changes == [False, True]


async def test_result_needs_a_check_that_started_after_it_finished():
    n = Net()
    async with netguard.NetworkGuard(n.check, batch_delay=0, offline_interval=0.01) as guard:
        assert await guard.confirm() is True
        n.up = False
        assert await guard.confirm() is False
        assert not guard.online
        n.up = True
        await asyncio.wait_for(guard.wait_online(), 1)
        assert await guard.confirm() is True


async def test_internet_check_handles_unreachable_hosts():
    assert await netguard.internet_ok([("127.0.0.1", 1)], timeout=1) is False


async def test_address_not_found_gets_a_second_try(monkeypatch):
    seen: list[str] = []

    async def flaky_dns(url, fetcher, settings):
        seen.append(url)
        return SiteResult(url, status=runner.DOMAIN_NOT_FOUND) if len(seen) == 1 else good(url)

    monkeypatch.setattr(runner, "crawl_site", flaky_dns)
    assert (await runner.run(["https://hiccup.in"], Settings()))[0].status == "ok"
    assert len(seen) == 2


# ------------------------------------------------------------------ app restarts

@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "JOBS_DIR", tmp_path / "jobs")
    api.JOBS.clear()
    api.UPLOADS.clear()
    yield tmp_path / "jobs"
    api.JOBS.clear()
    api.UPLOADS.clear()


def restart():
    """What a reboot does to the app: memory is gone, only the disk is left."""
    api.JOBS.clear()
    api.UPLOADS.clear()


def wait_for(client, job_id, until, tries=200):
    for _ in range(tries):
        job = client.get(f"/api/jobs/{job_id}").json()
        if until(job):
            return job
        time.sleep(0.02)
    raise AssertionError(f"timed out: {job}")


def fast_run(log: list):
    async def run(urls, settings, on_progress=None, on_network=None):
        log.append(list(dict.fromkeys(u for u in urls if u)))
        unique = log[-1]
        for i, u in enumerate(unique, 1):
            await on_progress(i, len(unique), good(u))
        return [good(u) for u in urls]
    return run


def run_that_hangs_after(n: int):
    async def run(urls, settings, on_progress=None, on_network=None):
        unique = list(dict.fromkeys(u for u in urls if u))
        for i, u in enumerate(unique[:n], 1):
            await on_progress(i, len(unique), good(u))
        await asyncio.Event().wait()  # still working on the rest
    return run


def excel_rows(content: bytes) -> list[tuple]:
    return list(load_workbook(io.BytesIO(content)).active.iter_rows(values_only=True))


def test_a_restart_mid_check_carries_on_without_redoing_finished_sites(jobs_dir, monkeypatch):
    monkeypatch.setattr(api, "run", run_that_hangs_after(2))
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}, data={"analytics": "true"}).json()["id"]
        wait_for(c, job_id, lambda j: j["done"] == 2)
    # The app stopped (not "Stop"): the job is still marked running on disk, with 2 results saved.
    assert json.loads((jobs_dir / job_id / "job.json").read_text(encoding="utf-8"))["status"] == "running"
    with open(jobs_dir / job_id / "results.jsonl", "ab") as f:
        f.write(b'{"url": "https://thr')  # and it was switched off in the middle of saving a third

    restart()
    seen: list = []
    monkeypatch.setattr(api, "run", fast_run(seen))
    with TestClient(api.app) as c:
        job = wait_for(c, job_id, lambda j: j["status"] != "running")
        assert job["status"] == "done" and job["done"] == 4 and job["with_contacts"] == 4
        assert seen == [["https://three.in", "https://four.in"]]  # only what wasn't saved yet
        rows = excel_rows(c.get(f"/api/jobs/{job_id}/download").content)
        assert [r[-1] for r in rows[1:]] == ["ok"] * 4
        assert c.get(f"/api/jobs/{job_id}?since=0").json()["feed_total"] == 4


def test_finished_checks_and_uploads_are_still_there_after_a_restart(jobs_dir, monkeypatch):
    monkeypatch.setattr(api, "run", fast_run([]))
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}).json()["id"]
        wait_for(c, job_id, lambda j: j["status"] == "done")
        upload_id = c.post("/api/uploads", files={"file": ("next.xlsx", FOUR)}).json()["upload_id"]

    restart()
    with TestClient(api.app) as c:
        listed = c.get("/api/jobs").json()["jobs"]
        assert [(j["id"], j["status"], j["done"]) for j in listed] == [(job_id, "done", 4)]
        rows = c.get(f"/api/jobs/{job_id}/results").json()["rows"]
        assert [r["name"] for r in rows] == ["A", "B", "C", "D"]
        assert c.get(f"/api/jobs/{job_id}/download").status_code == 200
        res = c.post("/api/jobs", data={"upload_id": upload_id})  # the previewed file can still be started
        assert res.status_code == 200, res.text


def test_stop_is_kept_as_stopped_after_a_restart(jobs_dir, monkeypatch):
    monkeypatch.setattr(api, "run", run_that_hangs_after(1))
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}, data={"analytics": "true"}).json()["id"]
        wait_for(c, job_id, lambda j: j["done"] == 1)
        assert c.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    restart()
    with TestClient(api.app) as c:
        job = c.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "cancelled" and job["done"] == 1 and job["to_retry"] == 3
        statuses = [r["status"] for r in c.get(f"/api/jobs/{job_id}/results").json()["rows"]]
        assert statuses == ["ok", api.STOPPED, api.STOPPED, api.STOPPED]


# ------------------------------------------------------------------ check again, results so far, offline, disk

def test_check_again_redoes_only_failed_and_unchecked_sites(jobs_dir, monkeypatch):
    async def half_bad(urls, settings, on_progress=None, on_network=None):
        unique = list(dict.fromkeys(u for u in urls if u))
        for i, u in enumerate(unique, 1):
            await on_progress(i, len(unique), good(u) if "o" in u.split("//")[1][:2] else
                              SiteResult(u, status="error: could not connect to website"))
        return []

    monkeypatch.setattr(api, "run", half_bad)  # one.in ok, two.in error, three.in error, four.in ok
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}).json()["id"]
        job = wait_for(c, job_id, lambda j: j["status"] == "done")
        assert (job["errors"], job["to_retry"]) == (2, 2)
        seen: list = []
        monkeypatch.setattr(api, "run", fast_run(seen))
        again = c.post(f"/api/jobs/{job_id}/retry")
        assert again.status_code == 200 and again.json()["done"] == 2
        job = wait_for(c, job_id, lambda j: j["status"] == "done")
        assert seen == [["https://two.in", "https://three.in"]]
        assert (job["done"], job["errors"], job["with_contacts"], job["to_retry"]) == (4, 0, 4, 0)
        assert c.post(f"/api/jobs/{job_id}/retry").status_code == 400  # nothing left to redo
    restart()  # the redone results, not the old errors, are what the disk remembers
    with TestClient(api.app) as c:
        assert c.get(f"/api/jobs/{job_id}").json()["errors"] == 0


def test_download_while_running_gives_results_so_far(jobs_dir, monkeypatch):
    monkeypatch.setattr(api, "run", run_that_hangs_after(1))
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}, data={"analytics": "true"}).json()["id"]
        wait_for(c, job_id, lambda j: j["done"] == 1)
        dl = c.get(f"/api/jobs/{job_id}/download")
        assert dl.status_code == 200 and 'filename="list_analytics_so_far.xlsx"' in dl.headers["content-disposition"]
        assert [r[-1] for r in excel_rows(dl.content)[1:]] == ["ok"] + [api.NOT_YET] * 3
        assert c.post(f"/api/jobs/{job_id}/retry").status_code == 409  # still running
    assert not list((jobs_dir / job_id).glob(".so-far-*"))  # temporary file cleaned up


def test_page_is_told_when_the_internet_is_down(jobs_dir, monkeypatch):
    async def offline_run(urls, settings, on_progress=None, on_network=None):
        assert settings.region == "IN"
        on_network(False)
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "run", offline_run)
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}, data={"region": "IN"}).json()["id"]
        assert wait_for(c, job_id, lambda j: j["offline"])["status"] == "running"


def test_a_full_disk_stops_the_job_with_a_clear_message_and_check_again_carries_on(jobs_dir, monkeypatch):
    real_append = jobstore.JobFiles.append_result
    state = {"full": False}

    def append(self, res):
        if state["full"]:
            raise OSError(28, "No space left on device")
        real_append(self, res)
        state["full"] = True  # the disk fills up after the first result

    monkeypatch.setattr(jobstore.JobFiles, "append_result", append)
    seen: list = []
    monkeypatch.setattr(api, "run", fast_run(seen))
    with TestClient(api.app) as c:
        job_id = c.post("/api/jobs", files={"file": ("list.xlsx", FOUR)}).json()["id"]
        job = wait_for(c, job_id, lambda j: j["status"] != "running")
        assert job["status"] == "error" and "Is the disk full?" in job["error"] and job["done"] == 1
        assert c.get(f"/api/jobs/{job_id}/download").status_code == 200  # what was saved can still be downloaded
        rows = c.get(f"/api/jobs/{job_id}/results").json()["rows"]
        assert [r["status"] for r in rows] == ["ok"] + [api.NOT_YET] * 3
        state["full"] = False
        monkeypatch.setattr(jobstore.JobFiles, "append_result", real_append)
        assert c.post(f"/api/jobs/{job_id}/retry").status_code == 200
        assert wait_for(c, job_id, lambda j: j["status"] != "running")["done"] == 4
        assert seen[-1] == ["https://two.in", "https://three.in", "https://four.in"]


def test_a_damaged_job_folder_does_not_stop_the_app(jobs_dir, monkeypatch):
    bad = jobs_dir / "badbadbadbad"
    bad.mkdir(parents=True)
    (bad / "job.json").write_text('{"id": "badbadbadbad", "status": "running", "filename": "x.xlsx", "total": 1}',
                                  encoding="utf-8")  # but no plan.json
    (jobs_dir / "abcabcabcabc").mkdir()
    (jobs_dir / "abcabcabcabc" / "job.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(api, "run", fast_run([]))
    with TestClient(api.app) as c:
        assert c.get("/api/health").json() == {"ok": True}
        assert c.get("/api/jobs").json()["jobs"] == []
