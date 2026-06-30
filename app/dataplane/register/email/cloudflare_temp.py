"""Cloudflare Temp Email provider for receiving registration verification."""

from __future__ import annotations

import asyncio
import json
import random
import re
import string
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from app.platform.logging.logger import logger


class CloudflareTempEmailProvider:
    """Email provider using Cloudflare Email Worker (temp-email)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_base = str(config.get("api_base", "")).rstrip("/")
        self._domains: list[str] = config.get("domain", [])
        self._admin_password = str(config.get("admin_password", ""))
        self._enabled = bool(config.get("enable", True))
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._admin_password:
            self._headers["Authorization"] = f"Bearer {self._admin_password}"

    async def check_connectivity(self) -> dict[str, Any]:
        """Check connectivity by hitting the health/info endpoint."""
        if not self._enabled:
            return {"ok": False, "message": "provider disabled"}
        if not self._api_base:
            return {"ok": False, "message": "api_base not configured"}
        try:
            result = await self._request("GET", "/health", timeout=10)
            return {"ok": result.get("ok", False), "message": result.get("data", {}).get("status", "unknown")}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def create_email(self, domain: str | None = None) -> str:
        """Create a new temporary email address."""
        chosen_domain = domain or (self._domains[0] if self._domains else "example.com")
        local_part = self._generate_local_part()
        email = f"{local_part}@{chosen_domain}"
        result = await self._request(
            "POST",
            "/api/email/create",
            payload={"email": email, "domain": chosen_domain},
            timeout=15,
        )
        if not result.get("ok"):
            raise RuntimeError(f"create_email failed: {result.get('data', result)}")
        logger.info("email provider: created temp email {}", email)
        return email

    async def wait_for_verification_link(
        self,
        email: str,
        sender_pattern: str = "noreply@x.ai",
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> str | None:
        """Poll the inbox for a verification email from sender_pattern."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                messages = await self._request(
                    "GET",
                    f"/api/email/{email}/messages",
                    timeout=10,
                )
                items = messages.get("data", []) if isinstance(messages, dict) else messages
                for msg in items:
                    sender = (msg.get("from") or msg.get("sender", "")).lower()
                    if sender_pattern.lower() in sender:
                        body = msg.get("body") or msg.get("text") or msg.get("html") or ""
                        link = self._extract_verification_link(body)
                        if link:
                            logger.info("email provider: found verification link for {}", email)
                            return link
            except Exception as exc:
                logger.debug("email provider: poll failed: {}", exc)
            await asyncio.sleep(interval)
        logger.warning("email provider: verification link not found within {}s for {}", timeout, email)
        return None

    async def dispose_email(self, email: str) -> None:
        """Delete the temporary email address."""
        try:
            await self._request("DELETE", f"/api/email/{email}", timeout=10)
            logger.debug("email provider: disposed {}", email)
        except Exception as exc:
            logger.warning("email provider: dispose failed for {}: {}", email, exc)

    def _generate_local_part(self, length: int = 12) -> str:
        chars = string.ascii_lowercase + string.digits
        return "user_" + "".join(random.choices(chars, k=length))

    def _extract_verification_link(self, body: str) -> str | None:
        """Extract a verification URL from email body text."""
        url_pattern = r'https?://[^\s"<>]+'
        urls = re.findall(url_pattern, body)
        for url in urls:
            url_lower = url.lower()
            if any(kw in url_lower for kw in ("verify", "confirm", "auth", "token", "magiclink")):
                return url
        return urls[0] if urls else None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        url = f"{self._api_base}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib_request.Request(
            url,
            data=body,
            method=method.upper(),
            headers=self._headers,
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    data = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    data = {"raw": raw[:300]}
                return {
                    "ok": 200 <= resp.status < 300,
                    "status_code": resp.status,
                    "data": data,
                }
        except HTTPError as exc:
            body_raw = exc.read().decode("utf-8", "replace")[:300]
            return {"ok": False, "status_code": exc.code, "data": body_raw}
        except URLError as exc:
            return {"ok": False, "status_code": None, "data": str(exc.reason)}


def create_email_provider(config: dict[str, Any]) -> CloudflareTempEmailProvider | None:
    """Factory: create a CloudflareTempEmailProvider from config dict."""
    provider_type = str(config.get("type", "")).strip()
    if provider_type == "cloudflare_temp_email" and config.get("enable", True):
        return CloudflareTempEmailProvider(config)
    return None
