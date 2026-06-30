"""Admin registration/import helper endpoints.

This module intentionally exposes only account import and connectivity status.
It does not automate sign-up, browser challenges, or CAPTCHA/Turnstile flows.
"""

import asyncio
import json
from typing import TYPE_CHECKING, Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Depends, Request
from pydantic import RootModel

from app.control.account.quota_defaults import supports_mode
from app.control.account.state_machine import derive_status, is_manageable
from app.control.model import registry as model_registry
from app.platform.config.snapshot import config
from app.products.openai.console import is_console_basic_model

from . import get_repo

if TYPE_CHECKING:
    from app.control.account.repository import AccountRepository

router = APIRouter(prefix="/register", tags=["Admin - Register"])
_POOL_ID_TO_NAME = {0: "basic", 1: "super", 2: "heavy"}


class MailConfigRequest(RootModel[dict[str, Any]]):
    """Loose mail config payload saved under register.mail."""


def _mask_secret(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.replace("，", ",").replace("\n", ",").split(",") if item.strip()]


def _api_keys() -> list[str]:
    raw = config.get("app.api_key", "")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _client_base_url(request: Request) -> str:
    configured = config.get_str("app.app_url", "").strip().rstrip("/")
    base_url = configured or str(request.base_url).rstrip("/")
    return f"{base_url}/v1"


def _model_available_for_pools(model: Any, pools: set[str]) -> bool:
    if not getattr(model, "enabled", False):
        return False
    if "basic" in pools and is_console_basic_model(model.model_name):
        return True
    for pool_id in model.pool_candidates():
        pool = _POOL_ID_TO_NAME.get(int(pool_id))
        if pool in pools and supports_mode(pool, int(model.mode_id)):
            return True
    return False


def _safe_mail_config() -> dict[str, Any]:
    raw = config.get("register.mail", {})
    if not isinstance(raw, dict):
        raw = {}
    providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
    safe_providers = []
    for item in providers:
        if not isinstance(item, dict):
            continue
        provider = dict(item)
        for key in ("admin_password", "api_key", "token", "cf_inbox_jwt", "ddg_token"):
            if key in provider:
                provider[key] = _mask_secret(provider.get(key))
        safe_providers.append(provider)
    return {
        "request_timeout": raw.get("request_timeout", 30),
        "wait_timeout": raw.get("wait_timeout", 30),
        "wait_interval": raw.get("wait_interval", 2),
        "providers": safe_providers,
    }


def _existing_mail_provider(index: int) -> dict[str, Any]:
    raw = config.get("register.mail", {})
    providers = raw.get("providers") if isinstance(raw, dict) and isinstance(raw.get("providers"), list) else []
    if 0 <= index < len(providers) and isinstance(providers[index], dict):
        return providers[index]
    return {}


