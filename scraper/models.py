from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any


def _improves(current: Any, candidate: Any) -> bool:
    if current is None:
        return candidate is not None
    return current == "" and candidate not in (None, "")


def _missing_updates(target: Any, source: Any) -> dict[str, Any]:
    return {
        f.name: getattr(source, f.name)
        for f in fields(target)
        if _improves(getattr(target, f.name), getattr(source, f.name))
    }


@dataclass(slots=True)
class Post:
    id: str | None
    shortcode: str | None
    url: str | None
    media_type: str
    timestamp: str | None
    caption: str
    likes: int | None
    comments: int | None
    image_url: str | None
    video_thumbnail: str | None

    def merged_with(self, other: Post) -> Post:
        return replace(self, **_missing_updates(self, other))


@dataclass(slots=True)
class Profile:
    username: str
    user_id: str | None = None
    full_name: str | None = None
    bio: str | None = None
    followers: int | None = None
    following: int | None = None
    post_count: int | None = None
    is_private: bool | None = None
    is_verified: bool | None = None

    def fill_missing(self, other: Profile) -> None:
        for name, value in _missing_updates(self, other).items():
            setattr(self, name, value)


@dataclass(slots=True)
class ScrapeStats:
    duration_seconds: float = 0.0
    requests: int = 0
    failed_requests: int = 0
    blocked_requests: int = 0
    bytes_received: int = 0
    payloads_parsed: int = 0
    detail_pages: int = 0
    navigation_retries: int = 0


@dataclass(slots=True)
class ScrapeResult:
    profile: Profile
    posts: list[Post]
    scraped_at: str
    complete: bool
    authenticated: bool
    stats: ScrapeStats = field(default_factory=ScrapeStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self.profile),
            "posts": [asdict(post) for post in self.posts],
            "meta": {
                "scraped_at": self.scraped_at,
                "posts_collected": len(self.posts),
                "complete": self.complete,
                "authenticated": self.authenticated,
                "stats": asdict(self.stats),
            },
        }
