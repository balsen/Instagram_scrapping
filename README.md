# Instagram Profile Extractor

Enter an Instagram username in a web form, or pass it on the command line, and get a clean, structured JSON file with the profile's metadata and posts.

- **Profile:** full name, follower count, following count, bio
- **Posts:** ID, shortcode, timestamp, caption, like count, comment count, image URL (or the first item of a carousel), video thumbnail

> [!IMPORTANT]
> **About "all posts":** Instagram shows logged-out visitors only a profile's **newest 12 posts**, then asks them to log in. By default the scraper returns those 12, with every required field filled, and marks the result `"complete": false`. A profile with 500 posts will therefore return 12.
>
> To get the full history, the scraper can use a logged-in session (`python -m scraper login`, then `IG_STORAGE_STATE`; see [Logged-in sessions](#logged-in-sessions)). The pagination code behind it is covered by the end-to-end tests, but **I haven't yet run it against a real Instagram account**. All the live results below come from logged-out runs.

## Why Playwright and not a data provider?

In production I would build this on a **third-party data provider** (for example Apify or Bright Data), or on the official Instagram Graph API for accounts we own. Those services handle proxies, rate limits, login walls and Instagram's frequent frontend changes, and they come with an SLA.

For this test I wanted a self-contained solution with no paid services or API keys, so the scraper drives a real Chromium browser with **Playwright** and reads the same JSON Instagram's own web app loads. The trade-off is that **Instagram can block it**: it may rate-limit, show a login wall, or change its internal payloads. The code is built to fail clearly when that happens, never with wrong data, and the parsing layer is isolated so a format change is a small, test-covered fix.

## Quick start

Requires Python 3.10 or newer. The test suite runs on 3.12 and 3.14.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### Web form

```bash
python app.py
```

Open http://127.0.0.1:8000 and enter a username, `@handle` or profile URL. The scrape runs in the background. The page shows live progress, a summary and the JSON, plus a **Download JSON** button. Files are saved to `output/<username>_<UTC timestamp>.json`.

### Command line

```bash
python -m scraper scrape natgeo                  # writes output/natgeo.json
python -m scraper scrape @natgeo -o natgeo.json  # custom path
python -m scraper scrape natgeo -o - | jq .      # stdout
python -m scraper scrape natgeo --max-posts 50 --headful -v
```

Exit codes: `0` ok, `1` other error, `2` invalid username, `3` profile not found, `4` blocked or login required.

### Docker

```bash
docker build -t ig-scraper .
docker run --rm -p 8000:8000 -v "$PWD/output:/app/output" ig-scraper
```

The image is based on the official Playwright image and runs gunicorn as a non-root user, with a `/healthz` health check.

## Output

```json
{
  "username": "natgeo",
  "user_id": "787132",
  "full_name": "National Geographic",
  "bio": "Step into wonder and find your inner explorer with National Geographic 🌎",
  "followers": 268425904,
  "following": 194,
  "post_count": 32000,
  "is_private": false,
  "is_verified": true,
  "posts": [
    {
      "id": "4002692467765081611",
      "shortcode": "DeMa5eeRPYL",
      "url": "https://www.instagram.com/p/DeMa5eeRPYL/",
      "media_type": "image",
      "timestamp": "2026-10-07T13:03:06+00:00",
      "caption": "Presented by @rolex. Around 40,000 tourists visit Bwindi…",
      "likes": 18838,
      "comments": 45,
      "image_url": "https://scontent-….cdninstagram.com/…jpg",
      "video_thumbnail": null
    }
  ],
  "meta": {
    "scraped_at": "2026-10-07T15:48:43+00:00",
    "posts_collected": 12,
    "complete": false,
    "authenticated": false,
    "stats": {
      "duration_seconds": 17.05,
      "requests": 81,
      "failed_requests": 0,
      "blocked_requests": 273,
      "bytes_received": 7501248,
      "payloads_parsed": 34,
      "detail_pages": 12,
      "navigation_retries": 0
    }
  }
}
```

Conventions:

- **`null` means unknown, never zero.** Hidden like counts come out as `null`, not `0`.
- **Media:** `media_type` is `image`, `video` or `carousel`. Images fill `image_url` and videos fill `video_thumbnail`. A carousel fills whichever one matches its first item.
- **Timestamps** are ISO 8601 in UTC. Posts are sorted newest first.
- **`meta.complete`** says whether the whole timeline was collected (see [Limitations](#limitations)).
- **`meta.stats`** records how the run went: duration, requests, bytes received, retries and post pages opened.
- **Contract:** the full output is defined in [`schema/output.schema.json`](schema/output.schema.json) (JSON Schema 2020-12), and every benchmark run is validated against it.

## Extraction approach

```text
 web form ─▶ job queue ─┐
                        ├─▶ Playwright (Chromium)
 CLI ───────────────────┘      │
          ┌────────────────────┼─────────────────────────┐
          ▼                    ▼                         ▼
  embedded <script> JSON   GraphQL / API responses   post pages /p/<code>/
  (first posts, profile)   captured while scrolling  (timestamps & counts)
          └────────────────────┼─────────────────────────┘
                               ▼
                parser: normalize every known payload format
                               ▼
        collector: dedupe by shortcode, merge, filter by owner
                               ▼
                     JSON (atomic write)
```

1. **Read the data Instagram itself loads; don't scrape the HTML.** Class names and layout change constantly. The JSON behind them changes far less often.
2. **Server-rendered data first.** The profile and first posts are embedded in `<script type="application/json">` tags, so they are parsed straight from the page.
3. **Paginate by scrolling.** A response listener captures the GraphQL/API calls the page makes as it loads more posts. Scrolling stops when the feed reports no next page, after several scrolls with no new posts, or at `--max-posts`.
4. **Fill in missing fields.** For logged-out visitors, the profile grid leaves out timestamps and counts. For each post still missing them, the scraper opens its `/p/<shortcode>/` page (two at a time) and merges in the full record. Those pages only fill in known posts; they never add new ones.
5. **Normalize several formats.** `scraper/parser.py` handles legacy GraphQL (`edge_*`), v1/`xdt_api` (`image_versions2`, `carousel_media`) and logged-out `XIGPolaris*` nodes. It picks the highest-resolution image, takes the numeric media ID from `pk`, and recognizes hidden like counts.
6. **Keep only the profile's own posts.** Everything in the profile's timeline is kept, including collaborations led by a partner account. Suggested and related posts from other accounts are dropped. Duplicates are merged by shortcode, which stays the same across formats.
7. **Fallbacks.** OpenGraph tags and the page title fill any profile fields the JSON didn't provide.

## Project structure

```text
├── app.py                 Flask app: form, job pages, JSON API, downloads
├── jobs.py                background job queue (thread pool), result files
├── scraper/
│   ├── __main__.py        CLI (scrape / login)
│   ├── instagram.py       orchestration: navigation, retries, pagination, enrichment
│   ├── browser.py         Playwright lifecycle, session, proxy, resource blocking
│   ├── parser.py          pure payload → model normalization
│   ├── collector.py       merge, dedupe, owner filtering, ordering
│   ├── models.py          output schema (dataclasses)
│   ├── config.py          settings + IG_* environment variables
│   ├── errors.py          typed failures (not found, blocked, rate limited…)
│   └── exporter.py        atomic JSON writer
├── benchmark/             live KPI benchmark → BENCHMARK.md
│   ├── kpis.py            KPI definitions, targets, tolerances
│   ├── ground_truth.py    independent reference values from SEO metadata
│   ├── media.py           CDN checks for image and thumbnail URLs
│   └── report.py          Markdown report
├── schema/                JSON Schema for the output
├── templates/             server-rendered UI
├── tests/                 unit, web and end-to-end tests + payload fixtures
└── Dockerfile
```

## Configuration

All settings are optional environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `IG_STORAGE_STATE` | – | Logged-in session file (see below) |
| `IG_MAX_POSTS` | unlimited | Stop after N posts |
| `IG_MAX_SCROLLS` | `100` | Upper bound on scroll iterations |
| `IG_SCROLL_DELAY` | `1.5` | Seconds between scrolls |
| `IG_STAGNANT_ROUNDS` | `5` | Stop after N scrolls without new posts |
| `IG_ENRICH_POSTS` | `true` | Open post pages to fill timestamps and counts |
| `IG_DETAIL_CONCURRENCY` | `2` | Post pages opened in parallel |
| `IG_NAV_TIMEOUT_MS` / `IG_NAV_RETRIES` | `30000` / `3` | Page-load timeout and retries (exponential backoff) |
| `IG_PROXY` | – | e.g. `http://user:pass@host:port` |
| `IG_HEADLESS` | `true` | Set `false` to watch the browser |
| `IG_BLOCK_MEDIA` | `true` | Skip downloading images, video and fonts |
| `IG_USER_AGENT` | desktop Chrome | Browser user agent |
| `IG_BASE_URL` | `https://www.instagram.com` | Target origin (the tests point it at a local server) |
| `IG_MAX_CONCURRENCY` | `2` | Parallel scrapes in the web app |
| `OUTPUT_DIR`, `LOG_LEVEL`, `HOST`, `PORT` | `output`, `INFO`, `127.0.0.1`, `8000` | Web app settings |

### Logged-in sessions

```bash
python -m scraper login --state ig_state.json   # log in by hand in the opened window
IG_STORAGE_STATE=ig_state.json python -m scraper scrape natgeo
```

The session file holds auth cookies. It is created with `0600` permissions and excluded by `.gitignore`.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                      # 108 tests
pytest -m integration       # only the end-to-end browser tests
ruff check . && ruff format --check .
```

- **Unit tests** run the parser and collector against fixtures shaped like each payload format Instagram serves.
- **End-to-end tests** run the real scraper in Chromium against a **local fake Instagram**: a server-rendered profile, a GraphQL request on scroll, post pages, a private profile, a 404, a soft 404 and a login redirect. They cover the whole browser flow without depending on Instagram or the network.
- **Web tests** cover the Flask app: form and JSON API, job lifecycle, downloads, path traversal, and errors that must not leak internals.
- **Benchmark tests** cover the KPI math, the tolerances and the schema itself.

## Benchmark

`python -m benchmark` runs the scraper against live Instagram and scores it on standard scraping KPIs, each with a target. The full report, with methodology, per-profile and per-field breakdowns and every mismatch, is in **[BENCHMARK.md](BENCHMARK.md)**.

Latest run: 5 live profiles (@natgeo, @nasa, @instagram, @nike, @cristiano) and 2 error cases, logged out, 2026-10-07. **13/13 KPIs met their target.**

| KPI | Result | Target |
|---|---|---|
| Success rate | 100% | ≥ 95% |
| Block / rate-limit rate | 0% | ≤ 5% |
| Schema conformance | 100% | 100% |
| Field completeness | 100% | ≥ 98% |
| Accuracy vs. independent source | 99.2% (123/124) | ≥ 95% |
| Media URL validity | 100% (60/60) | ≥ 98% |
| Duplicate rate | 0% | 0% |
| Coverage of posts visible to the session | 100% | ≥ 95% |
| Latency per profile, p95 | 18.8 s | ≤ 60 s |
| Throughput | 40.7 posts/min | ≥ 20 posts/min |
| Data transferred per profile | 7.5 MB | ≤ 25 MB |

**How accuracy is measured.** The scraper reads Instagram's JSON payloads. The benchmark opens the same profiles and posts in a fresh browser and reads Instagram's server-rendered SEO metadata, a separate source, for followers, following, likes, comments, dates, captions, names and bios. The only mismatch was @nasa's following count: 90 in Instagram's JSON against 94 in its SEO tag. That disagreement is inside Instagram's own data. The scraper reports the JSON value, and the benchmark lists it as a mismatch rather than widening the tolerance to hide it.

**Bugs the benchmark found, all now fixed and covered by tests:**

- **Missing post data.** For logged-out visitors, the profile grid leaves out timestamps, likes and comments. The scraper now fills them in from each post's own page.
- **Collab posts dropped.** On @nasa, 9 of 12 posts were collaborations whose primary author is another account, and an owner check discarded them.
- **Missing profiles looked like successes.** Instagram serves a normal page saying "Profile isn't available", which produced an empty result. That page is now detected, and a page with no profile data is always reported as an error.
- **Empty bios reported as unknown.** @cristiano has no bio, which came out as `null` instead of `""`.

**Not yet verified:** collecting the full history with a logged-in session (`IG_STORAGE_STATE`), and the Docker image build. Logged out, Instagram exposes the newest 12 posts, which is why full-timeline coverage is 0.1% and the target is set on visible posts.

## Limitations

- **Logged-out visitors see only the latest ~12 posts.** Instagram shows anonymous visitors that many and then requires login to load more. The scraper then returns what it collected with `meta.complete: false` and logs a warning. Pass a logged-in session (`IG_STORAGE_STATE`) to collect the full history.
- **Private profiles** return profile metadata with an empty `posts` list. This is covered by the end-to-end tests but not yet by the live benchmark.
- **Instagram can block, rate-limit or change its formats at any time.** Expect occasional maintenance, or use a data provider for production (see above).
- **No bypassing.** The scraper does not solve CAPTCHAs, rotate identities or get around access controls. Use it in line with Instagram's terms and applicable law.
- **Web app job state lives in memory.** Run a single process. To scale out, move jobs to a real queue such as RQ or Celery.
