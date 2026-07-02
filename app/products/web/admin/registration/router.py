"""Admin registration automation API endpoints.

Provides endpoints to start/stop/monitor automatic Grok account registration.
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson
from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.logging.logger import logger
from app.control.register.pipeline import (
    RegistrationOptions,
    get_registration_pipeline,
)
from app.dataplane.register.automation.turnstile import check_flaresolverr

from .. import verify_admin_key

router = APIRouter(
    prefix="/register/automation",
    tags=["Admin - Register"],
    dependencies=[Depends(verify_admin_key)],
)


def _json(data: Any, status: int = 200) -> Response:
    return Response(
        content=orjson.dumps(data), media_type="application/json", status_code=status
    )


# ---------------------------------------------------------------------------
# GET /admin/api/register/automation/status
# ---------------------------------------------------------------------------


@router.get("/status")
async def automation_status():
    """Check whether the registration automation is ready to run.

    Returns FlareSolverr connectivity, browser availability,
    email provider status, and whether a pipeline is currently running.
    """
    cfg = get_config()
    pipeline = get_registration_pipeline()
    engine = cfg.get_str("register.browser.engine", "drission").strip().lower()

    # FlareSolverr
    fs_url = cfg.get_str("proxy.clearance.flaresolverr_url", "")
    flaresolverr_status = {"configured": bool(fs_url), "required": engine == "playwright"}
    if fs_url:
        fs_timeout = cfg.get_int("proxy.clearance.timeout_sec", 30)
        flaresolverr_status["check"] = await check_flaresolverr(
            fs_url, timeout=fs_timeout
        )

    # Browser
    browser_available = False
    try:
        if engine == "drission":
            import DrissionPage  # noqa: F401
        else:
            from playwright.async_api import async_playwright  # noqa: F401
        browser_available = True
    except ImportError:
        pass

    # Email provider
    mail_cfg = cfg.get("register.mail", {})
    if not isinstance(mail_cfg, dict):
        mail_cfg = {}
    providers = mail_cfg.get("providers", [])
    email_provider_configured = bool(providers)

    return _json(
        {
            "automated_registration": True,
            "engine": engine,
            "browser_automation": browser_available,
            "flaresolverr": flaresolverr_status,
            "email_provider_configured": email_provider_configured,
            "pipeline_running": pipeline.is_running,
            "progress": pipeline.progress.snapshot() if pipeline.is_running else None,
        }
    )


# ---------------------------------------------------------------------------
# POST /admin/api/register/automation/start
# ---------------------------------------------------------------------------


class StartRegistrationRequest(BaseModel):
    count: int = 1
    pool: str = "basic"
    tags: list[str] = []
    headless: bool = False


@router.post("/start")
async def start_registration(req: StartRegistrationRequest):
    """Start a batch of automatic registrations.

    Runs as a background task. Progress can be monitored via
    `GET /admin/api/register/automation/progress`.
    """
    pipeline = get_registration_pipeline()

    if pipeline.is_running:
        raise AppError(
            "Registration pipeline is already running",
            kind=ErrorKind.SERVER,
            code="pipeline_already_running",
            status=409,
        )

    if req.pool not in {"basic"}:
        raise ValidationError(
            f"Invalid pool '{req.pool}'. Supported: basic", param="pool"
        )

    if req.count < 1 or req.count > 50:
        raise ValidationError(
            "Registration count must be between 1 and 50", param="count"
        )

    cfg = get_config()
    engine = cfg.get_str("register.browser.engine", "drission").strip().lower()
    fs_url = cfg.get_str("proxy.clearance.flaresolverr_url", "")
    if engine == "playwright" and not fs_url:
        raise ValidationError(
            "FlareSolverr is not configured. Set [proxy.clearance] flaresolverr_url first.",
            param="flaresolverr_url",
        )

    mail_cfg = cfg.get("register.mail", {})
    if not isinstance(mail_cfg, dict) or not mail_cfg.get("providers"):
        raise ValidationError(
            "Email provider is not configured. Set register.mail.providers first.",
            param="mail_providers",
        )

    options = RegistrationOptions(
        count=req.count,
        pool=req.pool,
        tags=req.tags or ["auto-register"],
        headless=req.headless,
    )

    await pipeline.initialize()

    task = asyncio.create_task(
        pipeline.run_batch(options),
        name="registration-pipeline-batch",
    )

    def _done_callback(t: asyncio.Task) -> None:
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error("registration pipeline batch failed: {}", exc)

    task.add_done_callback(_done_callback)

    return _json(
        {
            "status": "started",
            "count": req.count,
            "pool": req.pool,
            "message": f"Registration started: {req.count} account(s)",
        }
    )


# ---------------------------------------------------------------------------
# POST /admin/api/register/automation/stop
# ---------------------------------------------------------------------------


@router.post("/stop")
async def stop_registration():
    """Request graceful stop of the running registration pipeline."""
    pipeline = get_registration_pipeline()
    if not pipeline.is_running:
        raise AppError(
            "No registration pipeline is running",
            kind=ErrorKind.SERVER,
            code="pipeline_not_running",
            status=404,
        )
    await pipeline.stop()
    return _json(
        {
            "status": "stopping",
            "message": "Stop requested, finishing current registration...",
        }
    )


# ---------------------------------------------------------------------------
# GET /admin/api/register/automation/progress  — SSE stream
# ---------------------------------------------------------------------------


@router.get("/progress")
async def registration_progress():
    """SSE stream of registration pipeline events."""
    pipeline = get_registration_pipeline()
    return StreamingResponse(
        pipeline.progress.event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /admin/api/register/automation/results
# ---------------------------------------------------------------------------


@router.get("/results")
async def registration_results():
    """Return the event log from the most recent registration batch."""
    pipeline = get_registration_pipeline()
    return _json(
        {
            "events": [
                {"kind": e.kind, "data": e.data, "timestamp": e.timestamp}
                for e in pipeline.progress.events
            ],
            "snapshot": pipeline.progress.snapshot(),
        }
    )
