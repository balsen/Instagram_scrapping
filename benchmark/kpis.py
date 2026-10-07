from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from scraper.instagram import parse_count

from .ground_truth import PostTruth, ProfileTruth

REQUIRED_PROFILE_FIELDS = ("full_name", "followers", "following", "bio")
REQUIRED_POST_FIELDS = ("id", "shortcode", "timestamp", "caption", "likes", "comments")
LOGGED_OUT_VISIBLE_POSTS = 12
COUNT_DRIFT = 0.03
CAPTION_PREFIX = 40
BLOCK_OUTCOMES = {"blocked", "rate_limited"}
TRUNCATION_TAIL = re.compile(r'(?:"\.?|\.\.\.|…)+$')

Check = tuple[bool | None, Any, Any]


@dataclass
class CaseRun:
    username: str
    expected: str
    outcome: str
    wall_seconds: float
    error: str | None = None
    result: dict[str, Any] | None = None
    schema_errors: list[str] = field(default_factory=list)
    checks: dict[str, list[bool]] = field(default_factory=dict)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    media: list[bool] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return self.expected != "invalid"

    @property
    def succeeded(self) -> bool:
        return self.outcome == "ok" and self.result is not None


@dataclass
class Kpi:
    category: str
    name: str
    value: float | None
    display: str
    target: str = "–"
    passed: bool | None = None


def display_unit(display: str) -> float:
    text = display.strip().upper().replace(",", "").replace(" ", "")
    multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}.get(text[-1:], 1)
    if multiplier == 1:
        return 1
    decimals = len(text[:-1].split(".", 1)[1]) if "." in text else 0
    return multiplier / 10**decimals


def count_matches(scraped: int | None, display: str | None) -> bool | None:
    truth = parse_count(display) if display else None
    if truth is None:
        return None
    if scraped is None:
        return False
    return abs(scraped - truth) <= max(display_unit(display), truth * COUNT_DRIFT, 2)


def date_matches(timestamp: str | None, truth: date | None) -> bool | None:
    if truth is None:
        return None
    if not timestamp:
        return False
    return abs((datetime.fromisoformat(timestamp).date() - truth).days) <= 1


def _normalize(text: str) -> str:
    return " ".join(text.split())


def prefix_matches(scraped: str | None, truth: str | None) -> bool | None:
    if truth is None:
        return None
    truth = TRUNCATION_TAIL.sub("", _normalize(truth)).strip()
    if not truth:
        return None
    if scraped is None:
        return False
    prefix = truth[:CAPTION_PREFIX]
    return _normalize(scraped).startswith(prefix)


def profile_checks(result: dict[str, Any], truth: ProfileTruth) -> dict[str, Check]:
    pairs = {
        "profile.full_name": (prefix_matches, "full_name", truth.full_name),
        "profile.followers": (count_matches, "followers", truth.followers),
        "profile.following": (count_matches, "following", truth.following),
        "profile.post_count": (count_matches, "post_count", truth.posts),
        "profile.bio": (prefix_matches, "bio", truth.bio),
    }
    return {
        name: (compare(result.get(key), reference), result.get(key), reference)
        for name, (compare, key, reference) in pairs.items()
    }


def post_checks(post: dict[str, Any], truth: PostTruth) -> dict[str, Check]:
    pairs = {
        "post.likes": (count_matches, "likes", truth.likes),
        "post.comments": (count_matches, "comments", truth.comments),
        "post.date": (date_matches, "timestamp", truth.date),
        "post.caption": (prefix_matches, "caption", truth.caption),
    }
    return {
        name: (compare(post.get(key), reference), post.get(key), reference)
        for name, (compare, key, reference) in pairs.items()
    }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def completeness(result: dict[str, Any]) -> tuple[int, int]:
    filled = sum(result.get(name) is not None for name in REQUIRED_PROFILE_FIELDS)
    total = len(REQUIRED_PROFILE_FIELDS)
    for post in result["posts"]:
        filled += sum(post.get(name) is not None for name in REQUIRED_POST_FIELDS)
        filled += bool(post.get("image_url") or post.get("video_thumbnail"))
        total += len(REQUIRED_POST_FIELDS) + 1
    return filled, total


def visible_post_target(result: dict[str, Any]) -> int:
    post_count = result.get("post_count")
    if result["meta"]["authenticated"]:
        return post_count or len(result["posts"])
    cap = LOGGED_OUT_VISIBLE_POSTS
    return min(post_count, cap) if post_count is not None else cap


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _at_least(value: float | None, target: float) -> bool | None:
    return None if value is None else value >= target


def _at_most(value: float | None, target: float) -> bool | None:
    return None if value is None else value <= target


