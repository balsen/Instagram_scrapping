from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser import BrowserManager
from .collector import ResultCollector
from .config import ScraperConfig
from .errors import (
    AccessBlocked,
    ExtractionFailed,
    InvalidUsername,
    NavigationFailed,
    ProfileNotFound,
    RateLimited,
    ScraperError,
)
from .models import Post, Profile, ScrapeResult, ScrapeStats
from .parser import loads_lenient

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
COUNT_PATTERN = r"([\d.,]+\s?[KMB]?)"
API_PATH_MARKERS = ("/graphql", "/api/v1/")
BLOCKED_PATH_MARKERS = ("/accounts/login", "/challenge", "/accounts/suspended")
NOT_FOUND_MARKERS = ("Sorry, this page isn't available", "Profile isn't available")
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
EMBEDDED_JSON_JS = """
() => Array.from(
    document.querySelectorAll('script[type="application/json"]'),
    (script) => script.textContent || ""
)
"""


def clean_username(raw: str) -> str:
    value = raw.strip() if isinstance(raw, str) else ""
    if "instagram.com/" in value:
        path = urlparse(value if "://" in value else f"https://{value}").path
        value = path.strip("/").split("/", 1)[0]
    value = value.lstrip("@")

    if not USERNAME_RE.fullmatch(value):
        raise InvalidUsername(
            "Invalid Instagram username: use 1-30 letters, numbers, periods or underscores."
        )
    return value


def parse_count(value: str) -> int | None:
    value = value.strip().upper().replace(",", "").replace(" ", "")
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(value[-1:], 1)
    if multiplier != 1:
        value = value[:-1]
    try:
        return round(float(value) * multiplier)
    except ValueError:
        return None


def parse_og_description(text: str) -> Profile:
    profile = Profile(username="")
    for field_name, label in (
        ("followers", "Followers"),
        ("following", "Following"),
        ("post_count", "Posts"),
    ):
        match = re.search(rf"{COUNT_PATTERN}\s+{label}\b", text, re.IGNORECASE)
        if match:
            setattr(profile, field_name, parse_count(match.group(1)))
    return profile


def _is_api_response(response: Response) -> bool:
    if not response.ok or response.request.resource_type not in ("xhr", "fetch"):
        return False
    if not any(marker in urlparse(response.url).path for marker in API_PATH_MARKERS):
        return False
    content_type = response.headers.get("content-type", "")
    return "json" in content_type or "javascript" in content_type


def _needs_details(post: Post) -> bool:
    return bool(post.shortcode) and (post.timestamp is None or post.comments is None)


async def _wait_for(tasks: set[asyncio.Task], timeout: float) -> None:
    if tasks:
        await asyncio.wait(set(tasks), timeout=timeout)


