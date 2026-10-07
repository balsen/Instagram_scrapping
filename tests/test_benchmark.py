import json
from datetime import date

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from benchmark.ground_truth import parse_post_description, parse_profile_meta
from benchmark.kpis import (
    CaseRun,
    compute_kpis,
    count_matches,
    date_matches,
    display_unit,
    percentile,
    prefix_matches,
)
from scraper.collector import ResultCollector
from scraper.models import ScrapeResult, ScrapeStats
from tests.conftest import ROOT


def test_parse_post_description():
    truth = parse_post_description(
        "DeMa5eeRPYL",
        '21K likes, 47 comments - natgeo on October 7, 2026: "Presented by @rolex. Around',
    )
    assert (truth.likes, truth.comments, truth.owner) == ("21K", "47", "natgeo")
    assert truth.date == date(2026, 10, 7)
    assert truth.caption.startswith("Presented by @rolex")


def test_parse_post_description_with_hidden_likes():
    truth = parse_post_description("X", 'natgeo on May 1, 2026: "Hello"')
    assert (truth.likes, truth.comments) == (None, None)
    assert truth.caption == 'Hello"'


def test_parse_post_description_rejects_other_text():
    assert parse_post_description("X", "See Instagram photos and videos") is None


def test_parse_profile_meta():
    truth = parse_profile_meta(
        {
            "og:title": "National Geographic (@natgeo) • Instagram photos and videos",
            "og:description": "268M Followers, 195 Following, 32K Posts - See Instagram",
            "description": '268M Followers - National Geographic (@natgeo) on Instagram: "Step',
        }
    )
    assert truth.full_name == "National Geographic"
    assert (truth.followers, truth.following, truth.posts) == ("268M", "195", "32K")
    assert truth.bio == "Step"


@pytest.mark.parametrize(
    ("display", "unit"), [("21K", 1000), ("1.2M", 100_000), ("47", 1), ("12,345", 1)]
)
def test_display_unit(display, unit):
    assert display_unit(display) == unit


@pytest.mark.parametrize(
    ("scraped", "display", "expected"),
    [
        (20_950, "21K", True),
        (18_838, "21K", False),
        (268_425_904, "268M", True),
        (47, "47", True),
        (49, "47", True),
        (60, "47", False),
        (None, "47", False),
        (5, None, None),
    ],
)
def test_count_matches(scraped, display, expected):
    assert count_matches(scraped, display) is expected


def test_date_and_prefix_matches():
    assert date_matches("2026-10-07T23:30:00+00:00", date(2026, 10, 8)) is True
    assert date_matches("2026-10-01T00:00:00+00:00", date(2026, 10, 8)) is False
    assert date_matches(None, None) is None
    assert prefix_matches("Hello   world, long caption", 'Hello world..."') is True
    assert prefix_matches("Different", "Hello") is False
    assert prefix_matches("anything", '"') is None
    assert prefix_matches("🇵🇹", '🇵🇹".') is True
    assert prefix_matches("Done.", 'Done.".') is True


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([10.0], 95) == 10.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == pytest.approx(4.8)


def _result(posts=12, post_count=100, stats=None):
    return {
        "full_name": "A",
        "followers": 1,
        "following": 1,
        "bio": "b",
        "post_count": post_count,
        "posts": [
            {
                "id": str(i),
                "shortcode": f"S{i}",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "caption": "",
                "likes": 1,
                "comments": 0,
                "image_url": "u",
                "video_thumbnail": None,
            }
            for i in range(posts)
        ],
        "meta": {
            "authenticated": False,
            "stats": stats
            or {
                "requests": 50,
                "bytes_received": 2_000_000,
                "blocked_requests": 50,
                "navigation_retries": 0,
            },
        },
    }


def test_compute_kpis():
    runs = [
        CaseRun(
            "a",
            "ok",
            "ok",
            20.0,
            result=_result(),
            checks={"post.likes": [True, True]},
            media=[True] * 12,
        ),
        CaseRun("b", "ok", "blocked", 5.0, error="login wall"),
        CaseRun("missing", "not_found", "not_found", 3.0),
        CaseRun("bad name", "invalid", "invalid", 0.0),
    ]
    kpis = {kpi.name: kpi for kpi in compute_kpis(runs)}

    assert kpis["Success rate"].value == 0.5
    assert kpis["Success rate"].passed is False
    assert kpis["Block / rate-limit rate"].value == pytest.approx(1 / 3)
    assert kpis["Error classification accuracy"].value == 1.0
    assert kpis["Field completeness (fill rate)"].value == 1.0
    assert kpis["Coverage of posts visible to the session"].value == 1.0
    assert kpis["Coverage of full timeline"].value == 0.12
    assert kpis["Duplicate rate"].value == 0
    assert kpis["Throughput"].value == 36.0
    assert kpis["Data transferred per profile"].value == 2.0
    assert kpis["Heavy requests avoided (images, video, fonts)"].value == 0.5


def test_scraper_output_matches_published_schema(fixture):
    collector = ResultCollector("testuser")
    for name in ("legacy_web_profile_info.json", "v1_timeline.json"):
        collector.feed(fixture(name))
    result = ScrapeResult(
        profile=collector.profile,
        posts=collector.posts(),
        scraped_at="2026-10-07T12:00:00+00:00",
        complete=True,
        authenticated=False,
        stats=ScrapeStats(duration_seconds=1.5, requests=3),
    ).to_dict()

    schema = json.loads((ROOT / "schema" / "output.schema.json").read_text())
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(result))
    assert errors == []
