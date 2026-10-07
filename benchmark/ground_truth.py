from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from scraper import ScraperConfig
from scraper.browser import BrowserManager

COUNT = r"[\d.,]+\s?[KMB]?"
POST_DESCRIPTION_RE = re.compile(
    rf"^(?:(?P<likes>{COUNT}) likes?, )?"
    rf"(?:(?P<comments>{COUNT}) comments? - )?"
    r"(?P<owner>[A-Za-z0-9._]+) on (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})"
    r'(?:: "(?P<caption>.*))?$',
    re.DOTALL,
)
PROFILE_TITLE_RE = re.compile(r"^(?P<name>.*?)\s*\(@[A-Za-z0-9._]+\)")
BIO_RE = re.compile(r'on Instagram: "(?P<bio>.*)', re.DOTALL)
META_JS = """
() => Object.fromEntries(
    Array.from(document.querySelectorAll("meta[property], meta[name]"), (meta) => [
        meta.getAttribute("property") || meta.getAttribute("name"),
        meta.content || "",
    ])
)
"""


@dataclass(slots=True)
class PostTruth:
    shortcode: str
    likes: str | None
    comments: str | None
    owner: str
    date: date
    caption: str | None


@dataclass(slots=True)
class ProfileTruth:
    full_name: str | None
    followers: str | None
    following: str | None
    posts: str | None
    bio: str | None


def _display_count(text: str, label: str) -> str | None:
    match = re.search(rf"({COUNT})\s+{label}\b", text, re.IGNORECASE)
    return match.group(1) if match else None


def parse_post_description(shortcode: str, text: str) -> PostTruth | None:
    match = POST_DESCRIPTION_RE.match(text.strip())
    if not match:
        return None
    return PostTruth(
        shortcode=shortcode,
        likes=match.group("likes"),
        comments=match.group("comments"),
        owner=match.group("owner"),
        date=datetime.strptime(match.group("date"), "%B %d, %Y").date(),
        caption=match.group("caption"),
    )


def parse_profile_meta(meta: dict[str, str]) -> ProfileTruth:
    og_description = meta.get("og:description", "")
    title = PROFILE_TITLE_RE.match(meta.get("og:title", ""))
    bio = BIO_RE.search(meta.get("description", ""))
    return ProfileTruth(
        full_name=title.group("name") if title else None,
        followers=_display_count(og_description, "Followers"),
        following=_display_count(og_description, "Following"),
        posts=_display_count(og_description, "Posts"),
        bio=bio.group("bio") if bio else None,
    )


async def _read_meta(page: Page, url: str, timeout_ms: int) -> dict[str, str] | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return await page.evaluate(META_JS)
    except PlaywrightError:
        return None


async def fetch_ground_truth(
    config: ScraperConfig, username: str, shortcodes: list[str]
) -> tuple[ProfileTruth | None, dict[str, PostTruth]]:
    async with BrowserManager(config) as browser:
        page = await browser.new_page()
        profile_meta = await _read_meta(
            page, f"{config.base_url}/{username}/", config.navigation_timeout_ms
        )
        profile = parse_profile_meta(profile_meta) if profile_meta else None

        posts: dict[str, PostTruth] = {}
        for shortcode in shortcodes:
            meta = await _read_meta(
                page, f"{config.base_url}/p/{shortcode}/", config.navigation_timeout_ms
            )
            truth = parse_post_description(shortcode, (meta or {}).get("og:description", ""))
            if truth:
                posts[shortcode] = truth
        return profile, posts
