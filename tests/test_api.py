"""The web API used by the upload page: upload -> progress -> download, plus bad files."""
import io
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from contact_scraper import api
from contact_scraper.models import SiteResult


def xlsx_bytes(rows) -> bytes:
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SETTINGS_SEEN: list = []
RUN_URLS: list = []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "JOBS_DIR", tmp_path / "jobs")
    api.JOBS.clear()
    api.UPLOADS.clear()
    RUN_URLS.clear()

    async def fake_run(urls, settings, on_progress=None, on_network=None):  # no internet in tests
        SETTINGS_SEEN.append(settings)
        RUN_URLS.append(list(urls))
        unique = list(dict.fromkeys(u for u in urls if u))
        results = {}
        for i, u in enumerate(unique, 1):
            results[u] = (SiteResult(u, u, emails=[f"info@{u.split('//')[1]}"], mobiles=["+91 98450 43210"],
                                     phones=["+91 98450 43210", "+91 22 2345 6789"], status="ok")
                          if "good" in u else SiteResult(u, status="error: website address not found"))
            if on_progress:
                await on_progress(i, len(unique), results[u])
        return [results.get(u) or SiteResult("", status="skipped: no valid website address") for u in urls]

    monkeypatch.setattr(api, "run", fake_run)
    with TestClient(api.app) as c:
        yield c


def upload(client, content: bytes, name="list.xlsx", region="IN", analytics=True):
    return client.post("/api/jobs", files={"file": (name, content)},
                       data={"region": region, "analytics": str(analytics).lower()})


