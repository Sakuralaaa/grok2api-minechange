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

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class CloudflareTempEmailProvider:
    """Email provider using Cloudflare Email Worker (temp-email)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_base = str(config.get("api_base", "")).rstrip("/")
        self._domains: list[str] = config.get("domain", [])
        self._admin_password = str(config.get("admin_password", ""))
        self._enabled = bool(config.get("enable", True))
        self._address_jwts: dict[str, str] = {}

    async def check_connectivity(self) -> dict[str, Any]:
        """Check connectivity by hitting the health/info endpoint."""
        if not self._enabled:
            return {"ok": False, "message": "provider disabled"}
        if not self._api_base:
            return {"ok": False, "message": "api_base not configured"}
        if not self._admin_password:
            return {"ok": False, "message": "admin_password not configured"}
        try:
            result = await self._request(
                "GET",
                "/admin/mails?limit=1&offset=0",
                timeout=10,
                headers={"x-admin-auth": self._admin_password},
            )
            return {
                "ok": result.get("ok", False),
                "message": "connected" if result.get("ok") else str(result.get("data", "")),
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def create_email(self, domain: str | None = None) -> str:
        """Create a new temporary email address."""
        chosen_domain = domain or (self._domains[0] if self._domains else "example.com")
        local_part = self._generate_local_part()
        result = await self._request(
            "POST",
            "/admin/new_address",
            payload={
                "enablePrefix": True,
                "name": local_part,
                "domain": chosen_domain,
            },
            timeout=15,
            headers={"x-admin-auth": self._admin_password},
        )
        if not result.get("ok"):
            raise RuntimeError(f"create_email failed: {result.get('data', result)}")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        email = str(data.get("address") or "")
        jwt = str(data.get("jwt") or "")
        if not email or not jwt:
            raise RuntimeError(f"create_email response missing address/jwt: {data}")
        self._address_jwts[email] = jwt
        logger.info("email provider: created temp email {}", email)
        return email

    async def wait_for_verification_link(
        self,
        email: str,
        sender_pattern: str = "",
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> str | None:
        """Poll the inbox for a verification email from sender_pattern."""
        result = await self.wait_for_verification(email, sender_pattern, timeout, interval)
        return result.get("link")

    async def wait_for_verification(
        self,
        email: str,
        sender_pattern: str = "",
        timeout: float = 120.0,
        interval: float = 2.0,
    ) -> dict[str, str | None]:
        """Poll the inbox and extract either a verification link or code."""
        jwt = self._address_jwts.get(email)
        if not jwt:
            raise RuntimeError(f"missing jwt for {email}")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                messages = await self._request(
                    "GET",
                    "/api/mails?limit=10&offset=0",
                    timeout=10,
                    headers={"Authorization": f"Bearer {jwt}"},
                )
                data = messages.get("data") if isinstance(messages, dict) else {}
                items = data.get("results", []) if isinstance(data, dict) else []
                for msg in items:
                    sender = (msg.get("source") or msg.get("from") or msg.get("sender", "")).lower()
                    if sender_pattern and sender_pattern.lower() not in sender:
                        continue
                    body = msg.get("raw") or msg.get("body") or msg.get("text") or msg.get("html") or ""
                    link = self._extract_verification_link(body)
                    code = self._extract_verification_code(body)
                    if link or code:
                        logger.info("email provider: found verification message for {}", email)
                        return {"link": link, "code": code}
            except Exception as exc:
                logger.debug("email provider: poll failed: {}", exc)
            await asyncio.sleep(interval)
        logger.warning("email provider: verification message not found within {}s for {}", timeout, email)
        return {"link": None, "code": None}

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

    def _extract_verification_code(self, body: str) -> str | None:
        """Extract a 4-8 digit verification code from email body text."""
        patterns = [
            r"code is:?\s*(\d{4,8})",
            r"verification code[:：]?\s*(\d{4,8})",
            r"<strong>(\d{4,8})</strong>",
            r"\b(\d{6})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        url = f"{self._api_base}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib_request.Request(
            url,
            data=body,
            method=method.upper(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _DEFAULT_USER_AGENT,
                **(headers or {}),
            },
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
