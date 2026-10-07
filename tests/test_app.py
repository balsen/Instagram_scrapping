import time

import pytest

from app import create_app
from jobs import JobManager
from scraper import ProfileNotFound

RESULT = {"username": "testuser", "followers": 1, "posts": [], "meta": {"complete": True}}


def fake_runner(username):
    if username == "missing":
        raise ProfileNotFound("Instagram profile @missing not found.")
    if username == "crash":
        raise RuntimeError("boom")
    return {**RESULT, "username": username}


@pytest.fixture
def client(tmp_path):
    manager = JobManager(fake_runner, tmp_path, max_workers=1)
    app = create_app(manager)
    app.config["TESTING"] = True
    yield app.test_client()
    manager.shutdown()


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b'name="username"' in response.data


def test_form_submission_redirects_to_job_page(client):
    response = client.post("/jobs", data={"username": "@testuser"})
    assert response.status_code == 303
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert b"@testuser" in page.data

    job = wait_for_job(client, job_id)
    assert job["status"] == "done"
    assert job["result"]["username"] == "testuser"


def test_json_api_and_download(client):
    response = client.post("/jobs", json={"username": "https://instagram.com/testuser/"})
    assert response.status_code == 202
    job = wait_for_job(client, response.get_json()["id"])
    assert list(job["result"]) == ["username", "followers", "posts", "meta"]

    download = client.get(job["download_url"])
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]
    assert download.get_json()["username"] == "testuser"


def test_invalid_username(client):
    form = client.post("/jobs", data={"username": "not valid!"})
    assert form.status_code == 400
    assert b"Invalid Instagram username" in form.data

    api = client.post("/jobs", json={"username": 123})
    assert api.status_code == 400
    assert "error" in api.get_json()


def test_scraper_errors_are_shown(client):
    job_id = client.post("/jobs", json={"username": "missing"}).get_json()["id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "failed"
    assert job["error"] == "Instagram profile @missing not found."


def test_unexpected_errors_are_not_leaked(client):
    job_id = client.post("/jobs", json={"username": "crash"}).get_json()["id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "failed"
    assert "boom" not in job["error"]


def test_duplicate_submissions_share_a_job(tmp_path):
    started = []

    def slow_runner(username):
        started.append(username)
        time.sleep(0.2)
        return RESULT

    manager = JobManager(slow_runner, tmp_path, max_workers=1)
    try:
        first = manager.submit("testuser")
        second = manager.submit("TestUser")
        assert first.id == second.id
    finally:
        manager.shutdown()
    assert started == ["testuser"]


@pytest.mark.parametrize(
    "path", ["/download/../app.py", "/download/..%2Fapp.py", "/download/nope.json"]
)
def test_download_rejects_unknown_and_traversal(client, path):
    assert client.get(path).status_code == 404


def test_unknown_job(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}