def wait_done(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_full_flow(client):
    content = xlsx_bytes([["Name", "Website"], ["A", "good-a.in"], ["B", "bad-b.in"], ["C", None], ["D", "good-a.in"]])
    res = upload(client, content)
    assert res.status_code == 200, res.text
    job = wait_done(client, res.json()["id"])
    assert job == {**job, "status": "done", "done": 2, "total": 2, "with_contacts": 1, "errors": 1}

    dl = client.get(f"/api/jobs/{job['id']}/download")
    assert dl.status_code == 200
    assert 'filename="list_analytics.xlsx"' in dl.headers["content-disposition"]
    rows = list(load_workbook(io.BytesIO(dl.content)).active.iter_rows(values_only=True))
    assert rows[0][2:] == ("business_name", "website_final", "address", "city", "state", "pin_code", "country",
                           "email_1", "mobile_1", "phone_1", "source_pages", "status")
    assert rows[1][9:12] == ("info@good-a.in", "+91 98450 43210", "+91 22 2345 6789")  # one value per cell
    assert rows[2][-1] == "error: website address not found"
    assert rows[3][-1] == "skipped: no valid website address"
    assert rows[4][9] == "info@good-a.in"



def test_simple_mode_job_gives_the_simple_file(client):
    """Analytics mode off: only rows with an email or mobile, essential columns only."""
    content = xlsx_bytes([["Name", "Website"], ["A", "good-a.in"], ["B", "bad-b.in"], ["C", None], ["D", "good-a.in"]])
    job = wait_done(client, upload(client, content, analytics=False).json()["id"])
    assert job["analytics"] is False
    dl = client.get(f"/api/jobs/{job['id']}/download")
    assert 'filename="list_contacts.xlsx"' in dl.headers["content-disposition"]
    rows = list(load_workbook(io.BytesIO(dl.content)).active.iter_rows(values_only=True))
    assert rows == [("Name", "Website", "business_name", "address", "city", "state", "pin_code", "country",
                     "email_1", "mobile_1"),
                    ("A", "good-a.in", None, None, None, None, None, None, "info@good-a.in", "+91 98450 43210"),
                    ("D", "good-a.in", None, None, None, None, None, None, "info@good-a.in", "+91 98450 43210")]


def test_config(client):
    assert client.get("/api/config").json()["max_rows"] == api.MAX_ROWS


@pytest.mark.parametrize("name,content,message", [
    ("list.csv", b"a,b", "Please upload an Excel file"),
    ("list.xlsx", b"not really excel", "Couldn't open this file"),
    ("old.xls", b"junk", "Save As"),
    ("list.xlsx", xlsx_bytes([["Name", "City"], ["Acme", "Pune"]]), "Couldn't find a column with website addresses"),
    ("list.xlsx", xlsx_bytes([["Website"]]), "looks empty"),
    ("list.xlsx", xlsx_bytes([["Website"], ["not a url"], ["also bad"]]), "No website addresses found"),
])
def test_bad_uploads_get_plain_english_errors(client, name, content, message):
    res = upload(client, content, name=name)
    assert res.status_code == 400
    assert message in res.json()["detail"]


def test_too_many_rows(client, monkeypatch):
    monkeypatch.setattr(api, "MAX_ROWS", 3)
    res = upload(client, xlsx_bytes([["Website"]] + [[f"s{i}.in"] for i in range(5)]))
    assert res.status_code == 400 and "limit is 3" in res.json()["detail"]


def test_bad_region(client):
    res = upload(client, xlsx_bytes([["Website"], ["good.in"]]), region="XX")
    assert res.status_code == 400


def test_unknown_job_and_early_download(client):
    assert client.get("/api/jobs/000000000000").status_code == 404
    assert client.get("/api/jobs/../../etc").status_code == 404
    api.JOBS["abcabcabcabc"] = api.Job(id="abcabcabcabc", filename="x.xlsx", total=1)
    assert client.get("/api/jobs/abcabcabcabc/results").status_code == 409


# ------------------------------------------------------------------ upload preview, live feed, results, cancel

def test_upload_preview_then_start_with_chosen_column(client):
    content = xlsx_bytes([["Company", "Main site", "Old site"],
                          ["Acme", "good-a.in", "good-old-a.in"], ["Beta", "good-b.in", None], ["Gamma", None, "bad.in"]])
    prev = client.post("/api/uploads", files={"file": ("list.xlsx", content)})
    assert prev.status_code == 200, prev.text
    info = prev.json()
    assert info["rows"] == 3 and info["filename"] == "list.xlsx"
    assert [c["valid"] for c in info["columns"]] == [0, 2, 2]
    assert info["columns"][1]["samples"] == ["https://good-a.in", "https://good-b.in"]
    assert info["detected"] in (1, 2)

    res = client.post("/api/jobs", data={"upload_id": info["upload_id"], "url_column": "Old site", "region": "IN"})
    assert res.status_code == 200, res.text
    job = wait_done(client, res.json()["id"])
    assert job["total"] == 2

    status = client.get(f"/api/jobs/{job['id']}?since=0").json()
    assert status["feed_total"] == 2
    assert {f["url"] for f in status["feed"]} == {"https://good-old-a.in", "https://bad.in"}
    assert {f["name"] for f in status["feed"]} == {"Acme", "Gamma"}          # label column shown with each site
    assert client.get(f"/api/jobs/{job['id']}?since=2").json()["feed"] == []

    rows = client.get(f"/api/jobs/{job['id']}/results").json()["rows"]
    assert [r["name"] for r in rows] == ["Acme", "Beta", "Gamma"]            # input order
    assert rows[0]["mobiles"] == ["+91 98450 43210"] and rows[0]["phones"] == ["+91 22 2345 6789"]  # phones = non-mobile
    assert rows[1]["status"] == "skipped: no valid website address"
    assert job["emails"] == 1 and job["mobiles"] == 1 and job["phones"] == 2


def test_upload_preview_rejects_file_without_websites(client):
    res = client.post("/api/uploads", files={"file": ("x.xlsx", xlsx_bytes([["Name", "City"], ["A", "Pune"]]))})
    assert res.status_code == 400 and "Couldn't find any website addresses" in res.json()["detail"]


def test_unknown_upload_id(client):
    res = client.post("/api/jobs", data={"upload_id": "ffffffffffff", "region": "IN"})
    assert res.status_code == 404


def test_cancel_keeps_finished_results(client, monkeypatch):
    import asyncio

    async def slow_run(urls, settings, on_progress=None, on_network=None):
        unique = list(dict.fromkeys(u for u in urls if u))
        for i, u in enumerate(unique, 1):
            await asyncio.sleep(0.05 if i == 1 else 30)
            await on_progress(i, len(unique), SiteResult(u, u, emails=["a@x.in"], status="ok"))
        return []

    monkeypatch.setattr(api, "run", slow_run)
    res = upload(client, xlsx_bytes([["Website"], ["one.in"], ["two.in"], ["three.in"]]))
    job_id = res.json()["id"]
    for _ in range(50):
        if client.get(f"/api/jobs/{job_id}").json()["done"] == 1:
            break
        time.sleep(0.05)
    job = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert job["status"] == "cancelled" and job["done"] == 1
    rows = client.get(f"/api/jobs/{job_id}/results").json()["rows"]
    assert [r["status"] for r in rows] == ["ok", api.STOPPED, api.STOPPED]
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 200   # partial results downloadable


def test_sample_file(client):
    res = client.get("/api/sample")
    assert res.status_code == 200
    rows = list(load_workbook(io.BytesIO(res.content)).active.iter_rows(values_only=True))
    assert rows[0] == ("Company", "Website", "City") and len(rows) == 4
