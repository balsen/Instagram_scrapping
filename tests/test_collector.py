from scraper.collector import ResultCollector


def test_grid_and_post_page_merge_by_shortcode(fixture):
    collector = ResultCollector("testuser")
    for blob in fixture("polaris_profile_page.json"):
        collector.feed(blob)
    pages = fixture("polaris_post_pages.json")
    assert collector.feed(pages["EEE555"], allow_new=False) == 0

    post = next(p for p in collector.posts() if p.shortcode == "EEE555")
    assert post.timestamp == "2025-10-02T10:13:20+00:00"
    assert (post.likes, post.comments) == (77, 3)
    assert post.image_url == "https://cdn.example/eee555-640.jpg"


def test_allow_new_false_keeps_related_posts_out(fixture):
    collector = ResultCollector("testuser")
    collector.feed(fixture("polaris_profile_page.json")[1])
    collector.feed(fixture("polaris_post_pages.json")["EEE555"], allow_new=False)
    assert "HHH888" not in {p.shortcode for p in collector.posts()}
    assert collector.seen == 3


def test_same_post_in_two_formats_is_deduplicated(fixture):
    collector = ResultCollector("testuser")
    collector.feed(fixture("legacy_web_profile_info.json"))
    v1_copy = {
        "items": [
            {
                "id": "9001_1001",
                "pk": "9001",
                "code": "AAA111",
                "media_type": 1,
                "taken_at": 1759320000,
                "comment_count": 6,
                "image_versions2": {"candidates": [{"url": "https://cdn.example/x.jpg"}]},
                "user": {"pk": "1001", "username": "testuser"},
            }
        ]
    }
    assert collector.feed(v1_copy) == 0
    assert [p.shortcode for p in collector.posts()].count("AAA111") == 1


def test_timeline_posts_kept_including_collabs_newest_first(fixture):
    collector = ResultCollector("testuser")
    collector.feed(fixture("legacy_web_profile_info.json"))
    collector.feed(fixture("v1_timeline.json"))

    codes = [p.shortcode for p in collector.posts()]
    assert codes == ["AAA111", "BBB222", "CCC333", "DDD444", "ZZZ999"]


def test_other_owners_outside_the_timeline_are_dropped(fixture):
    collector = ResultCollector("testuser")
    page = fixture("polaris_post_pages.json")["EEE555"]
    collector.feed(page)
    assert {p.shortcode for p in collector.posts()} == {"EEE555"}
    assert collector.seen == 2


def test_owner_id_only_posts_resolved_once_profile_id_known():
    collector = ResultCollector("testuser")
    collector.feed(
        {
            "edges": [
                {
                    "node": {
                        "id": "1",
                        "shortcode": "MINE",
                        "display_url": "u",
                        "owner": {"id": "1001"},
                    }
                },
                {
                    "node": {
                        "id": "2",
                        "shortcode": "THEIRS",
                        "display_url": "u",
                        "owner": {"id": "2002"},
                    }
                },
            ]
        }
    )
    assert {p.shortcode for p in collector.posts()} == {"MINE", "THEIRS"}

    collector.feed({"user": {"username": "testuser", "id": "1001", "biography": ""}})
    assert {p.shortcode for p in collector.posts()} == {"MINE"}


def test_empty_bio_is_known_empty_not_unknown():
    collector = ResultCollector("testuser")
    collector.feed({"user": {"username": "testuser", "biography": "", "follower_count": 5}})
    assert collector.profile.bio == ""


def test_profile_fields_fill_but_do_not_overwrite(fixture):
    collector = ResultCollector("testuser")
    collector.feed({"user": {"username": "testuser", "follower_count": 1, "full_name": "First"}})
    collector.feed(fixture("legacy_web_profile_info.json"))
    assert collector.profile.followers == 1
    assert collector.profile.full_name == "First"
    assert collector.profile.bio == "Coffee & code\nBerlin"
