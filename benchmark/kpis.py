from __future__ import annotations

import math
import re
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


TARGETS: dict[str, tuple[str, float, str]] = {
    "Success rate": (">=", 0.95, "≥ 95%"),
    "Block / rate-limit rate": ("<=", 0.05, "≤ 5%"),
    "Error classification accuracy": (">=", 1.0, "100%"),
    "Navigation retries per profile": ("<=", 1, "≤ 1"),
    "Schema conformance": (">=", 1.0, "100%"),
    "Field completeness (fill rate)": (">=", 0.98, "≥ 98%"),
    "Accuracy vs. independent source": (">=", 0.95, "≥ 95%"),
    "Media URL validity": (">=", 0.98, "≥ 98%"),
    "Duplicate rate": ("<=", 0, "0%"),
    "Coverage of posts visible to the session": (">=", 0.95, "≥ 95%"),
    "Latency per profile, p95": ("<=", 60, "≤ 60 s"),
    "Throughput": (">=", 20, "≥ 20 posts/min"),
    "Data transferred per profile": ("<=", 25, "≤ 25 MB"),
}


def _kpi(category: str, name: str, value: float | None, display: str) -> Kpi:
    if name not in TARGETS:
        return Kpi(category, name, value, display)
    operator, threshold, label = TARGETS[name]
    if value is None:
        passed = None
    else:
        passed = value >= threshold if operator == ">=" else value <= threshold
    return Kpi(category, name, value, display, label, passed)


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def _number(value: float | None, unit: str = "", digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}{unit}"


def compute_kpis(runs: list[CaseRun]) -> list[Kpi]:
    expected_ok = [run for run in runs if run.expected == "ok"]
    live = [run for run in runs if run.live]
    negative = [run for run in runs if run.expected != "ok"]
    done = [run for run in expected_ok if run.succeeded]
    results = [run.result for run in done]
    stats = [result["meta"]["stats"] for result in results]
    posts = [post for result in results for post in result["posts"]]
    checks = [ok for run in done for values in run.checks.values() for ok in values]
    media = [ok for run in done for ok in run.media]
    counts = [completeness(result) for result in results]
    filled = sum(done_fields for done_fields, _ in counts)
    required = sum(total for _, total in counts)
    unique = sum(len({post["shortcode"] for post in result["posts"]}) for result in results)
    latencies = [run.wall_seconds for run in done]
    requests = sum(s["requests"] for s in stats)
    blocked = sum(s["blocked_requests"] for s in stats)
    megabytes = sum(s["bytes_received"] for s in stats) / 1_000_000

    success = ratio(len(done), len(expected_ok))
    blocks = ratio(sum(run.outcome in BLOCK_OUTCOMES for run in live), len(live))
    classified = ratio(sum(run.outcome == run.expected for run in negative), len(negative))
    retries = ratio(sum(s["navigation_retries"] for s in stats), len(stats))
    schema_ok = ratio(sum(not run.schema_errors for run in done), len(done))
    fill_rate = ratio(filled, required)
    accuracy = ratio(sum(checks), len(checks))
    media_ok = ratio(sum(media), len(media))
    duplicates = ratio(len(posts) - unique, len(posts))
    visible = ratio(len(posts), sum(visible_post_target(result) for result in results))
    timeline = ratio(len(posts), sum(result.get("post_count") or 0 for result in results))
    p50, p95 = percentile(latencies, 50), percentile(latencies, 95)
    throughput = ratio(len(posts) * 60, sum(latencies))
    per_profile = ratio(requests, len(stats))
    mb_per_profile = ratio(megabytes, len(stats))
    mb_per_1k_posts = ratio(megabytes * 1000, len(posts))
    avoided = ratio(blocked, blocked + requests)

    return [
        _kpi("Reliability", "Success rate", success, _pct(success)),
        _kpi("Reliability", "Block / rate-limit rate", blocks, _pct(blocks)),
        _kpi("Reliability", "Error classification accuracy", classified, _pct(classified)),
        _kpi("Reliability", "Navigation retries per profile", retries, _number(retries, "", 2)),
        _kpi("Data quality", "Schema conformance", schema_ok, _pct(schema_ok)),
        _kpi("Data quality", "Field completeness (fill rate)", fill_rate, _pct(fill_rate)),
        _kpi("Data quality", "Accuracy vs. independent source", accuracy, _pct(accuracy)),
        _kpi("Data quality", "Media URL validity", media_ok, _pct(media_ok)),
        _kpi("Data quality", "Duplicate rate", duplicates, _pct(duplicates)),
        _kpi("Coverage", "Coverage of posts visible to the session", visible, _pct(visible)),
        _kpi("Coverage", "Coverage of full timeline", timeline, _pct(timeline)),
        _kpi("Performance", "Latency per profile, p50", p50, _seconds(p50)),
        _kpi("Performance", "Latency per profile, p95", p95, _seconds(p95)),
        _kpi("Performance", "Throughput", throughput, _number(throughput, " posts/min")),
        _kpi("Efficiency", "Requests per profile", per_profile, _number(per_profile, "", 0)),
        _kpi(
            "Efficiency",
            "Data transferred per profile",
            mb_per_profile,
            _number(mb_per_profile, " MB"),
        ),
        _kpi(
            "Efficiency",
            "Data transferred per 1,000 posts",
            mb_per_1k_posts,
            _number(mb_per_1k_posts, " MB", 0),
        ),
        _kpi(
            "Efficiency",
            "Heavy requests avoided (images, video, fonts)",
            avoided,
            _pct(avoided),
        ),
    ]
