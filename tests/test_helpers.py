import json
from pathlib import Path

import pytest

from scraper import ConfigurationError, InvalidUsername, ScraperConfig, clean_username
from scraper.exporter import export_json
from scraper.instagram import parse_count, parse_og_description


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("natgeo", "natgeo"),
        ("  @nat.geo_1 ", "nat.geo_1"),
        ("https://www.instagram.com/natgeo/", "natgeo"),
        ("instagram.com/natgeo?hl=en", "natgeo"),
        ("https://instagram.com/natgeo/reels/", "natgeo"),
    ],
)
def test_clean_username(raw, expected):
    assert clean_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "@", "bad name", "../etc", "a" * 31, None, 42])
def test_clean_username_rejects(raw):
    with pytest.raises(InvalidUsername):
        clean_username(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12,345", 12345),
        ("1.2M", 1_200_000),
        ("87.5K", 87_500),
        ("3B", 3_000_000_000),
        ("268M", 268_000_000),
        ("", None),
        ("lots", None),
    ],
)
def test_parse_count(raw, expected):
    assert parse_count(raw) == expected


def test_parse_og_description():
    profile = parse_og_description(
        "268M Followers, 194 Following, 32K Posts - See Instagram photos and videos "
        "from National Geographic (@natgeo)"
    )
    assert (profile.followers, profile.following, profile.post_count) == (
        268_000_000,
        194,
        32_000,
    )


def test_config_from_env():
    config = ScraperConfig.from_env(
        {
            "IG_HEADLESS": "false",
            "IG_MAX_POSTS": "50",
            "IG_SCROLL_DELAY": "0.5",
            "IG_STORAGE_STATE": "~/state.json",
            "IG_BASE_URL": "http://localhost:8000/",
            "IG_ENRICH_POSTS": "0",
        }
    )
    assert config.headless is False
    assert config.max_posts == 50
    assert config.scroll_delay == 0.5
    assert config.storage_state == Path("~/state.json").expanduser()
    assert config.base_url == "http://localhost:8000"
    assert config.enrich_posts is False


def test_config_defaults_from_empty_env():
    assert ScraperConfig.from_env({}) == ScraperConfig()


@pytest.mark.parametrize(
    "env",
    [{"IG_MAX_SCROLLS": "many"}, {"IG_NAV_RETRIES": "0"}, {"IG_DETAIL_CONCURRENCY": "0"}],
)
def test_config_rejects_bad_values(env):
    with pytest.raises(ConfigurationError):
        ScraperConfig.from_env(env)


def test_export_json_is_atomic_and_utf8(tmp_path):
    path = export_json({"bio": "café 🌎"}, tmp_path / "nested" / "out.json")
    assert json.loads(path.read_text(encoding="utf-8")) == {"bio": "café 🌎"}
    assert "café 🌎" in path.read_text(encoding="utf-8")
    assert [p.name for p in path.parent.iterdir()] == ["out.json"]
