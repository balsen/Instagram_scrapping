from __future__ import annotations

from typing import Any

from .models import Post, Profile
from .parser import ParsedPost, belongs_to, extract


class ResultCollector:
    def __init__(self, username: str) -> None:
        self.username = username
        self.profile = Profile(username=username)
        self.end_of_feed = False
        self.payloads = 0
        self._posts: dict[str, ParsedPost] = {}

    def feed(self, data: Any, allow_new: bool = True) -> int:
        extraction = extract(data, self.username)
        self.payloads += 1

        for profile in extraction.profiles:
            self.profile.fill_missing(profile)

        new = 0
        for parsed in extraction.posts:
            key = parsed.post.shortcode or parsed.post.id
            if not key:
                continue
            existing = self._posts.get(key)
            if existing is None:
                if not allow_new:
                    continue
                self._posts[key] = parsed
                new += 1
            else:
                self._posts[key] = ParsedPost(
                    existing.post.merged_with(parsed.post),
                    existing.owner_username or parsed.owner_username,
                    existing.owner_id or parsed.owner_id,
                    existing.coauthors or parsed.coauthors,
                    existing.in_timeline or parsed.in_timeline,
                )

        self.end_of_feed = self.end_of_feed or extraction.end_of_feed
        return new

    @property
    def seen(self) -> int:
        return len(self._posts)

    def posts(self) -> list[Post]:
        own = [
            parsed.post
            for parsed in self._posts.values()
            if belongs_to(parsed, self.username, self.profile.user_id)
        ]
        return sorted(own, key=lambda post: post.timestamp or "", reverse=True)