def _request_json(
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib_request.Request(
        url,
        data=body,
        method=method.upper(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        parsed: Any
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": raw[:300]}
        return {
            "ok": 200 <= resp.status < 300,
            "status_code": resp.status,
            "data": parsed,
        }


async def _check_flaresolverr(url: str, timeout: int) -> dict:
    endpoint = f"{url.rstrip('/')}/v1"
    payload = json.dumps({"cmd": "sessions.list"}).encode("utf-8")
    req = urllib_request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        def _post() -> dict:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return {
                    "ok": 200 <= resp.status < 300,
                    "status_code": resp.status,
                    "body": body[:300],
                }

        result = await asyncio.to_thread(_post)
        try:
            parsed = json.loads(result["body"] or "{}")
        except json.JSONDecodeError:
            parsed = {}
        result["service_status"] = parsed.get("status", "")
        result["message"] = parsed.get("message", "")
        return result
    except HTTPError as exc:
        return {
            "ok": False,
            "status_code": exc.code,
            "message": exc.read().decode("utf-8", "replace")[:300],
        }
    except URLError as exc:
        return {"ok": False, "status_code": None, "message": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "status_code": None, "message": str(exc)}


async def _check_cloudflare_temp_email(provider: dict[str, Any], timeout: int) -> dict[str, Any]:
    api_base = str(provider.get("api_base") or "").strip().rstrip("/")
    admin_password = str(provider.get("admin_password") or "").strip()
    if not api_base:
        return {"ok": False, "configured": False, "message": "api_base is required"}
    if not admin_password:
        return {"ok": False, "configured": False, "message": "admin_password is required"}

    def _check() -> dict[str, Any]:
        return _request_json(
            "GET",
            f"{api_base}/admin/domains",
            timeout=timeout,
            headers={"x-admin-auth": admin_password},
        )

    try:
        result = await asyncio.to_thread(_check)
        data = result.get("data")
        domain_count = 0
        if isinstance(data, list):
            domain_count = len(data)
        elif isinstance(data, dict):
            for key in ("domains", "data", "results"):
                if isinstance(data.get(key), list):
                    domain_count = len(data[key])
                    break
        return {**result, "configured": True, "domain_count": domain_count}
    except HTTPError as exc:
        return {
            "ok": False,
            "configured": True,
            "status_code": exc.code,
            "message": exc.read().decode("utf-8", "replace")[:300],
        }
    except URLError as exc:
        return {"ok": False, "configured": True, "status_code": None, "message": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "configured": True, "status_code": None, "message": str(exc)}


async def _check_cloudmail_gen(provider: dict[str, Any], timeout: int) -> dict[str, Any]:
    api_base = str(provider.get("api_base") or "").strip().rstrip("/")
    admin_email = str(provider.get("admin_email") or "").strip()
    admin_password = str(provider.get("admin_password") or "").strip()
    if not api_base:
        return {"ok": False, "configured": False, "message": "api_base is required"}
    if not admin_email or not admin_password:
        return {"ok": False, "configured": False, "message": "admin_email and admin_password are required"}

    def _check() -> dict[str, Any]:
        result = _request_json(
            "POST",
            f"{api_base}/api/public/genToken",
            timeout=timeout,
            payload={"email": admin_email, "password": admin_password},
        )
        data = result.get("data")
        token = ""
        if isinstance(data, dict):
            token = str((data.get("data") or {}).get("token") if isinstance(data.get("data"), dict) else "")
        return {
            **result,
            "ok": bool(result.get("ok")) and bool(token),
            "token_received": bool(token),
            "message": "" if token else "token not returned",
        }

    try:
        return {"configured": True, **await asyncio.to_thread(_check)}
    except HTTPError as exc:
        return {
            "ok": False,
            "configured": True,
            "status_code": exc.code,
            "message": exc.read().decode("utf-8", "replace")[:300],
        }
    except URLError as exc:
        return {"ok": False, "configured": True, "status_code": None, "message": str(exc.reason)}
    except Exception as exc:
        return {"ok": False, "configured": True, "status_code": None, "message": str(exc)}


async def _mail_provider_checks(timeout: int) -> list[dict[str, Any]]:
    mail = config.get("register.mail", {})
    providers = mail.get("providers") if isinstance(mail, dict) and isinstance(mail.get("providers"), list) else []
    checks = []
    for idx, provider in enumerate(providers):
        if not isinstance(provider, dict):
            continue
        provider_type = str(provider.get("type") or "").strip()
        if provider_type == "cloudflare_temp_email":
            check = await _check_cloudflare_temp_email(provider, timeout)
        elif provider_type == "cloudmail_gen":
            check = await _check_cloudmail_gen(provider, timeout)
        else:
            check = {
                "ok": False,
                "configured": False,
                "message": f"unsupported mail provider: {provider_type}",
            }
        checks.append({
            "index": idx,
            "type": provider_type,
            "enabled": bool(provider.get("enable", True)),
            "api_base": str(provider.get("api_base") or "").strip(),
            "domain_count": len(_list_value(provider.get("domain"))),
            "check": check,
        })
    return checks


@router.get("/status")
async def register_status():
    egress_mode = config.get_str("proxy.egress.mode", "direct")
    proxy_url = config.get_str("proxy.egress.proxy_url", "")
    proxy_pool = config.get_list("proxy.egress.proxy_pool", [])
    clearance_mode = config.get_str("proxy.clearance.mode", "none")
    flaresolverr_url = config.get_str("proxy.clearance.flaresolverr_url", "")
    timeout = max(1, min(config.get_int("proxy.clearance.timeout_sec", 60), 15))

    check = {
        "configured": bool(flaresolverr_url.strip()),
        "ok": False,
        "status_code": None,
        "message": "not configured",
    }
    if flaresolverr_url.strip():
        check = {
            "configured": True,
            **await _check_flaresolverr(flaresolverr_url, timeout),
        }

    return {
        "proxy": {
            "mode": egress_mode,
            "proxy_url": proxy_url,
            "proxy_pool_size": len(proxy_pool),
            "socks_supported": True,
        },
        "clearance": {
            "mode": clearance_mode,
            "flaresolverr_url": flaresolverr_url,
            "flaresolverr": check,
        },
        "mail": {
            "config": _safe_mail_config(),
            "checks": await _mail_provider_checks(timeout),
        },
        "capabilities": {
            "token_import": True,
            "cf_mail_config": True,
            "automated_registration": True,
            "browser_automation": True,
        },
    }


@router.get("/client")
async def client_status(
    request: Request,
    repo: "AccountRepository" = Depends(get_repo),
):
    snapshot = await repo.runtime_snapshot()
    pool_stats: dict[str, dict[str, Any]] = {
        "basic": {"total": 0, "manageable": 0, "status": {}},
        "super": {"total": 0, "manageable": 0, "status": {}},
        "heavy": {"total": 0, "manageable": 0, "status": {}},
    }
    manageable_pools: set[str] = set()
    manageable_count = 0

    for record in snapshot.items:
        pool = record.pool if record.pool in pool_stats else "basic"
        status = derive_status(record).value
        pool_stats[pool]["total"] += 1
        pool_stats[pool]["status"][status] = pool_stats[pool]["status"].get(status, 0) + 1
        if is_manageable(record):
            pool_stats[pool]["manageable"] += 1
            manageable_count += 1
            manageable_pools.add(pool)

    models = [
        {
            "id": model.model_name,
            "name": model.public_name,
        }
        for model in model_registry.list_enabled()
        if _model_available_for_pools(model, manageable_pools)
    ]
    keys = _api_keys()

    return {
        "ready": bool(manageable_count and models),
        "base_url": _client_base_url(request),
        "api_key_required": bool(keys),
        "api_keys": [_mask_secret(key) for key in keys],
        "accounts": {
            "total": len(snapshot.items),
            "manageable": manageable_count,
            "pools": pool_stats,
        },
        "models": {
            "count": len(models),
            "items": models[:12],
        },
        "checks": {
            "has_accounts": manageable_count > 0,
            "has_models": bool(models),
            "has_api_key": bool(keys),
        },
    }


@router.post("/mail")
async def save_mail_config(req: MailConfigRequest):
    payload = dict(req.root or {})
    providers = payload.get("providers") if isinstance(payload.get("providers"), list) else []
    normalized_providers = []
    for index, item in enumerate(providers):
        if not isinstance(item, dict):
            continue
        provider_type = str(item.get("type") or "").strip()
        if provider_type not in {"cloudflare_temp_email", "cloudmail_gen"}:
            continue
        normalized = {
            "type": provider_type,
            "enable": bool(item.get("enable", True)),
            "api_base": str(item.get("api_base") or "").strip().rstrip("/"),
            "domain": _list_value(item.get("domain")),
        }
        if provider_type == "cloudflare_temp_email":
            if item.get("admin_password_unchanged"):
                normalized["admin_password"] = str(_existing_mail_provider(index).get("admin_password") or "").strip()
            else:
                normalized["admin_password"] = str(item.get("admin_password") or "").strip()
        if provider_type == "cloudmail_gen":
            normalized["admin_email"] = str(item.get("admin_email") or "").strip()
            if item.get("admin_password_unchanged"):
                normalized["admin_password"] = str(_existing_mail_provider(index).get("admin_password") or "").strip()
            else:
                normalized["admin_password"] = str(item.get("admin_password") or "").strip()
            normalized["subdomain"] = _list_value(item.get("subdomain"))
            normalized["email_prefix"] = str(item.get("email_prefix") or "").strip()
        normalized_providers.append(normalized)

    mail_config = {
        "request_timeout": int(payload.get("request_timeout") or 30),
        "wait_timeout": int(payload.get("wait_timeout") or 30),
        "wait_interval": int(payload.get("wait_interval") or 2),
        "providers": normalized_providers,
    }
    await config.update({"register": {"mail": mail_config}})
    await config.load()
    return {"status": "success", "mail": _safe_mail_config()}


__all__ = ["router"]