class InstagramScraper:
    def __init__(self, config: ScraperConfig | None = None) -> None:
        self.config = config or ScraperConfig()

    async def scrape(self, username: str) -> ScrapeResult:
        username = clean_username(username)
        started = time.perf_counter()
        stats = ScrapeStats()
        collector = ResultCollector(username)
        pending: set[asyncio.Task] = set()
        logger.info("Scraping @%s", username)

        async with BrowserManager(self.config) as browser:
            page = await browser.new_page()

            def on_response(response: Response) -> None:
                if _is_api_response(response):
                    task = asyncio.create_task(self._ingest_response(response, collector))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

            page.on("response", on_response)

            await self._open_profile(page, username, stats)
            await self._ingest_embedded_json(page, collector, needle=username)
            fallback = await self._read_page_metadata(page, username)
            await self._paginate(page, collector, pending)
            await _wait_for(pending, timeout=5)
            await page.close()

            if self.config.enrich_posts:
                await self._enrich_posts(browser, collector, stats)

        profile = collector.profile
        profile.fill_missing(fallback)
        posts = collector.posts()
        if not posts and profile.user_id is None and profile.followers is None:
            raise ExtractionFailed(
                f"No profile data found for @{username}. Instagram may have blocked the "
                "request or changed its page format."
            )
        complete = collector.end_of_feed or (
            profile.post_count is not None and len(posts) >= profile.post_count
        )
        capped = bool(self.config.max_posts) and len(posts) >= self.config.max_posts
        if self.config.max_posts:
            posts = posts[: self.config.max_posts]

        if not posts and profile.is_private:
            logger.info("@%s is private; only profile metadata is available", username)
        elif not complete and not capped:
            hint = (
                "Instagram stopped serving more posts to this session."
                if browser.authenticated
                else "Instagram limits logged-out browsing; set IG_STORAGE_STATE "
                "for a full timeline."
            )
            logger.warning(
                "@%s: collected %d of %s posts. %s",
                username,
                len(posts),
                profile.post_count if profile.post_count is not None else "unknown",
                hint,
            )
        logger.info(
            "Scraped @%s: %d posts from %d payloads (complete=%s)",
            username,
            len(posts),
            collector.payloads,
            complete,
        )

        network = browser.network
        stats.duration_seconds = round(time.perf_counter() - started, 2)
        stats.requests = network.requests
        stats.failed_requests = network.failed
        stats.blocked_requests = network.blocked
        stats.bytes_received = network.bytes_received
        stats.payloads_parsed = collector.payloads

        return ScrapeResult(
            profile=profile,
            posts=posts,
            scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            complete=complete,
            authenticated=browser.authenticated,
            stats=stats,
        )

    async def _open_profile(self, page: Page, username: str, stats: ScrapeStats) -> None:
        config = self.config
        url = f"{config.base_url}/{username}/"

        for attempt in range(1, config.navigation_retries + 1):
            failure: ScraperError
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=config.navigation_timeout_ms
                )
            except PlaywrightError as exc:
                failure = NavigationFailed(f"Could not load {url}: {exc.message}")
            else:
                status = response.status if response else 0
                if status == 404:
                    raise ProfileNotFound(f"Instagram profile @{username} not found.")
                if status in (401, 403):
                    raise AccessBlocked(f"Instagram refused access (HTTP {status}).")
                if status not in RETRYABLE_STATUSES:
                    break
                failure = (
                    RateLimited("Instagram is rate limiting requests; try again later.")
                    if status == 429
                    else NavigationFailed(f"Instagram returned HTTP {status}.")
                )

            if attempt == config.navigation_retries:
                raise failure
            delay = config.retry_backoff * 2 ** (attempt - 1)
            stats.navigation_retries += 1
            logger.warning(
                "Loading %s failed (%s); retrying in %.1fs (%d/%d)",
                url,
                failure,
                delay,
                attempt,
                config.navigation_retries - 1,
            )
            await asyncio.sleep(delay)

        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=config.settle_timeout_ms)

        await self._check_access(page, username)

    async def _check_access(self, page: Page, username: str) -> None:
        path = urlparse(page.url).path
        if any(marker in path for marker in BLOCKED_PATH_MARKERS):
            raise AccessBlocked(
                "Instagram redirected to a login or challenge page. "
                "Provide a logged-in session with IG_STORAGE_STATE."
            )
        title = await page.title()
        for marker in NOT_FOUND_MARKERS:
            if marker in title or await page.get_by_text(marker).count():
                raise ProfileNotFound(f"Instagram profile @{username} not found.")

    async def _ingest_response(self, response: Response, collector: ResultCollector) -> None:
        try:
            body = await response.text()
        except PlaywrightError as exc:
            logger.debug("Could not read body of %s: %s", response.url, exc.message)
            return
        try:
            data = loads_lenient(body)
        except ValueError:
            logger.debug("Skipping non-JSON response from %s", response.url)
            return
        try:
            new = collector.feed(data)
        except Exception:
            logger.exception("Failed to parse payload from %s", response.url)
            return
        if new:
            logger.debug("+%d posts from %s", new, urlparse(response.url).path)

    async def _ingest_embedded_json(
        self, page: Page, collector: ResultCollector, needle: str, allow_new: bool = True
    ) -> None:
        needle = needle.lower()
        for text in await page.evaluate(EMBEDDED_JSON_JS):
            if needle not in text.lower():
                continue
            try:
                collector.feed(json.loads(text), allow_new=allow_new)
            except ValueError:
                continue
            except Exception:
                logger.exception("Failed to parse embedded JSON on %s", page.url)

    async def _enrich_posts(
        self, browser: BrowserManager, collector: ResultCollector, stats: ScrapeStats
    ) -> None:
        config = self.config
        targets = [post.shortcode for post in collector.posts() if _needs_details(post)]
        if config.max_posts:
            targets = targets[: config.max_posts]
        if not targets:
            return

        logger.info("Fetching details for %d posts", len(targets))
        semaphore = asyncio.Semaphore(config.detail_concurrency)
        blocked = asyncio.Event()

        async def enrich(shortcode: str) -> None:
            async with semaphore:
                if blocked.is_set():
                    return
                page = await browser.new_page()
                stats.detail_pages += 1
                try:
                    await page.goto(
                        f"{config.base_url}/p/{shortcode}/",
                        wait_until="domcontentloaded",
                        timeout=config.navigation_timeout_ms,
                    )
                    if any(m in urlparse(page.url).path for m in BLOCKED_PATH_MARKERS):
                        blocked.set()
                        return
                    await self._ingest_embedded_json(
                        page, collector, needle=shortcode, allow_new=False
                    )
                except PlaywrightError as exc:
                    logger.warning("Could not load post %s: %s", shortcode, exc.message)
                finally:
                    await page.close()

        await asyncio.gather(*(enrich(shortcode) for shortcode in targets))

        missing = sum(_needs_details(post) for post in collector.posts())
        if blocked.is_set():
            logger.warning("Instagram started requiring login for post pages; stopped early")
        if missing:
            logger.warning("%d posts still lack timestamp or counts", missing)

    async def _read_page_metadata(self, page: Page, username: str) -> Profile:
        profile = Profile(username=username)

        meta = page.locator('meta[property="og:description"]').first
        if await meta.count():
            description = await meta.get_attribute("content")
            if description:
                profile.fill_missing(parse_og_description(description))

        match = re.match(
            r"(.+?)\s+\(@" + re.escape(username) + r"\)", await page.title(), re.IGNORECASE
        )
        if match:
            profile.full_name = match.group(1).strip()

        return profile

    async def _paginate(
        self, page: Page, collector: ResultCollector, pending: set[asyncio.Task]
    ) -> None:
        config = self.config
        stagnant = 0
        last_seen = collector.seen

        for _ in range(config.max_scrolls):
            if collector.end_of_feed:
                logger.debug("Reached end of timeline")
                return
            if config.max_posts and len(collector.posts()) >= config.max_posts:
                return

            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except PlaywrightError as exc:
                logger.warning("Pagination interrupted: %s", exc.message)
                return
            await asyncio.sleep(config.scroll_delay)
            await _wait_for(pending, timeout=config.navigation_timeout_ms / 1000)

            if collector.seen > last_seen:
                stagnant, last_seen = 0, collector.seen
                continue
            stagnant += 1
            if stagnant >= config.stagnant_rounds:
                logger.info("No new posts after %d scrolls; stopping", stagnant)
                return
