from __future__ import annotations

import asyncio

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright


async def check_media_urls(urls: list[str], user_agent: str, concurrency: int = 8) -> list[bool]:
    if not urls:
        return []
    semaphore = asyncio.Semaphore(concurrency)

    async with async_playwright() as playwright:
        context = await playwright.request.new_context(user_agent=user_agent)

        async def check(url: str) -> bool:
            async with semaphore:
                try:
                    response = await context.get(
                        url, headers={"Range": "bytes=0-1023"}, timeout=15_000
                    )
                except PlaywrightError:
                    return False
                content_type = response.headers.get("content-type", "")
                return response.status in (200, 206) and content_type.startswith("image/")

        try:
            return list(await asyncio.gather(*(check(url) for url in urls)))
        finally:
            await context.dispose()
