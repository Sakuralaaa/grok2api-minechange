"""Grok registration step orchestration using Playwright."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.dataplane.register.automation.browser import BrowserManager
from app.dataplane.register.automation.turnstile import (
    resolve_turnstile_with_flaresolverr,
)

_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F"
_GROK_SIGNUP_URL = "https://grok.com/i/flow/signup"
_GROK_URL = "https://grok.com"
_MAX_RETRIES_TURNSTILE = 2
_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "cloudflare",
    "cf-challenge",
    "turnstile",
)
_HOME_SIGNUP_BUTTON = re.compile(r"^(sign up|register|注册|创建账户|创建账号)$", re.I)
_EMAIL_SIGNUP_BUTTON = re.compile(
    r"(email|e-mail|邮箱|邮件).*(sign up|register|注册|continue|继续|use|使用)"
    r"|"
    r"(sign up|register|注册|continue|继续|use|使用).*(email|e-mail|邮箱|邮件)",
    re.I,
)
_SUBMIT_BUTTON = re.compile(
    r"^(sign up|register|continue|next|create account|注册|继续|下一步|创建账户|创建账号|验证|提交)$",
    re.I,
)
_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email' i]",
    "input#email",
    "input[id*='email' i]",
    "input[autocomplete='email']",
    "input[inputmode='email']",
    "input[placeholder*='email' i]",
    "input[placeholder*='邮箱']",
    "input[aria-label*='email' i]",
    "input[aria-label*='邮箱']",
]


async def _click_named_button(page: Any, pattern: re.Pattern[str], *, timeout: int = 2500) -> bool:
    """Click the first visible button/link matching a localized label."""
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=pattern).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            await page.wait_for_timeout(1200)
            return True
        except Exception:
            continue
    return False


async def _first_visible_input(page: Any, selectors: list[str]) -> Any | None:
    """Return the first visible input matching any selector."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 5)):
                item = locator.nth(index)
                if await item.is_visible():
                    return item
        except Exception:
            continue
    return None


async def _page_mentions_email_signup(page: Any) -> bool:
    """Best-effort guard so generic textbox fallback does not fill the Grok chat box."""
    try:
        if "accounts.x.ai" in page.url.lower():
            return True
        body_text = (await page.inner_text("body")).lower()
        return any(text in body_text for text in ("email", "e-mail", "邮箱", "邮件"))
    except Exception:
        return False


async def _find_email_input(page: Any, *, allow_generic_textbox: bool = False) -> Any | None:
    """Find the visible email textbox on the current signup step."""
    email_input = await _first_visible_input(page, _EMAIL_INPUT_SELECTORS)
    if email_input:
        return email_input

    if allow_generic_textbox and await _page_mentions_email_signup(page):
        try:
            locator = page.get_by_role("textbox").first
            await locator.wait_for(state="visible", timeout=2500)
            return locator
        except Exception:
            return None
    return None


async def _ensure_email_signup_form(page: Any) -> bool:
    """Navigate the current auth UI to the email-signup form."""
    for _ in range(4):
        if await _find_email_input(page, allow_generic_textbox=True):
            return True
        if await _click_named_button(page, _EMAIL_SIGNUP_BUTTON):
            continue
        if await _click_named_button(page, _HOME_SIGNUP_BUTTON):
            continue
        break
    return bool(await _find_email_input(page, allow_generic_textbox=True))


async def _page_has_cloudflare_gate(page: Any) -> bool:
    """Detect whether the current page is still blocked by a Cloudflare gate."""
    try:
        title = (await page.title()).lower()
    except Exception:
        title = ""

    try:
        body_text = ((await page.inner_text("body")) if await page.query_selector("body") else "").lower()
    except Exception:
        body_text = ""

    text = f"{page.url.lower()} {title} {body_text[:4000]}"
    return any(marker in text for marker in _CF_CHALLENGE_MARKERS)


