import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scraper import (
    AccessBlocked,
    ExtractionFailed,
    InstagramScraper,
    ProfileNotFound,
    ScraperConfig,
)
from tests.conftest import load_fixture

pytestmark = pytest.mark.integration

OG_TAIL = "See Instagram photos and videos from Test User (@testuser)"
PROFILE_TEMPLATE = """<!DOCTYPE html><html><head>
<title>Test User (@testuser) &bull; Instagram photos and videos</title>
<meta property="og:description" content="12.3K Followers, 321 Following, 5 Posts - {og_tail}">
{scripts}
</head><body style="height: 5000px">
<script>
  window.addEventListener("scroll", () => {{
    if (window.__requested) return;
    window.__requested = true;
    fetch("/api/graphql", {{method: "POST"}});
  }});
</script>
</body></html>"""


def json_script(data):
    body = json.dumps(data).replace("</", "<\\/")
    return f'<script type="application/json">{body}</script>'


class FakeInstagram(BaseHTTPRequestHandler):
    post_pages = load_fixture("polaris_post_pages.json")
    detail_hits: list[str] = []

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="text/html", headers=None):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/testuser/":
            scripts = "\n".join(json_script(b) for b in load_fixture("polaris_profile_page.json"))
            page = PROFILE_TEMPLATE.format(scripts=scripts, og_tail=OG_TAIL)
            return self.send(200, page)
        if path.startswith("/p/"):
            code = path.strip("/").split("/")[1]
            self.detail_hits.append(code)
            page = self.post_pages.get(code)
            if page is None:
                return self.send(404, "not found")
            return self.send(200, f"<html><head>{json_script(page)}</head><body></body></html>")
        if path == "/blocked/":
            return self.send(302, "", headers={"Location": "/accounts/login/?next=/blocked/"})
        if path == "/accounts/login/":
            return self.send(200, "<html><body>Log in</body></html>")
        if path == "/gone/":
            return self.send(200, "<html><body>Sorry, this page isn't available.</body></html>")
        if path == "/removed/":
            title = "<title>Profile isn't available &bull; Instagram</title>"
            return self.send(200, f"<html><head>{title}</head><body>Log in</body></html>")
        if path == "/secret/":
            profile = {
                "require": [
                    {
                        "data": {
                            "xig_user_by_username": {
                                "pk": "4004",
                                "username": "secret",
                                "full_name": "Private Person",
                                "biography": "",
                                "is_private": True,
                                "follower_count": 42,
                                "following_count": 7,
                            }
                        }
                    }
                ]
            }
            return self.send(200, f"<html><head>{json_script(profile)}</head><body></body></html>")
        if path == "/empty/":
            return self.send(200, "<html><head><title>Instagram</title></head><body></body></html>")
        return self.send(404, "<html><body>Not found</body></html>")

    def do_POST(self):
        if self.path.startswith("/api/graphql"):
            body = json.dumps(load_fixture("v1_timeline.json"))
            return self.send(200, body, content_type="application/json; charset=utf-8")
        return self.send(404, "")


@pytest.fixture(scope="module")
def fake_instagram():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeInstagram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def config(fake_instagram):
    return ScraperConfig(
        base_url=fake_instagram,
        scroll_delay=0.2,
        stagnant_rounds=3,
        settle_timeout_ms=1500,
        navigation_timeout_ms=10_000,
        navigation_retries=1,
    )


@pytest.fixture(scope="module", autouse=True)
def require_chromium():
    async def probe():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            await browser.close()

    try:
        asyncio.run(probe())
    except Exception as exc:
        pytest.skip(f"Chromium not available: {exc}")


def test_full_scrape(config):
    FakeInstagram.detail_hits.clear()
    result = asyncio.run(InstagramScraper(config).scrape("@testuser")).to_dict()

    assert result["full_name"] == "Test User"
    assert result["bio"] == "Coffee & code\nBerlin"
    assert result["followers"] == 12345
    assert result["following"] == 321
    assert result["post_count"] == 5

    codes = [post["shortcode"] for post in result["posts"]]
    assert codes == ["EEE555", "FFF666", "GGG777", "CCC333", "DDD444", "ZZZ999"]

    posts = {post["shortcode"]: post for post in result["posts"]}
    assert posts["EEE555"]["likes"] == 77
    assert posts["EEE555"]["timestamp"] == "2025-10-02T10:13:20+00:00"
    assert posts["FFF666"]["video_thumbnail"] == "https://cdn.example/fff666-640.jpg"
    assert posts["GGG777"]["likes"] is None
    assert posts["GGG777"]["video_thumbnail"] == "https://cdn.example/ggg777-child1.jpg"
    assert all(post["timestamp"] and post["comments"] is not None for post in posts.values())

    assert sorted(FakeInstagram.detail_hits) == ["EEE555", "FFF666", "GGG777"]
    assert result["meta"]["complete"] is True
    assert result["meta"]["authenticated"] is False

    stats = result["meta"]["stats"]
    assert stats["detail_pages"] == 3
    assert stats["requests"] >= 5
    assert stats["bytes_received"] > 0
    assert stats["duration_seconds"] > 0
    assert stats["navigation_retries"] == 0


def test_max_posts(config):
    result = asyncio.run(InstagramScraper(replace(config, max_posts=2)).scrape("testuser"))
    assert [p.shortcode for p in result.posts] == ["EEE555", "FFF666"]


def test_private_profile_returns_metadata_without_posts(config):
    result = asyncio.run(InstagramScraper(config).scrape("secret")).to_dict()
    assert result["is_private"] is True
    assert result["full_name"] == "Private Person"
    assert (result["followers"], result["following"]) == (42, 7)
    assert result["bio"] == ""
    assert result["posts"] == []
    assert result["meta"]["complete"] is False


def test_missing_profile(config):
    with pytest.raises(ProfileNotFound):
        asyncio.run(InstagramScraper(config).scrape("nobody"))


@pytest.mark.parametrize("username", ["gone", "removed"])
def test_soft_404(config, username):
    with pytest.raises(ProfileNotFound):
        asyncio.run(InstagramScraper(config).scrape(username))


def test_page_without_data_is_an_error_not_an_empty_result(config):
    with pytest.raises(ExtractionFailed):
        asyncio.run(InstagramScraper(config).scrape("empty"))


def test_login_wall(config):
    with pytest.raises(AccessBlocked):
        asyncio.run(InstagramScraper(config).scrape("blocked"))
