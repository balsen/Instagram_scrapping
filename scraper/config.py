from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    base_url: str = "https://www.instagram.com"
    headless: bool = True
    max_scrolls: int = 100
    scroll_delay: float = 1.5
    stagnant_rounds: int = 5
    max_posts: int | None = None
    settle_timeout_ms: int = 8_000
    navigation_timeout_ms: int = 30_000
    navigation_retries: int = 3
    retry_backoff: float = 2.0
    storage_state: Path | None = None
    proxy: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    block_media: bool = True
    enrich_posts: bool = True
    detail_concurrency: int = 2

    def __post_init__(self) -> None:
        if self.max_scrolls < 0 or self.stagnant_rounds < 1:
            raise ConfigurationError("max_scrolls must be >= 0, stagnant_rounds >= 1")
        if self.scroll_delay < 0 or self.retry_backoff < 0:
            raise ConfigurationError("delays must be >= 0")
        if self.navigation_retries < 1:
            raise ConfigurationError("navigation_retries must be >= 1")
        if self.detail_concurrency < 1:
            raise ConfigurationError("detail_concurrency must be >= 1")
        if self.max_posts is not None and self.max_posts < 1:
            raise ConfigurationError("max_posts must be >= 1")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ScraperConfig:
        env = os.environ if env is None else env
        defaults = cls()

        def text(key: str) -> str | None:
            value = env.get(key, "").strip()
            return value or None

        def number(key: str, default: float, cast: type = int):
            value = text(key)
            if value is None:
                return default
            try:
                return cast(value)
            except ValueError as exc:
                raise ConfigurationError(f"{key} must be a number, got {value!r}") from exc

        def flag(key: str, default: bool) -> bool:
            value = text(key)
            return default if value is None else value.lower() in _TRUE

        storage_state = text("IG_STORAGE_STATE")
        return cls(
            base_url=(text("IG_BASE_URL") or defaults.base_url).rstrip("/"),
            headless=flag("IG_HEADLESS", defaults.headless),
            max_scrolls=number("IG_MAX_SCROLLS", defaults.max_scrolls),
            scroll_delay=number("IG_SCROLL_DELAY", defaults.scroll_delay, float),
            stagnant_rounds=number("IG_STAGNANT_ROUNDS", defaults.stagnant_rounds),
            max_posts=number("IG_MAX_POSTS", 0) or None,
            navigation_timeout_ms=number("IG_NAV_TIMEOUT_MS", defaults.navigation_timeout_ms),
            navigation_retries=number("IG_NAV_RETRIES", defaults.navigation_retries),
            storage_state=Path(storage_state).expanduser() if storage_state else None,
            proxy=text("IG_PROXY"),
            user_agent=text("IG_USER_AGENT") or defaults.user_agent,
            block_media=flag("IG_BLOCK_MEDIA", defaults.block_media),
            enrich_posts=flag("IG_ENRICH_POSTS", defaults.enrich_posts),
            detail_concurrency=number("IG_DETAIL_CONCURRENCY", defaults.detail_concurrency),
        )
