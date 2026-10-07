from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from .config import ScraperConfig
from .errors import ConfigurationError

BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


@dataclass(slots=True)
class NetworkStats:
    requests: int = 0
    failed: int = 0
    blocked: int = 0
    bytes_received: int = 0


def _proxy_settings(proxy: str) -> dict[str, str]:
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not parsed.hostname:
        raise ConfigurationError(f"Invalid proxy URL: {proxy!r}")
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    settings = {"server": server}
    if parsed.username:
        settings["username"] = parsed.username
    if parsed.password:
        settings["password"] = parsed.password
    return settings


class BrowserManager:
    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self.authenticated = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.network = NetworkStats()
        self._size_tasks: set[asyncio.Task] = set()

    async def __aenter__(self) -> BrowserManager:
        config = self.config
        context_options: dict = {
            "viewport": {"width": 1440, "height": 1000},
            "locale": "en-US",
            "user_agent": config.user_agent,
        }
        if config.storage_state:
            if not config.storage_state.is_file():
                raise ConfigurationError(
                    f"Session file not found: {config.storage_state} (IG_STORAGE_STATE)"
                )
            context_options["storage_state"] = str(config.storage_state)

        launch_options: dict = {"headless": config.headless}
        if config.proxy:
            launch_options["proxy"] = _proxy_settings(config.proxy)

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(**launch_options)
            self._context = await self._browser.new_context(**context_options)
            self._context.on("requestfinished", self._on_request_finished)
            self._context.on("requestfailed", self._on_request_failed)
            if config.block_media:
                await self._context.route("**/*", self._block_heavy_resources)
        except BaseException:
            await self.close()
            raise

        self.authenticated = config.storage_state is not None
        return self

    async def new_page(self) -> Page:
        if not self._context:
            raise RuntimeError("Browser context not initialized.")
        return await self._context.new_page()

    async def _block_heavy_resources(self, route: Route) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            self.network.blocked += 1
            await route.abort()
        else:
            await route.continue_()

    def _on_request_finished(self, request: Request) -> None:
        self.network.requests += 1
        task = asyncio.create_task(self._record_size(request))
        self._size_tasks.add(task)
        task.add_done_callback(self._size_tasks.discard)

    def _on_request_failed(self, request: Request) -> None:
        if request.resource_type not in BLOCKED_RESOURCE_TYPES or not self.config.block_media:
            self.network.failed += 1

    async def _record_size(self, request: Request) -> None:
        try:
            sizes = await request.sizes()
        except PlaywrightError:
            return
        self.network.bytes_received += max(sizes["responseBodySize"], 0) + max(
            sizes["responseHeadersSize"], 0
        )

    async def close(self) -> None:
        if self._size_tasks:
            await asyncio.wait(set(self._size_tasks), timeout=5)
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
