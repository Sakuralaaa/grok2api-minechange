"""FlareSolverr Turnstile bridge for Grok registration."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger


async def check_flaresolverr(url: str, timeout: int = 30) -> dict:
    """Check FlareSolverr connectivity by listing sessions.

    Returns:
        A dict with keys: `ok` (bool), `message` (str), `version` (str|None).
    """
    endpoint = f"{url.rstrip('/')}/v1"
    payload = json.dumps({"cmd": "sessions.list"}).encode("utf-8")
    req = urllib_request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        loop = _get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: urllib_request.urlopen(req, timeout=timeout)
        )
        raw = await loop.run_in_executor(None, lambda: resp.read().decode("utf-8", "replace"))
        data = json.loads(raw or "{}")
        ok = data.get("status") == "ok"
        return {
            "ok": ok,
            "message": data.get("message", "") if ok else data.get("error", "FlareSolverr returned non-ok status"),
            "version": data.get("version", None) if ok else None,
        }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "message": f"HTTP {exc.code}: {body}", "version": None}
    except URLError as exc:
        return {"ok": False, "message": f"Connection failed: {exc.reason}", "version": None}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "version": None}


async def create_flaresolverr_session(
    url: str,
    *,
    proxy_url: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Create a FlareSolverr session and return the session descriptor."""
    endpoint = f"{url.rstrip('/')}/v1"
    cmd: dict[str, Any] = {"cmd": "sessions.create"}
    if proxy_url and proxy_url.strip():
        cmd["proxy"] = {"url": proxy_url.strip()}
    payload = json.dumps(cmd).encode("utf-8")
    req = urllib_request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        loop = _get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: urllib_request.urlopen(req, timeout=timeout)
        )
        raw = await loop.run_in_executor(None, lambda: resp.read().decode("utf-8", "replace"))
        data = json.loads(raw or "{}")
        if data.get("status") != "ok":
            return {"ok": False, "error": data.get("error", "unknown error")}
        sess = data.get("session", {})
        if isinstance(sess, dict):
            return {
                "ok": True,
                "session": sess.get("session", ""),
                "cookies": sess.get("cookies", []),
                "user_agent": sess.get("user_agent", ""),
            }
        return {"ok": False, "error": "unexpected response format"}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {body}"}
    except URLError as exc:
        return {"ok": False, "error": f"Connection failed: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def resolve_turnstile_with_flaresolverr(
    url_to_open: str,
    *,
    flaresolverr_url: str | None = None,
    proxy_url: str | None = None,
    session_id: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Use FlareSolverr to open a URL and solve any Cloudflare challenge (including Turnstile).

    Returns cookies and user_agent that can be injected into Playwright
    to "inherit" the solved challenge.

    Returns:
        Dict with keys: `ok` (bool), `cookies` (list), `user_agent` (str),
        `session` (str), or `error` (str).
    """
    cfg = get_config()
    fs_url = flaresolverr_url or cfg.get_str("proxy.clearance.flaresolverr_url", "")
    if not fs_url:
        return {"ok": False, "error": "FlareSolverr URL not configured"}

    endpoint = f"{fs_url.rstrip('/')}/v1"
    cmd: dict[str, Any] = {
        "cmd": "request.get",
        "url": url_to_open,
        "maxTimeout": timeout * 1000,
        "session": session_id if session_id else None,
    }
    if proxy_url and proxy_url.strip():
        cmd["proxy"] = {"url": proxy_url.strip()}
    cmd = {k: v for k, v in cmd.items() if v is not None}

    payload = json.dumps(cmd).encode("utf-8")
    req = urllib_request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        loop = _get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: urllib_request.urlopen(req, timeout=timeout + 10)
        )
        raw = await loop.run_in_executor(None, lambda: resp.read().decode("utf-8", "replace"))
        data = json.loads(raw or "{}")
        if data.get("status") != "ok" or data.get("solution", {}).get("status") != 200:
            return {
                "ok": False,
                "error": data.get("error", data.get("message", "FlareSolverr failed to resolve")),
            }
        solution = data.get("solution", {})
        cookies = solution.get("cookies", [])
        return {
            "ok": True,
            "cookies": cookies,
            "user_agent": solution.get("user_agent", ""),
            "session": data.get("session", ""),
        }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {body}"}
    except URLError as exc:
        return {"ok": False, "error": f"Connection failed: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _get_event_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.new_event_loop()
