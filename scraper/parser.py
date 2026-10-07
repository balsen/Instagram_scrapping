from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from .models import Post, Profile

POST_URL = "https://www.instagram.com/p/{shortcode}/"
V1_MEDIA_TYPES = {1: "image", 2: "video", 8: "carousel"}
TIMELINE_KEY_MARKERS = ("edge_owner_to_timeline_media", "user_timeline", "ordered_timeline")
PROFILE_KEYS = frozenset(
    {"biography", "follower_count", "edge_followed_by", "media_count", "full_name"}
)
ANTI_HIJACK_PREFIXES = ("for (;;);", ")]}'")


class ParsedPost(NamedTuple):
    post: Post
    owner_username: str | None
    owner_id: str | None
    coauthors: tuple[str, ...] = ()
    in_timeline: bool = False


@dataclass(slots=True)
class Extraction:
    posts: list[ParsedPost] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    end_of_feed: bool = False


def loads_lenient(text: str) -> Any:
    text = text.lstrip()
    for prefix in ANTI_HIJACK_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return json.loads(text)


def unix_to_iso(timestamp: Any) -> str | None:
    if timestamp in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_id(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _edge_count(node: dict, key: str) -> int | None:
    edge = node.get(key)
    return _as_int(edge.get("count")) if isinstance(edge, dict) else None


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def is_media_node(node: dict) -> bool:
    has_code = bool(node.get("shortcode") or node.get("code"))
    has_id = bool(node.get("id") or node.get("pk"))
    has_media = (
        node.get("media_type") in V1_MEDIA_TYPES
        or bool(node.get("image_versions2"))
        or bool(node.get("display_url") or node.get("display_uri") or node.get("thumbnail_src"))
    )
    return has_code and has_id and has_media


def media_type_of(node: dict) -> str:
    v1_type = V1_MEDIA_TYPES.get(node.get("media_type"))
    if v1_type:
        return v1_type
    typename = str(node.get("__typename") or "")
    if "Sidecar" in typename or node.get("edge_sidecar_to_children"):
        return "carousel"
    if "Video" in typename or node.get("is_video") or node.get("video_versions"):
        return "video"
    return "image"


def best_image_url(node: dict) -> str | None:
    candidates = [
        c
        for c in (node.get("image_versions2") or {}).get("candidates") or []
        if isinstance(c, dict) and c.get("url")
    ]
    if candidates:
        best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        return best["url"]

    resources = [
        r for r in node.get("display_resources") or [] if isinstance(r, dict) and r.get("src")
    ]
    if resources:
        return max(resources, key=lambda r: r.get("config_width") or 0)["src"]

    return node.get("display_url") or node.get("display_uri") or node.get("thumbnail_src") or None


def carousel_children(node: dict) -> list[dict]:
    items = node.get("carousel_media")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    edges = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
    return [
        edge["node"]
        for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
    ]


def extract_caption(node: dict) -> str:
    caption = node.get("caption")
    if isinstance(caption, dict):
        return caption.get("text") or ""
    if isinstance(caption, str):
        return caption

    edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if edges and isinstance(edges[0], dict):
        return (edges[0].get("node") or {}).get("text") or ""
    return ""


def extract_likes(node: dict) -> int | None:
    if node.get("like_and_view_counts_disabled"):
        return None
    return _first(
        _as_int(node.get("like_count")),
        _edge_count(node, "edge_media_preview_like"),
        _edge_count(node, "edge_liked_by"),
    )


def extract_comments(node: dict) -> int | None:
    return _first(
        _as_int(node.get("comment_count")),
        _edge_count(node, "edge_media_to_comment"),
        _edge_count(node, "edge_media_preview_comment"),
    )


def media_id(node: dict) -> str | None:
    pk = _as_id(node.get("pk"))
    if pk:
        return pk
    raw = _as_id(node.get("id"))
    return raw.split("_", 1)[0] if raw else None


def post_owner(node: dict) -> tuple[str | None, str | None]:
    username = owner_id = None
    for key in ("owner", "user"):
        owner = node.get(key)
        if isinstance(owner, dict):
            username = username or owner.get("username") or None
            owner_id = owner_id or _as_id(owner.get("pk") or owner.get("id"))
    return username, owner_id


def post_coauthors(node: dict) -> tuple[str, ...]:
    return tuple(
        producer["username"]
        for producer in node.get("coauthor_producers") or []
        if isinstance(producer, dict) and isinstance(producer.get("username"), str)
    )


def belongs_to(parsed: ParsedPost, username: str, user_id: str | None) -> bool:
    if parsed.in_timeline:
        return True
    username = username.lower()
    if any(coauthor.lower() == username for coauthor in parsed.coauthors):
        return True
    if parsed.owner_username:
        return parsed.owner_username.lower() == username
    if parsed.owner_id and user_id:
        return parsed.owner_id == user_id
    return True


def parse_post(node: dict) -> Post:
    shortcode = node.get("shortcode") or node.get("code")
    kind = media_type_of(node)
    image_url = video_thumbnail = None

    if kind == "carousel":
        children = carousel_children(node)
        first = children[0] if children else node
        still = best_image_url(first) or best_image_url(node)
        if children and media_type_of(first) == "video":
            video_thumbnail = still
        else:
            image_url = still
    elif kind == "video":
        video_thumbnail = best_image_url(node)
    else:
        image_url = best_image_url(node)

    return Post(
        id=media_id(node),
        shortcode=shortcode,
        url=POST_URL.format(shortcode=shortcode) if shortcode else None,
        media_type=kind,
        timestamp=unix_to_iso(
            _first(node.get("taken_at_timestamp"), node.get("taken_at"), node.get("timestamp"))
        ),
        caption=extract_caption(node),
        likes=extract_likes(node),
        comments=extract_comments(node),
        image_url=image_url,
        video_thumbnail=video_thumbnail,
    )


def parse_profile(node: dict, username: str) -> Profile | None:
    found = node.get("username")
    if not isinstance(found, str) or found.lower() != username.lower():
        return None
    if not PROFILE_KEYS.intersection(node):
        return None

    bio = node.get("biography")
    is_private = node.get("is_private")
    is_verified = node.get("is_verified")
    return Profile(
        username=found,
        user_id=_as_id(node.get("pk") or node.get("id")),
        full_name=node.get("full_name") or None,
        bio=bio if isinstance(bio, str) else None,
        followers=_first(
            _as_int(node.get("follower_count")), _edge_count(node, "edge_followed_by")
        ),
        following=_first(_as_int(node.get("following_count")), _edge_count(node, "edge_follow")),
        post_count=_first(
            _as_int(node.get("media_count")),
            _edge_count(node, "edge_owner_to_timeline_media"),
        ),
        is_private=is_private if isinstance(is_private, bool) else None,
        is_verified=is_verified if isinstance(is_verified, bool) else None,
    )


def _feed_exhausted(connection: dict) -> bool:
    page_info = connection.get("page_info")
    if isinstance(page_info, dict) and page_info.get("has_next_page") is False:
        return True
    return connection.get("more_available") is False


def extract(data: Any, username: str) -> Extraction:
    result = Extraction()

    def walk(node: Any, key: str = "", in_timeline: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, in_timeline=in_timeline)
            return
        if not isinstance(node, dict):
            return

        if is_media_node(node):
            owner_username, owner_id = post_owner(node)
            result.posts.append(
                ParsedPost(
                    parse_post(node), owner_username, owner_id, post_coauthors(node), in_timeline
                )
            )
            return

        profile = parse_profile(node, username)
        if profile:
            result.profiles.append(profile)

        if any(marker in key for marker in TIMELINE_KEY_MARKERS):
            in_timeline = True
            if _feed_exhausted(node):
                result.end_of_feed = True

        for child_key, value in node.items():
            walk(value, child_key, in_timeline)

    rest_feed = isinstance(data, dict) and "items" in data and "more_available" in data
    walk(data, in_timeline=rest_feed)
    if rest_feed and result.posts and _feed_exhausted(data):
        result.end_of_feed = True

    return result