def compute_kpis(runs: list[CaseRun]) -> list[Kpi]:
    expected_ok = [run for run in runs if run.expected == "ok"]
    live = [run for run in runs if run.live]
    negative = [run for run in runs if run.expected != "ok"]
    done = [run for run in expected_ok if run.succeeded]
    results = [run.result for run in done]
    stats = [result["meta"]["stats"] for result in results]
    posts = [post for result in results for post in result["posts"]]

    success = ratio(len(done), len(expected_ok))
    blocks = ratio(sum(run.outcome in BLOCK_OUTCOMES for run in live), len(live))
    classified = ratio(sum(run.outcome == run.expected for run in negative), len(negative))
    retries = ratio(sum(s["navigation_retries"] for s in stats), len(stats))

    schema_ok = ratio(sum(not run.schema_errors for run in done), len(done))
    filled = sum(completeness(result)[0] for result in results)
    required = sum(completeness(result)[1] for result in results)
    checks = [ok for run in done for values in run.checks.values() for ok in values]
    media = [ok for run in done for ok in run.media]
    unique = sum(len({p["shortcode"] for p in result["posts"]}) for result in results)

    visible = ratio(len(posts), sum(visible_post_target(result) for result in results))
    timeline = ratio(len(posts), sum(result.get("post_count") or 0 for result in results))

    latencies = [run.wall_seconds for run in done]
    p50, p95 = percentile(latencies, 50), percentile(latencies, 95)
    total_seconds = sum(latencies)
    throughput = ratio(len(posts) * 60, total_seconds)
    requests = ratio(sum(s["requests"] for s in stats), len(stats))
    megabytes = sum(s["bytes_received"] for s in stats) / 1_000_000
    mb_per_profile = ratio(megabytes, len(stats))
    mb_per_1k_posts = ratio(megabytes * 1000, len(posts))
    blocked = sum(s["blocked_requests"] for s in stats)
    avoided = ratio(blocked, blocked + sum(s["requests"] for s in stats))

    def seconds(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f} s"

    def number(value: float | None, unit: str = "", digits: int = 1) -> str:
        return "n/a" if value is None else f"{value:,.{digits}f}{unit}"

    rows: list[tuple[str, str, float | None, str, str, Callable[[], bool | None]]] = [
        (
            "Reliability",
            "Success rate",
            success,
            _pct(success),
            "≥ 95%",
            lambda: _at_least(success, 0.95),
        ),
        (
            "Reliability",
            "Block / rate-limit rate",
            blocks,
            _pct(blocks),
            "≤ 5%",
            lambda: _at_most(blocks, 0.05),
        ),
        (
            "Reliability",
            "Error classification accuracy",
            classified,
            _pct(classified),
            "100%",
            lambda: _at_least(classified, 1.0),
        ),
        (
            "Reliability",
            "Navigation retries per profile",
            retries,
            number(retries, "", 2),
            "≤ 1",
            lambda: _at_most(retries, 1),
        ),
        (
            "Data quality",
            "Schema conformance",
            schema_ok,
            _pct(schema_ok),
            "100%",
            lambda: _at_least(schema_ok, 1.0),
        ),
        (
            "Data quality",
            "Field completeness (fill rate)",
            ratio(filled, required),
            _pct(ratio(filled, required)),
            "≥ 98%",
            lambda: _at_least(ratio(filled, required), 0.98),
        ),
        (
            "Data quality",
            "Accuracy vs. independent source",
            ratio(sum(checks), len(checks)),
            _pct(ratio(sum(checks), len(checks))),
            "≥ 95%",
            lambda: _at_least(ratio(sum(checks), len(checks)), 0.95),
        ),
        (
            "Data quality",
            "Media URL validity",
            ratio(sum(media), len(media)),
            _pct(ratio(sum(media), len(media))),
            "≥ 98%",
            lambda: _at_least(ratio(sum(media), len(media)), 0.98),
        ),
        (
            "Data quality",
            "Duplicate rate",
            ratio(len(posts) - unique, len(posts)),
            _pct(ratio(len(posts) - unique, len(posts))),
            "0%",
            lambda: _at_most(ratio(len(posts) - unique, len(posts)), 0),
        ),
        (
            "Coverage",
            "Coverage of posts visible to the session",
            visible,
            _pct(visible),
            "≥ 95%",
            lambda: _at_least(visible, 0.95),
        ),
        ("Coverage", "Coverage of full timeline", timeline, _pct(timeline), "–", lambda: None),
        ("Performance", "Latency per profile, p50", p50, seconds(p50), "–", lambda: None),
        (
            "Performance",
            "Latency per profile, p95",
            p95,
            seconds(p95),
            "≤ 60 s",
            lambda: _at_most(p95, 60),
        ),
        (
            "Performance",
            "Throughput",
            throughput,
            number(throughput, " posts/min"),
            "≥ 20 posts/min",
            lambda: _at_least(throughput, 20),
        ),
        (
            "Efficiency",
            "Requests per profile",
            requests,
            number(requests, "", 0),
            "–",
            lambda: None,
        ),
        (
            "Efficiency",
            "Data transferred per profile",
            mb_per_profile,
            number(mb_per_profile, " MB"),
            "≤ 25 MB",
            lambda: _at_most(mb_per_profile, 25),
        ),
        (
            "Efficiency",
            "Data transferred per 1,000 posts",
            mb_per_1k_posts,
            number(mb_per_1k_posts, " MB", 0),
            "–",
            lambda: None,
        ),
        (
            "Efficiency",
            "Heavy requests avoided (images, video, fonts)",
            avoided,
            _pct(avoided),
            "–",
            lambda: None,
        ),
    ]
    return [
        Kpi(category, name, value, display, target, check())
        for category, name, value, display, target, check in rows
    ]