async def step_prepare_signup_clearance(
    *,
    flaresolverr_url: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Resolve the CF challenge that appears before the email signup page."""
    cfg = get_config()
    fs_url = flaresolverr_url or cfg.get_str("proxy.clearance.flaresolverr_url", "")
    if not fs_url:
        return {"ok": True, "cookies": [], "user_agent": "", "target_url": _SIGNUP_URL}

    timeout = cfg.get_int("proxy.clearance.timeout_sec", 60)
    errors: list[str] = []
    for target_url in (_SIGNUP_URL, _GROK_SIGNUP_URL):
        result = await resolve_turnstile_with_flaresolverr(
            target_url,
            flaresolverr_url=fs_url,
            proxy_url=proxy_url,
            timeout=timeout,
        )
        if result.get("ok"):
            result["target_url"] = target_url
            logger.info("registration step: pre-signup CF clearance solved for {}", target_url)
            return result
        errors.append(f"{target_url}: {result.get('error', 'unknown error')}")

    error = "; ".join(errors) if errors else "unknown error"
    logger.warning("registration step: pre-signup CF clearance failed: {}", error)
    return {"ok": False, "error": error, "target_url": _SIGNUP_URL}


async def step_navigate_signup(browser: BrowserManager) -> bool:
    """Navigate to the Grok / x.ai signup page."""
    try:
        page = browser.page
        await page.goto(_SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        if await _page_has_cloudflare_gate(page):
            logger.info("registration step: CF gate detected before email signup page, invoking FlareSolverr")
            if not await step_handle_turnstile(browser, current_url=page.url or _SIGNUP_URL, force=True):
                return False
        if "accounts.x.ai" not in page.url.lower():
            await page.goto(_GROK_SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            if await _page_has_cloudflare_gate(page):
                logger.info("registration step: CF gate detected on Grok signup page, invoking FlareSolverr")
                if not await step_handle_turnstile(browser, current_url=page.url or _GROK_SIGNUP_URL, force=True):
                    return False
        if not await _ensure_email_signup_form(page):
            logger.warning("registration step: email signup form not available after navigation")
            return False
        logger.info("registration step: navigated to email signup page")
        return True
    except Exception as exc:
        logger.warning("registration step: navigate signup failed: {}", exc)
        return False


async def step_fill_email(browser: BrowserManager, email: str) -> bool:
    """Fill the email input field on the signup form."""
    try:
        page = browser.page
        await _ensure_email_signup_form(page)
        email_input = await _find_email_input(page, allow_generic_textbox=True)
        if not email_input:
            logger.error("registration step: no email input found at {} title={}", page.url, await page.title())
            return False

        await email_input.click()
        await email_input.fill(email)
        value = await email_input.evaluate("(el) => el.value || el.textContent || ''")
        if value.strip() != email:
            logger.error("registration step: email input value mismatch at {}", page.url)
            return False
        logger.info("registration step: filled email: {}", email)
        return True
    except Exception as exc:
        logger.warning("registration step: fill email failed: {}", exc)
        return False


async def step_handle_turnstile(
    browser: BrowserManager,
    *,
    flaresolverr_url: str | None = None,
    current_url: str | None = None,
    force: bool = False,
) -> bool:
    """Detect and resolve Cloudflare Turnstile / CF gates via FlareSolverr."""
    cfg = get_config()
    fs_url = flaresolverr_url or cfg.get_str("proxy.clearance.flaresolverr_url", "")
    if not fs_url:
        logger.info("registration step: FlareSolverr not configured, skipping Turnstile handling")
        return True

    page = browser.page
    try:
        turnstile_present = await page.query_selector("iframe[src*='turnstile'], div[class*='turnstile']")
        challenge_present = await _page_has_cloudflare_gate(page)
        if not force and not turnstile_present and not challenge_present:
            logger.info("registration step: no Turnstile or CF gate detected, skipping")
            return True

        target_url = current_url or page.url or _SIGNUP_URL
        logger.info("registration step: CF challenge detected at {}, resolving via FlareSolverr", target_url)
        for attempt in range(1, _MAX_RETRIES_TURNSTILE + 1):
            result = await resolve_turnstile_with_flaresolverr(
                target_url,
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
                if not await _page_has_cloudflare_gate(page):
                    logger.info("registration step: CF challenge resolved and cookies injected")
                    return True
                logger.warning("registration step: CF challenge still present after reload, retrying")
            else:
                logger.warning(
                    "registration step: CF resolve attempt {}/{} failed: {}",
                    attempt, _MAX_RETRIES_TURNSTILE, result.get("error"),
                )
                if attempt < _MAX_RETRIES_TURNSTILE:
                    await asyncio.sleep(3)

        logger.error("registration step: CF challenge resolution failed after {} attempts", _MAX_RETRIES_TURNSTILE)
        return False
    except Exception as exc:
        logger.warning("registration step: Turnstile handling failed: {}", exc)
        return False


async def step_submit_form(browser: BrowserManager) -> bool:
    """Submit the signup form."""
    try:
        page = browser.page
        if await _click_named_button(page, _SUBMIT_BUTTON):
            logger.info("registration step: form submitted")
            return True

        selectors = [
            "button[type='submit']",
            "button:has-text('Continue')",
            "button:has-text('Sign Up')",
            "button:has-text('Register')",
            "button:has-text('Next')",
            "button:has-text('注册')",
            "button:has-text('继续')",
            "button:has-text('下一步')",
            "button:has-text('验证')",
            "button:has-text('提交')",
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


async def step_fill_verification_code(browser: BrowserManager, code: str) -> bool:
    """Fill an email verification code and submit it."""
    try:
        page = browser.page
        selectors = [
            "input[name='code']",
            "input[name='verification_code']",
            "input[autocomplete='one-time-code']",
            "input[inputmode='numeric']",
            "input[type='tel']",
            "input[type='text']",
        ]
        code_input = None
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                code_input = el
                break
        if not code_input:
            inputs = await page.query_selector_all("input:visible")
            code_input = inputs[0] if inputs else None
        if not code_input:
            logger.error("registration step: no verification code input found")
            return False

        await code_input.click()
        await code_input.fill(code)
        await page.wait_for_timeout(500)
        await step_submit_form(browser)
        logger.info("registration step: verification code submitted")
        return True
    except Exception as exc:
        logger.warning("registration step: verification code submit failed: {}", exc)
        return False


async def step_wait_verification(browser: BrowserManager, timeout: int = 120) -> bool:
    """Wait for the page to transition to a verification-required state."""
    try:
        page = browser.page
        success_keywords = [
            "verify",
            "verification",
            "check your email",
            "verification sent",
            "confirm",
            "code",
            "验证码",
            "验证",
            "检查你的邮箱",
            "检查您的邮箱",
            "确认",
        ]
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
