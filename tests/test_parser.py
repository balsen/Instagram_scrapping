import pytest

from scraper.models import Post
from scraper.parser import ParsedPost, belongs_to, extract, loads_lenient, unix_to_iso


def by_code(extraction):
    return {parsed.post.shortcode: parsed for parsed in extraction.posts}


class TestLegacyGraphQL:
    def test_profile(self, fixture):
        (profile,) = extract(fixture("legacy_web_profile_info.json"), "testuser").profiles
        assert profile.full_name == "Test User"
        assert profile.bio == "Coffee & code\nBerlin"
        assert (profile.followers, profile.following, profile.post_count) == (12345, 321, 4)
        assert profile.user_id == "1001"
        assert profile.is_verified is True

    def test_image_post(self, fixture):
        post = by_code(extract(fixture("legacy_web_profile_info.json"), "testuser"))["AAA111"].post
        assert post.id == "9001"
        assert post.url == "https://www.instagram.com/p/AAA111/"
        assert post.media_type == "image"
        assert post.timestamp == "2025-10-01T12:00:00+00:00"
        assert post.caption == "First post"
        assert (post.likes, post.comments) == (100, 5)
        assert post.image_url == "https://cdn.example/aaa.jpg"
        assert post.video_thumbnail is None

    def test_carousel_children_are_not_separate_posts(self, fixture):
        posts = by_code(extract(fixture("legacy_web_profile_info.json"), "testuser"))
        assert set(posts) == {"AAA111", "BBB222"}

    def test_carousel_starting_with_video_uses_video_thumbnail(self, fixture):
        post = by_code(extract(fixture("legacy_web_profile_info.json"), "testuser"))["BBB222"].post
        assert post.media_type == "carousel"
        assert post.image_url is None
        assert post.video_thumbnail == "https://cdn.example/bbb-1.jpg"
        assert post.caption == ""

    def test_other_connections_do_not_end_the_feed(self, fixture):
        assert extract(fixture("legacy_web_profile_info.json"), "testuser").end_of_feed is False


class TestV1:
    def test_video_uses_largest_cover_and_numeric_id(self, fixture):
        post = by_code(extract(fixture("v1_timeline.json"), "testuser"))["CCC333"].post
        assert post.id == "9003"
        assert post.media_type == "video"
        assert post.video_thumbnail == "https://cdn.example/ccc-large.jpg"
        assert post.image_url is None
        assert (post.likes, post.comments) == (2000, 40)

    def test_hidden_likes_are_none_not_zero(self, fixture):
        post = by_code(extract(fixture("v1_timeline.json"), "testuser"))["DDD444"].post
        assert post.likes is None
        assert post.comments == 1

    def test_carousel_uses_first_child(self, fixture):
        post = by_code(extract(fixture("v1_timeline.json"), "testuser"))["DDD444"].post
        assert post.media_type == "carousel"
        assert post.image_url == "https://cdn.example/ddd-1.jpg"
        assert post.video_thumbnail is None

    def test_owner_and_timeline_provenance(self, fixture):
        posts = by_code(extract(fixture("v1_timeline.json"), "testuser"))
        assert posts["CCC333"].owner_username == "testuser"
        assert posts["ZZZ999"].owner_username == "otheruser"
        assert all(parsed.in_timeline for parsed in posts.values())

    def test_end_of_feed(self, fixture):
        assert extract(fixture("v1_timeline.json"), "testuser").end_of_feed is True

    def test_rest_feed_root(self):
        payload = {
            "items": [
                {"pk": "1", "code": "R1", "media_type": 1, "image_versions2": {"candidates": []}}
            ],
            "more_available": False,
        }
        assert extract(payload, "testuser").end_of_feed is True


class TestLoggedOutPolaris:
    def test_profile(self, fixture):
        profiles = []
        for blob in fixture("polaris_profile_page.json"):
            profiles += extract(blob, "testuser").profiles
        (profile,) = profiles
        assert profile.user_id == "1001"
        assert (profile.followers, profile.following) == (12345, 321)
        assert profile.post_count is None

    def test_grid_nodes_have_partial_data(self, fixture):
        posts = by_code(extract(fixture("polaris_profile_page.json")[1], "testuser"))
        assert set(posts) == {"EEE555", "FFF666", "GGG777"}
        grid = posts["EEE555"].post
        assert grid.id == "9005"
        assert grid.image_url == "https://cdn.example/eee555-640.jpg"
        assert (grid.timestamp, grid.likes, grid.comments) == (None, None, None)
        assert posts["FFF666"].post.video_thumbnail == "https://cdn.example/fff666-640.jpg"

    def test_grid_has_more_pages(self, fixture):
        assert extract(fixture("polaris_profile_page.json")[1], "testuser").end_of_feed is False

    def test_post_page_has_full_data(self, fixture):
        page = fixture("polaris_post_pages.json")["GGG777"]
        posts = by_code(extract(page, "testuser"))
        post = posts["GGG777"].post
        assert post.timestamp == "2025-10-02T04:40:00+00:00"
        assert post.likes is None
        assert post.comments == 4
        assert post.video_thumbnail == "https://cdn.example/ggg777-child1.jpg"
        assert posts["HHH888"].owner_username == "neighbour"
        assert not posts["HHH888"].in_timeline


POST = Post(None, "X", None, "image", None, "", None, None, None, None)


@pytest.mark.parametrize(
    ("owner_username", "owner_id", "coauthors", "in_timeline", "user_id", "expected"),
    [
        ("otheruser", "2002", (), True, "1001", True),
        ("testuser", None, (), False, None, True),
        ("TestUser", "1", (), False, "2", True),
        ("otheruser", None, (), False, None, False),
        ("otheruser", None, ("TESTUSER",), False, None, True),
        (None, "1001", (), False, "1001", True),
        (None, "2002", (), False, "1001", False),
        (None, "2002", (), False, None, True),
        (None, None, (), False, None, True),
    ],
)
def test_belongs_to(owner_username, owner_id, coauthors, in_timeline, user_id, expected):
    parsed = ParsedPost(POST, owner_username, owner_id, coauthors, in_timeline)
    assert belongs_to(parsed, "testuser", user_id) is expected


@pytest.mark.parametrize("prefix", ["", "for (;;);", ")]}'", "  for (;;);"])
def test_loads_lenient(prefix):
    assert loads_lenient(prefix + '{"ok": true}') == {"ok": True}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1759320000, "2025-10-01T12:00:00+00:00"),
        ("1759320000", "2025-10-01T12:00:00+00:00"),
        (None, None),
        (0, None),
        ("soon", None),
        (10**20, None),
    ],
)
def test_unix_to_iso(value, expected):
    assert unix_to_iso(value) == expected
