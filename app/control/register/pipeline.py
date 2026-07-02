"""Registration pipeline orchestration.

Chains: FlareSolverr clearance -> Playwright signup -> generate email ->
submit registration -> wait for verification email -> extract token ->
post-process (ToS/NSFW) -> import.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.control.account.commands import AccountUpsert
from app.dataplane.register.automation.browser import BrowserManager, get_browser_manager
from app.dataplane.register.automation.drission_runner import DrissionRegistrationRunner
from app.dataplane.register.automation import steps as reg_steps
from app.dataplane.register.email.cloudflare_temp import (
    CloudflareTempEmailProvider,
    create_email_provider,
)
from .progress import PipelineEvent, PipelineProgress


@dataclass
class RegistrationOptions:
    """Configuration for a single registration run."""
    count: int = 1
    pool: str = "basic"
    tags: list[str] = field(default_factory=lambda: ["auto-register"])
    headless: bool = False


class RegistrationPipeline:
    """Orchestrates the full registration pipeline for one or more accounts."""

    def __init__(self) -> None:
        self._browser: BrowserManager | None = None
        self._email_provider: CloudflareTempEmailProvider | None = None
        self._progress = PipelineProgress()
        self._running = False
        self._stop_requested = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def progress(self) -> PipelineProgress:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize email provider from config."""
        self._email_provider = None
        cfg = get_config()
        mail_cfg = cfg.get("register.mail", {})
        if isinstance(mail_cfg, dict):
            providers = mail_cfg.get("providers", [])
            if providers:
                for p in providers:
                    if isinstance(p, dict) and p.get("type") == "cloudflare_temp_email" and p.get("enable", True):
                        self._email_provider = create_email_provider(p)
                        if self._email_provider:
                            logger.info("registration pipeline: email provider initialized")
                            break

    async def run_batch(
        self,
        options: RegistrationOptions | None = None,
        *,
        on_event: Callable[[PipelineEvent], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a batch of registrations.

        Returns a list of result dicts, one per account attempt.
        """
        opts = options or RegistrationOptions()
        self._running = True
        self._stop_requested = False
        self._progress.reset()

        if on_event:
            self._progress.on_event = on_event

        results: list[dict[str, Any]] = []
        self._progress.emit(PipelineEvent("batch_start", {"count": opts.count, "pool": opts.pool}))

        for i in range(opts.count):
            if self._stop_requested:
                self._progress.emit(PipelineEvent("batch_stop", {"reason": "user requested stop"}))
                break

            result = await self._register_one(opts, index=i + 1, total=opts.count)
            results.append(result)

            # Small delay between registrations to avoid rate limiting
            if i < opts.count - 1 and not self._stop_requested:
                await asyncio.sleep(5)

        self._progress.emit(PipelineEvent("batch_done", {"total": len(results), "success": sum(1 for r in results if r.get("success"))}))
        self._running = False
        return results

    async def stop(self) -> None:
        """Request graceful stop of the current registration batch."""
        self._stop_requested = True
        self._progress.emit(PipelineEvent("stop_requested", {}))

    async def shutdown(self) -> None:
        """Clean up all resources."""
        self._stop_requested = True
        self._running = False
        if self._browser:
            await self._browser.stop()
            self._browser = None

    # ------------------------------------------------------------------
    # Single registration
    # ------------------------------------------------------------------

    async def _register_one(self, opts: RegistrationOptions, index: int, total: int) -> dict[str, Any]:
        """Run a single registration flow."""
        cfg = get_config()
        engine = cfg.get_str("register.browser.engine", "drission").strip().lower()
        if engine == "drission":
            return await self._register_one_drission(opts, index=index, total=total)

        step_results: dict[str, bool | str | None] = {}
        email: str | None = None
        token: str | None = None
        error: str | None = None
        clearance_bundle: dict[str, Any] | None = None

        self._progress.emit(PipelineEvent("step", {"step": "init", "message": f"Starting registration {index}/{total}", "index": index}))

        try:
            cfg = get_config()
            proxy_url = cfg.get_str("proxy.egress.proxy_url", "") or None
            fs_url = cfg.get_str("proxy.clearance.flaresolverr_url", "") or None

            # --- Step 1: Clear the CF gate before opening the email signup page ---
            self._progress.emit(PipelineEvent("step", {"step": "clearance", "message": "Resolving Cloudflare challenge before signup..."}))
            clearance_bundle = await reg_steps.step_prepare_signup_clearance(
                flaresolverr_url=fs_url,
                proxy_url=proxy_url,
            )
            if not clearance_bundle.get("ok"):
                step_results["clearance"] = False
                error = f"Cloudflare clearance failed: {clearance_bundle.get('error', 'unknown error')}"
                self._progress.emit(PipelineEvent("error", {"step": "clearance", "error": error}))
                return {"success": False, "email": None, "token": None, "error": error, "steps": step_results}
            step_results["clearance"] = True

            # --- Step 2: Start browser ---
            self._progress.emit(PipelineEvent("step", {"step": "start_browser", "message": "Starting browser..."}))
            try:
                self._browser = await get_browser_manager()
                if not self._browser.is_running:
                    await self._browser.start(
                        headless=opts.headless,
                        proxy_url=proxy_url,
                        user_agent=(clearance_bundle or {}).get("user_agent") or None,
                    )
                cookies = (clearance_bundle or {}).get("cookies", [])
                if cookies:
                    await self._browser.inject_flaresolverr_cookies(cookies)
                step_results["start_browser"] = True
            except Exception as exc:
                step_results["start_browser"] = False
                error = f"Browser start failed: {exc}"
                self._progress.emit(PipelineEvent("error", {"step": "start_browser", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}

            # --- Step 3: Navigate to signup ---
            self._progress.emit(PipelineEvent("step", {"step": "navigate", "message": "Navigating to email signup page..."}))
            if not await reg_steps.step_navigate_signup(
                self._browser,
                preferred_url=(clearance_bundle or {}).get("target_url"),
            ):
                step_results["navigate"] = False
                error = "Navigation to email signup page failed"
                self._progress.emit(PipelineEvent("error", {"step": "navigate", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["navigate"] = True

            # --- Step 4: Create email only after the CF gate is cleared ---
            self._progress.emit(PipelineEvent("step", {"step": "create_email", "message": "Creating temporary email..."}))
            if self._email_provider:
                try:
                    email = await self._email_provider.create_email()
                    step_results["create_email"] = True
                except Exception as exc:
                    step_results["create_email"] = False
                    error = f"Email creation failed: {exc}"
                    self._progress.emit(PipelineEvent("error", {"step": "create_email", "error": error}))
                    return {"success": False, "email": None, "token": None, "error": error, "steps": step_results}
            else:
                error = "No email provider configured"
                step_results["create_email"] = False
                self._progress.emit(PipelineEvent("error", {"step": "create_email", "error": error}))
                return {"success": False, "email": None, "token": None, "error": error, "steps": step_results}

            # --- Step 5: Fill email ---
            self._progress.emit(PipelineEvent("step", {"step": "fill_email", "message": f"Filling email: {email}..."}))
            if not await reg_steps.step_fill_email(self._browser, email):
                step_results["fill_email"] = False
                error = "Failed to fill email field"
                self._progress.emit(PipelineEvent("error", {"step": "fill_email", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["fill_email"] = True

            # --- Step 6: Handle any inline Turnstile challenge via FlareSolverr ---
            self._progress.emit(PipelineEvent("step", {"step": "turnstile", "message": "Resolving Turnstile challenge..."}))
            if not await reg_steps.step_handle_turnstile(self._browser):
                step_results["turnstile"] = False
                error = "Turnstile resolution failed"
                self._progress.emit(PipelineEvent("error", {"step": "turnstile", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["turnstile"] = True

            # FlareSolverr may navigate/reload the page while solving the challenge.
            # Refill the email field so submit runs against the live post-challenge form state.
            self._progress.emit(PipelineEvent("step", {"step": "fill_email_retry", "message": f"Re-filling email after challenge: {email}..."}))
            if not await reg_steps.step_fill_email(self._browser, email):
                step_results["fill_email_retry"] = False
                error = "Failed to refill email field after challenge"
                self._progress.emit(PipelineEvent("error", {"step": "fill_email_retry", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["fill_email_retry"] = True

            # --- Step 7: Submit form ---
            self._progress.emit(PipelineEvent("step", {"step": "submit", "message": "Submitting registration form..."}))
            if not await reg_steps.step_submit_form(self._browser):
                step_results["submit"] = False
                error = "Form submission failed"
                self._progress.emit(PipelineEvent("error", {"step": "submit", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["submit"] = True

            # --- Step 8: Wait for verification page ---
            self._progress.emit(PipelineEvent("step", {"step": "wait_verification_page", "message": "Waiting for verification page..."}))
            verification_page_ready = await reg_steps.step_wait_verification(self._browser, timeout=20)
            if not verification_page_ready:
                # Some flows surface Turnstile or validation only after the first submit.
                self._progress.emit(PipelineEvent("step", {"step": "turnstile_retry", "message": "Verification page not reached, retrying challenge/submit..."}))
                await reg_steps.step_handle_turnstile(self._browser)
                self._progress.emit(PipelineEvent("step", {"step": "fill_email_retry_after_submit", "message": f"Re-filling email after submit retry: {email}..."}))
                if not await reg_steps.step_fill_email(self._browser, email):
                    step_results["fill_email_retry_after_submit"] = False
                    error = "Failed to refill email field after submit retry"
                    self._progress.emit(PipelineEvent("error", {"step": "fill_email_retry_after_submit", "error": error}))
                    return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
                step_results["fill_email_retry_after_submit"] = True
                if not await reg_steps.step_submit_form(self._browser):
                    step_results["wait_verification_page"] = False
                    error = "Verification page not reached after submit retry"
                    self._progress.emit(PipelineEvent("error", {"step": "wait_verification_page", "error": error}))
                    return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
                verification_page_ready = await reg_steps.step_wait_verification(self._browser, timeout=15)

            if not verification_page_ready:
                step_results["wait_verification_page"] = False
                page_state = await reg_steps.step_describe_page(self._browser)
                error = f"Verification page not reached after submit; {page_state}"
                self._progress.emit(PipelineEvent("error", {"step": "wait_verification_page", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["wait_verification_page"] = True

            # --- Step 9: Poll for verification email ---
            self._progress.emit(PipelineEvent("step", {"step": "wait_email", "message": "Waiting for verification email..."}))
            if self._email_provider:
                mail_cfg = cfg.get("register.mail", {})
                wait_timeout = float(mail_cfg.get("wait_timeout", 120)) if isinstance(mail_cfg, dict) else 120.0
                wait_interval = float(mail_cfg.get("wait_interval", 2)) if isinstance(mail_cfg, dict) else 2.0
                try:
                    if hasattr(self._email_provider, "wait_for_verification"):
                        verification = await self._email_provider.wait_for_verification(
                            email, timeout=wait_timeout, interval=wait_interval
                        )
                    else:
                        verification = {
                            "link": await self._email_provider.wait_for_verification_link(
                                email, timeout=wait_timeout, interval=wait_interval
                            ),
                            "code": None,
                        }
                    verify_link = verification.get("link")
                    verify_code = verification.get("code")
                    step_results["wait_email"] = bool(verify_link or verify_code)
                    if verify_link:
                        self._progress.emit(PipelineEvent("step", {"step": "verify_link", "message": "Opening verification link..."}))
                        await self._browser.page.goto(verify_link, wait_until="domcontentloaded", timeout=30000)
                        await self._browser.page.wait_for_timeout(3000)
                    elif verify_code:
                        self._progress.emit(PipelineEvent("step", {"step": "verify_code", "message": "Submitting verification code..."}))
                        if not await reg_steps.step_fill_verification_code(self._browser, verify_code):
                            error = "Verification code submission failed"
                            self._progress.emit(PipelineEvent("error", {"step": "verify_code", "error": error}))
                            return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
                    else:
                        error = "Verification email not received or no link/code found"
                        self._progress.emit(PipelineEvent("error", {"step": "wait_email", "error": error}))
                        return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
                except Exception as exc:
                    step_results["wait_email"] = False
                    error = f"Email verification failed: {exc}"
                    self._progress.emit(PipelineEvent("error", {"step": "wait_email", "error": error}))
                    return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}

            # --- Step 10: Navigate to grok.com to trigger token ---
            self._progress.emit(PipelineEvent("step", {"step": "grok_login", "message": "Logging into Grok..."}))
            await reg_steps.step_navigate_grok(self._browser)

            # --- Step 11: Extract SSO token ---
            self._progress.emit(PipelineEvent("step", {"step": "extract_token", "message": "Extracting SSO token..."}))
            token = await reg_steps.step_extract_token(self._browser)
            if not token:
                step_results["extract_token"] = False
                error = "SSO token not found after registration"
                self._progress.emit(PipelineEvent("error", {"step": "extract_token", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["extract_token"] = True

            # --- Step 12: Import token into account pool ---
            self._progress.emit(PipelineEvent("step", {"step": "import", "message": "Importing account into pool..."}))
            try:
                from app.control.account.backends.factory import create_repository
                repo = create_repository()
                try:
                    await repo.initialize()
                    await repo.upsert_accounts([
                        AccountUpsert(token=token, pool=opts.pool, tags=opts.tags)
                    ])
                finally:
                    await repo.close()
                from app.dataplane.account import get_account_directory
                directory = await get_account_directory()
                await directory.sync_if_changed()
                step_results["import"] = True
            except Exception as exc:
                step_results["import"] = False
                logger.warning("registration step: account import failed (token already saved): {}", exc)

            # --- Step 13: Accept ToS, set birth date, enable NSFW ---
            self._progress.emit(PipelineEvent("step", {"step": "tos_nsfw", "message": "Configuring account (ToS/NSFW)..."}))
            nsfw_ok = await reg_steps.step_accept_tos_and_nsfw(token)
            step_results["tos_nsfw"] = nsfw_ok

            self._progress.emit(PipelineEvent("account_done", {"success": True, "email": email, "token_prefix": token[:10]}))
            return {"success": True, "email": email, "token": token, "error": None, "steps": step_results}

        except Exception as exc:
            error = str(exc)
            self._progress.emit(PipelineEvent("error", {"step": "unknown", "error": error}))
            return {"success": False, "email": email, "token": token, "error": error, "steps": step_results}

    async def _register_one_drission(self, opts: RegistrationOptions, index: int, total: int) -> dict[str, Any]:
        """Run a single registration flow through DrissionPage."""
        step_results: dict[str, bool | str | None] = {}
        email: str | None = None
        token: str | None = None
        error: str | None = None
        runner = DrissionRegistrationRunner()

        self._progress.emit(PipelineEvent("step", {"step": "init", "message": f"Starting registration {index}/{total}", "index": index}))

        try:
            cfg = get_config()
            proxy_url = cfg.get_str("register.browser.proxy_url", "") or cfg.get_str("proxy.egress.proxy_url", "") or None
            executable_path = cfg.get_str("register.browser.executable_path", "") or None
            browser_channel = cfg.get_str("register.browser.channel", "msedge")
            effective_headless = False
            if opts.headless:
                logger.info("drission registration: forcing headed browser because Cloudflare blocks headless sessions")

            # --- Step 1: Prepare a real browser session that can pass CF/Turnstile ---
            self._progress.emit(PipelineEvent("step", {"step": "clearance", "message": "Preparing browser session for Cloudflare challenges..."}))
            step_results["clearance"] = True

            # --- Step 2: Start browser ---
            self._progress.emit(PipelineEvent("step", {"step": "start_browser", "message": "Starting browser..."}))
            await asyncio.to_thread(
                runner.start,
                headless=effective_headless,
                proxy_url=proxy_url,
                executable_path=executable_path,
                browser_channel=browser_channel,
            )
            step_results["start_browser"] = True

            # --- Step 3: Open signup page ---
            self._progress.emit(PipelineEvent("step", {"step": "navigate", "message": "Navigating to email signup page..."}))
            await asyncio.to_thread(runner.open_signup_page)
            step_results["navigate"] = True

            # --- Step 4: Create email ---
            self._progress.emit(PipelineEvent("step", {"step": "create_email", "message": "Creating temporary email..."}))
            if self._email_provider:
                email = await self._email_provider.create_email()
                step_results["create_email"] = True
            else:
                error = "No email provider configured"
                step_results["create_email"] = False
                self._progress.emit(PipelineEvent("error", {"step": "create_email", "error": error}))
                return {"success": False, "email": None, "token": None, "error": error, "steps": step_results}

            # --- Step 5: Fill email and submit ---
            self._progress.emit(PipelineEvent("step", {"step": "fill_email", "message": f"Filling email: {email}..."}))
            await asyncio.to_thread(runner.fill_email_and_submit, email)
            step_results["fill_email"] = True

            # --- Step 6: Wait for verification prompt ---
            self._progress.emit(PipelineEvent("step", {"step": "wait_verification_page", "message": "Waiting for verification page..."}))
            verification_prompt_ready = await asyncio.to_thread(runner.wait_for_verification_prompt, 25.0)
            if not verification_prompt_ready:
                step_results["wait_verification_page"] = False
                page_state = await asyncio.to_thread(runner.describe_page)
                error = f"Verification page not reached after submit; {page_state}"
                self._progress.emit(PipelineEvent("error", {"step": "wait_verification_page", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["wait_verification_page"] = True

            # --- Step 7: Poll for verification email ---
            self._progress.emit(PipelineEvent("step", {"step": "wait_email", "message": "Waiting for verification email..."}))
            mail_cfg = cfg.get("register.mail", {})
            wait_timeout = float(mail_cfg.get("wait_timeout", 120)) if isinstance(mail_cfg, dict) else 120.0
            wait_interval = float(mail_cfg.get("wait_interval", 2)) if isinstance(mail_cfg, dict) else 2.0
            verification = {"link": None, "code": None}
            if self._email_provider:
                verification = await self._email_provider.wait_for_verification(
                    email,
                    timeout=wait_timeout,
                    interval=wait_interval,
                )
            verify_link = verification.get("link")
            verify_code = verification.get("code")
            step_results["wait_email"] = bool(verify_link or verify_code)
            if not (verify_link or verify_code):
                error = "Verification email not received or no link/code found"
                self._progress.emit(PipelineEvent("error", {"step": "wait_email", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}

            # --- Step 8: Complete the verification step ---
            if verify_link:
                self._progress.emit(PipelineEvent("step", {"step": "verify_link", "message": "Opening verification link..."}))
                await asyncio.to_thread(runner.open_verification_link, verify_link)
                step_results["verify_link"] = True
            if verify_code:
                self._progress.emit(PipelineEvent("step", {"step": "verify_code", "message": "Submitting verification code..."}))
                await asyncio.to_thread(runner.fill_code_and_submit, verify_code)
                step_results["verify_code"] = True

            # --- Step 9: Complete the profile form + second Turnstile ---
            self._progress.emit(PipelineEvent("step", {"step": "complete_signup", "message": "Completing signup profile and challenge..."}))
            await asyncio.to_thread(runner.fill_profile_and_submit)
            step_results["complete_signup"] = True

            # --- Step 10: Wait for SSO cookie ---
            self._progress.emit(PipelineEvent("step", {"step": "extract_token", "message": "Extracting SSO token..."}))
            token = await asyncio.to_thread(runner.wait_for_sso_cookie)
            if not token:
                step_results["extract_token"] = False
                error = "SSO token not found after registration"
                self._progress.emit(PipelineEvent("error", {"step": "extract_token", "error": error}))
                return {"success": False, "email": email, "token": None, "error": error, "steps": step_results}
            step_results["extract_token"] = True

            # --- Step 11: Import token into account pool ---
            self._progress.emit(PipelineEvent("step", {"step": "import", "message": "Importing account into pool..."}))
            try:
                from app.control.account.backends.factory import create_repository
                repo = create_repository()
                try:
                    await repo.initialize()
                    await repo.upsert_accounts([
                        AccountUpsert(token=token, pool=opts.pool, tags=opts.tags)
                    ])
                finally:
                    await repo.close()
                from app.dataplane.account import get_account_directory
                directory = await get_account_directory()
                await directory.sync_if_changed()
                step_results["import"] = True
            except Exception as exc:
                step_results["import"] = False
                logger.warning("registration step: account import failed (token already saved): {}", exc)

            # --- Step 12: Accept ToS and enable NSFW ---
            self._progress.emit(PipelineEvent("step", {"step": "tos_nsfw", "message": "Configuring account (ToS/NSFW)..."}))
            nsfw_ok = await reg_steps.step_accept_tos_and_nsfw(token)
            step_results["tos_nsfw"] = nsfw_ok

            self._progress.emit(PipelineEvent("account_done", {"success": True, "email": email, "token_prefix": token[:10]}))
            return {"success": True, "email": email, "token": token, "error": None, "steps": step_results}

        except Exception as exc:
            error = str(exc)
            self._progress.emit(PipelineEvent("error", {"step": "unknown", "error": error}))
            return {"success": False, "email": email, "token": token, "error": error, "steps": step_results}
        finally:
            try:
                await asyncio.to_thread(runner.stop)
            except Exception:
                pass


_pipeline: RegistrationPipeline | None = None


def get_registration_pipeline() -> RegistrationPipeline:
    """Return the singleton RegistrationPipeline."""
    global _pipeline
    if _pipeline is None:
        _pipeline = RegistrationPipeline()
    return _pipeline
