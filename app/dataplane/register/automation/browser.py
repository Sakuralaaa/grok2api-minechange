"""Playwright browser manager for Grok registration automation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger

_BROWSER_TYPES = {"chromium", "firefox", "webkit"}


class BrowserManager:
    """Manages a single Playwright Chromium instance for registration.

    Provides start/stop lifecycle, cookie persistence, proxy integration,
    and headless/visible mode switching.
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._running = False
        self._user_data_dir: Path | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def page(self) -> Any:
        return self._page

    @property
    def context(self) -> Any:
        return self._context

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        headless: bool | None = None,
        proxy_url: str | None = None,
        user_data_dir: str | Path | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Start a Playwright Chromium browser.

        Args:
            headless: Run headless. Defaults to config `register.browser.headless`
                      (itself defaulting to True).
            proxy_url: SOCKS/HTTP proxy URL. Falls back to config
                       `proxy.egress.proxy_url`.
            user_data_dir: Persistent context directory for cookies.
            user_agent: Optional user-agent override, used to align with
                        a FlareSolverr-issued clearance bundle.
        """
        if self._running:
            logger.warning("browser manager already started, skipping")
            return

        cfg = get_config()
        if headless is None:
            headless = cfg.get_bool("register.browser.headless", True)

        if proxy_url is None:
            proxy_url = cfg.get_str("register.browser.proxy_url", "") or cfg.get_str("proxy.egress.proxy_url", "")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright is not installed. Run: pip install playwright && playwright install chromium"
            )

        self._playwright = await async_playwright().start()

        launch_options: dict[str, Any] = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        executable_path = (
            os.getenv("REGISTRATION_BROWSER_EXECUTABLE", "").strip()
            or cfg.get_str("register.browser.executable_path", "").strip()
        )
        if executable_path:
            launch_options["executable_path"] = executable_path
        if proxy_url:
            launch_options["proxy"] = {"server": proxy_url}

        # Channel: prefer "msedge" on Windows if available, otherwise default chromium
        if os.name == "nt" and "executable_path" not in launch_options:
            # Try msedge first (many users have it), fall back to bundled chromium
            launch_options["channel"] = cfg.get_str("register.browser.channel", "msedge")

        try:
            self._browser = await self._playwright.chromium.launch(**launch_options)
        except Exception:
            # Fallback to bundled chromium if msedge is not available
            launch_options.pop("channel", None)
            self._browser = await self._playwright.chromium.launch(**launch_options)

        context_options: dict[str, Any] = {
            "user_agent": user_agent or cfg.get_str(
                "register.browser.user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
            ),
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

        if user_data_dir:
            self._user_data_dir = Path(user_data_dir)
            context_options["storage_state"] = str(self._user_data_dir)

        self._context = await self._browser.new_context(**context_options)
        await self._context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
              get: () => undefined,
            });
            Object.defineProperty(navigator, 'languages', {
              get: () => ['en-US', 'en'],
            });
            Object.defineProperty(navigator, 'plugins', {
              get: () => [1, 2, 3, 4, 5],
            });
            window.chrome = window.chrome || { runtime: {} };
            const originalQuery = window.navigator.permissions?.query;
            if (originalQuery) {
              window.navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                  ? Promise.resolve({ state: Notification.permission })
                  : originalQuery(parameters)
              );
            }
            """
        )
        self._page = await self._context.new_page()
        self._running = True
        logger.info(
            "browser manager started: headless={} proxy={} channel={}",
            headless,
            bool(proxy_url),
            launch_options.get("channel") or launch_options.get("executable_path") or "chromium",
        )

    async def stop(self) -> None:
        """Close the browser and release resources."""
        if not self._running:
            return
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning("browser stop error: {}", exc)
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._running = False
        logger.info("browser manager stopped")

    async def save_cookies(self, path: str | Path | None = None) -> None:
        """Persist current browser cookies to a file."""
        if not self._context:
            return
        target = Path(path) if path else (self._user_data_dir or Path("browser_cookies.json"))
        await self._context.storage_state(path=str(target))

    async def load_cookies(self, path: str | Path) -> None:
        """Load cookies from a file into the current context."""
        if not self._context:
            return
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cookies = data.get("cookies", data) if isinstance(data, dict) else data
        await self._context.add_cookies([self._normalize_cookie(cookie) for cookie in cookies])

    async def inject_flaresolverr_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Inject cookies obtained from FlareSolverr into the browser context."""
        if not self._context:
            logger.warning("cannot inject cookies: browser context not available")
            return
        for c in cookies:
            try:
                await self._context.add_cookies([self._normalize_cookie(c)])
            except Exception as exc:
                logger.debug("cookie inject skipped: {}", exc)
        logger.info("injected {} FlareSolverr cookies", len(cookies))

    def _normalize_cookie(self, cookie: dict[str, Any]) -> dict[str, Any]:
        """Keep only Playwright-supported cookie fields."""
        allowed = {
            "name", "value", "url", "domain", "path", "expires",
            "httpOnly", "secure", "sameSite",
        }
        normalized = {k: v for k, v in cookie.items() if k in allowed}
        if "sameSite" in normalized and normalized["sameSite"] not in {"Strict", "Lax", "None"}:
            normalized.pop("sameSite", None)
        return normalized

"""
This module provides a singleton BrowserManager for the registration pipeline.
"""

_manager: BrowserManager | None = None


async def get_browser_manager() -> BrowserManager:
    """Return the singleton BrowserManager."""
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager


async def close_browser_manager() -> None:
    """Shut down the singleton BrowserManager."""
    global _manager
    if _manager is not None:
        await _manager.stop()
        _manager = None
