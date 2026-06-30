"""Grok registration step orchestration using Playwright."""

from __future__ import annotations

import asyncio
from typing import Any

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.dataplane.register.automation.browser import BrowserManager
from app.dataplane.register.automation.turnstile import (
    resolve_turnstile_with_flaresolverr,
    create_flaresolverr_session,
)

_SIGNUP_URL = "https://accounts.x.ai/signup"
_GROK_URL = "https://grok.com"
_MAX_RETRIES_TURNSTILE = 2


async def step_navigate_signup(browser: BrowserManager) -> bool:
    """Navigate to the Grok / x.ai signup page."""
    try:
        page = browser.page
        await page.goto(_SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        logger.info("registration step: navigated to signup page")
        return True
    except Exception as exc:
        logger.warning("registration step: navigate signup failed: {}", exc)
        return False


async def step_fill_email(browser: BrowserManager, email: str) -> bool:
    """Fill the email input field on the signup form."""
    try:
        page = browser.page
        selectors = [
            "input[type='email']",
            "input[name='email']",
            "input#email",
            "input[placeholder*='email']",
            "input[autocomplete='email']",
        ]
        email_input = None
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                email_input = el
                break

        if not email_input:
            inputs = await page.query_selector_all("input:visible")
            if inputs:
                email_input = inputs[0]
            else:
                logger.error("registration step: no email input found")
                return False

        await email_input.click()
        await email_input.fill(email)
        logger.info("registration step: filled email: {}", email)
        return True
    except Exception as exc:
        logger.warning("registration step: fill email failed: {}", exc)
        return False


async def step_handle_turnstile(
    browser: BrowserManager,
    *,
    flaresolverr_url: str | None = None,
) -> bool:
    """Detect and resolve Cloudflare Turnstile via FlareSolverr."""
    cfg = get_config()
    fs_url = flaresolverr_url or cfg.get_str("proxy.clearance.flaresolverr_url", "")
    if not fs_url:
        logger.info("registration step: FlareSolverr not configured, skipping Turnstile handling")
        return True

    page = browser.page
    try:
        turnstile_present = await page.query_selector("iframe[src*='turnstile'], div[class*='turnstile']")
        if not turnstile_present:
            logger.info("registration step: no Turnstile detected, skipping")
            return True

        logger.info("registration step: Turnstile detected, resolving via FlareSolverr")
        current_url = page.url
        for attempt in range(1, _MAX_RETRIES_TURNSTILE + 1):
            result = await resolve_turnstile_with_flaresolverr(
                current_url or _SIGNUP_URL,
                flaresolverr_url=fs_url,
                proxy_url=cfg.get_str("proxy.egress.proxy_url", "") or None,
                timeout=cfg.get_int("proxy.clearance.timeout_sec", 60),
            )
            if result.get("ok"):
                cookies = result.get("cookies", [])
                if cookies:
                    await browser.inject_flaresolverr_cookies(cookies)
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                logger.info("registration step: Turnstile resolved and cookies injected")
                return True
            else:
                logger.warning(
                    "registration step: Turnstile resolve attempt {}/{} failed: {}",
                    attempt, _MAX_RETRIES_TURNSTILE, result.get("error"),
                )
                if attempt < _MAX_RETRIES_TURNSTILE:
                    await asyncio.sleep(3)

        logger.error("registration step: Turnstile resolution failed after {} attempts", _MAX_RETRIES_TURNSTILE)
        return False
    except Exception as exc:
        logger.warning("registration step: Turnstile handling failed: {}", exc)
        return False


async def step_submit_form(browser: BrowserManager) -> bool:
    """Submit the signup form."""
    try:
        page = browser.page
        selectors = [
            "button[type='submit']",
            "button:has-text('Continue')",
            "button:has-text('Sign Up')",
            "button:has-text('Next')",
            "input[type='submit']",
            "button[class*='submit']",
            "button[class*='continue']",
        ]
        btn = None
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                btn = el
                break

        if not btn:
            buttons = await page.query_selector_all("button:visible")
            if buttons:
                btn = buttons[0]
            else:
                logger.error("registration step: no submit button found")
                return False

        await btn.click()
        logger.info("registration step: form submitted")
        return True
    except Exception as exc:
        logger.warning("registration step: form submit failed: {}", exc)
        return False


async def step_wait_verification(browser: BrowserManager, timeout: int = 120) -> bool:
    """Wait for the page to transition to a verification-required state."""
    try:
        page = browser.page
        success_keywords = ["verify", "check your email", "verification sent", "confirm"]
        for _ in range(timeout // 2):
            await page.wait_for_timeout(2000)
            body_text = await page.inner_text("body") if await page.query_selector("body") else ""
            body_lower = body_text.lower()
            if any(kw in body_lower for kw in success_keywords):
                logger.info("registration step: verification page detected")
                return True
            current = page.url.lower()
            if "verify" in current or "check" in current or "confirm" in current:
                logger.info("registration step: verification URL detected")
                return True
        logger.warning("registration step: verification page not detected within timeout")
        return False
    except Exception as exc:
        logger.warning("registration step: wait verification failed: {}", exc)
        return False


async def step_extract_token(browser: BrowserManager) -> str | None:
    """Extract the SSO token from the browser after successful login/signup."""
    try:
        page = browser.page
        # 1. Check cookies
        cookies = await page.context.cookies()
        for c in cookies:
            name_lower = c.get("name", "").lower()
            if name_lower in ("sso", "sso_token", "xai-sso", "token"):
                token = c.get("value", "")
                if token:
                    logger.info("registration step: token extracted from cookie '{}'", c["name"])
                    return token

        # 2. Check localStorage
        for key in ("sso", "sso_token", "xai_token", "token"):
            try:
                val = await page.evaluate(f"localStorage.getItem('{key}')")
                if val:
                    logger.info("registration step: token extracted from localStorage '{}'", key)
                    return val
            except Exception:
                continue

        # 3. Wait briefly and check again
        await page.wait_for_timeout(3000)
        cookies = await page.context.cookies()
        for c in cookies:
            name_lower = c.get("name", "").lower()
            if name_lower in ("sso", "sso_token", "xai-sso", "token"):
                token = c.get("value", "")
                if token:
                    return token

        logger.warning("registration step: no SSO token found in cookies or localStorage")
        return None
    except Exception as exc:
        logger.warning("registration step: token extraction failed: {}", exc)
        return None


async def step_accept_tos_and_nsfw(token: str) -> bool:
    """Accept ToS, set birth date, and enable NSFW for the new account."""
    try:
        from app.dataplane.reverse.protocol.xai_auth import nsfw_sequence
        await nsfw_sequence(token)
        logger.info("registration step: ToS/NSFW sequence completed for token")
        return True
    except UpstreamError as exc:
        logger.warning("registration step: ToS/NSFW sequence failed: {}", exc)
        return False
    except Exception as exc:
        logger.warning("registration step: ToS/NSFW sequence error: {}", exc)
        return False


async def step_navigate_grok(browser: BrowserManager) -> bool:
    """Navigate to grok.com to trigger SSO token generation."""
    try:
        page = browser.page
        await page.goto(_GROK_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        logger.info("registration step: navigated to grok.com for token generation")
        return True
    except Exception as exc:
        logger.warning("registration step: navigate grok.com failed: {}", exc)
        return False
