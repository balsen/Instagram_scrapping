from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from .config import ScraperConfig
from .errors import AccessBlocked, InvalidUsername, ProfileNotFound, ScraperError
from .exporter import export_json
from .instagram import InstagramScraper

logger = logging.getLogger("scraper")

EXIT_CODES = ((InvalidUsername, 2), (ProfileNotFound, 3), (AccessBlocked, 4))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scraper", description="Export an Instagram profile to JSON."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    commands = parser.add_subparsers(dest="command", required=True)

    scrape = commands.add_parser("scrape", help="scrape a profile")
    scrape.add_argument("username", help="username, @username or profile URL")
    scrape.add_argument(
        "-o", "--output", help="output file (default output/<username>.json, '-' = stdout)"
    )
    scrape.add_argument("--max-posts", type=int, help="stop after this many posts")
    scrape.add_argument("--headful", action="store_true", help="show the browser window")
    scrape.add_argument("--storage-state", type=Path, help="logged-in session file")

    login = commands.add_parser("login", help="log in manually and save the session")
    login.add_argument("--state", type=Path, default=Path("ig_state.json"))
    return parser


async def _login(config: ScraperConfig, state_path: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="en-US", user_agent=config.user_agent)
        page = await context.new_page()
        await page.goto(f"{config.base_url}/accounts/login/")
        await asyncio.to_thread(
            input, "Log in in the browser window, then press Enter here to save the session. "
        )
        await context.storage_state(path=str(state_path))
        await browser.close()
    os.chmod(state_path, 0o600)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = ScraperConfig.from_env()
        if args.command == "login":
            asyncio.run(_login(config, args.state))
            logger.info("Session saved to %s; use it with IG_STORAGE_STATE", args.state)
            return 0

        overrides = {}
        if args.max_posts is not None:
            overrides["max_posts"] = args.max_posts
        if args.headful:
            overrides["headless"] = False
        if args.storage_state:
            overrides["storage_state"] = args.storage_state
        config = dataclasses.replace(config, **overrides)

        result = asyncio.run(InstagramScraper(config).scrape(args.username))
    except ScraperError as exc:
        logger.error("%s", exc)
        return next((code for kind, code in EXIT_CODES if isinstance(exc, kind)), 1)
    except KeyboardInterrupt:
        return 130

    data = result.to_dict()
    if args.output == "-":
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        path = Path(args.output or f"output/{result.profile.username}.json")
        logger.info("Wrote %s", export_json(data, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
