"""Basic-account console.x.ai Responses transport and OpenAI adapters."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

import orjson

from app.control.account.enums import FeedbackKind
from app.control.model.enums import ModeId
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_s
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens
from app.products._account_selection import selection_max_retries

from ._format import (
    build_usage,
    make_chat_response,
    make_response_id,
    make_stream_chunk,
    make_thinking_chunk,
)


_BASIC_POOL_ID = 0
_BASIC_FEEDBACK_MODE = int(ModeId.FAST)
_DEFAULT_CONSOLE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
)
_DEFAULT_CONSOLE_BROWSER = "edge"

_CONSOLE_MODEL_MAP: dict[str, str] = {
    "grok-4.20-0309-reasoning": "grok-4.20-0309-reasoning",
    "grok-4.3": "grok-4.3",
    "grok-4.3-beta": "grok-4.3",
    "grok-4.20-multi-agent-0309": "grok-4.20-multi-agent-0309",
}


class NoBasicConsoleAccount(Exception):
    """Raised internally when the basic pool cannot serve a console request."""


def is_console_basic_model(model: str) -> bool:
    return model in _CONSOLE_MODEL_MAP


def console_upstream_model(model: str) -> str:
    return _CONSOLE_MODEL_MAP.get(model, model)


def _default_console_tools() -> list[dict[str, Any]]:
    if not get_config().get_bool("console.enable_search_tools", True):
        return []
    return [
        {"type": "web_search", "enable_image_understanding": True},
        {"type": "x_search"},
    ]


def _console_url() -> str:
    return get_config().get_str("console.responses_url", "https://console.x.ai/v1/responses")


def _console_cluster() -> str:
    return get_config().get_str("console.cluster", "https://us-east-1.api.x.ai")


def _console_referer() -> str:
    team_id = get_config().get_str("console.team_id", "").strip()
    if team_id:
        return f"https://console.x.ai/team/{team_id}/chat-playground"
    return "https://console.x.ai/"


def _console_user_agent() -> str:
    return get_config().get_str(
        "console.user_agent",
        _DEFAULT_CONSOLE_USER_AGENT,
    ).strip() or _DEFAULT_CONSOLE_USER_AGENT


def _console_browser_override() -> str | None:
    browser = get_config().get_str("console.browser", "").strip()
    return browser or _DEFAULT_CONSOLE_BROWSER


def _console_sso_cookie(token: str) -> str:
    tok = token[4:] if token.startswith("sso=") else token
    tok = "".join(str(tok).split())
    return f"sso={tok}; sso-rw={tok}"


def _build_console_payload(
    *,
    model: str,
    input_val: str | list[Any],
    instructions: str | None = None,
    stream: bool,
    temperature: float,
    top_p: float,
    max_output_tokens: int | None = None,
    include: list[str] | None = None,
    tools: list[Any] | None = None,
    tool_choice: Any = None,
) -> bytes:
    payload: dict[str, Any] = {
        "model": console_upstream_model(model),
        "input": input_val,
        "temperature": temperature,
        "top_p": top_p,
        "store": False,
        "stream": stream,
    }
    if instructions:
        payload["instructions"] = instructions
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens
    if include is not None:
        payload["include"] = include
    effective_tools = tools if tools is not None else _default_console_tools()
    if effective_tools:
        payload["tools"] = effective_tools
        payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
    elif tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return orjson.dumps(payload)


def _build_console_headers(token: str, _lease) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cookie": _console_sso_cookie(token),
        "Origin": "https://console.x.ai",
        "User-Agent": _console_user_agent(),
        "Authorization": "Bearer anonymous",
        "X-Cluster": _console_cluster(),
    }


def _proxy_feedback_kind(status: int | None):
    from app.control.proxy.models import ProxyFeedbackKind

    if status == 429:
        return ProxyFeedbackKind.RATE_LIMITED
    if status == 403:
        return ProxyFeedbackKind.CHALLENGE
    if status == 401:
        return ProxyFeedbackKind.UNAUTHORIZED
    if status and status >= 500:
        return ProxyFeedbackKind.UPSTREAM_5XX
    return ProxyFeedbackKind.TRANSPORT_ERROR


def _feedback_kind_for_status(status: int) -> FeedbackKind:
    if status == 429:
        return FeedbackKind.RATE_LIMITED
    if status == 401:
        return FeedbackKind.UNAUTHORIZED
    if status == 403:
        return FeedbackKind.FORBIDDEN
    return FeedbackKind.SERVER_ERROR


def _console_status_message(status: int) -> str:
    if status == 403:
        return (
            "Console upstream returned 403; console.x.ai requires a valid "
            "Cloudflare/browser session. Configure console.cf_cookies and "
            "console.user_agent from the same browser session."
        )
    return f"Console upstream returned {status}"


async def _post_console_json(token: str, payload: bytes, *, timeout_s: float) -> dict:
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    from app.dataplane.proxy import get_proxy_runtime

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(clearance_origin="console.x.ai")
    headers = _build_console_headers(token, lease)
    session_kwargs = build_session_kwargs(
        lease=lease,
        browser_override=_console_browser_override(),
    )

    try:
        async with ResettableSession(**session_kwargs) as session:
            response = await session.post(
                _console_url(),
                headers=headers,
                data=payload,
                timeout=timeout_s,
            )
            body_bytes = response.content
            if response.status_code != 200:
                body = body_bytes.decode("utf-8", "replace")[:400]
                await proxy.feedback(
                    lease,
                    ProxyFeedback(
                        kind=_proxy_feedback_kind(response.status_code),
                        status_code=response.status_code,
                    ),
                )
                raise UpstreamError(
                    _console_status_message(response.status_code),
                    status=response.status_code,
                    body=body,
                )
            await proxy.feedback(
                lease,
                ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
            )
            return orjson.loads(body_bytes) if body_bytes.strip() else {}
    except UpstreamError:
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR, status_code=None),
        )
        body = str(exc).replace("\n", "\\n")[:400]
        raise UpstreamError(f"Console transport failed: {exc}", status=502, body=body) from exc


async def _post_console_stream(
    token: str,
    payload: bytes,
    *,
    timeout_s: float,
) -> AsyncGenerator[str, None]:
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    from app.dataplane.proxy import get_proxy_runtime

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(clearance_origin="console.x.ai")
    headers = _build_console_headers(token, lease)
    session_kwargs = build_session_kwargs(
        lease=lease,
        browser_override=_console_browser_override(),
    )
    session = ResettableSession(**session_kwargs)

    try:
        response = await session.post(
            _console_url(),
            headers=headers,
            data=payload,
            timeout=timeout_s,
            stream=True,
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", "replace")[:400]
            await proxy.feedback(
                lease,
                ProxyFeedback(
                    kind=_proxy_feedback_kind(response.status_code),
                    status_code=response.status_code,
                ),
            )
            await session.close()
            raise UpstreamError(
                _console_status_message(response.status_code),
                status=response.status_code,
                body=body,
            )
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )
    except UpstreamError:
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR, status_code=None),
        )
        try:
            await session.close()
        except Exception:
            pass
        body = str(exc).replace("\n", "\\n")[:400]
        raise UpstreamError(f"Console transport failed: {exc}", status=502, body=body) from exc

    async def _lines() -> AsyncGenerator[str, None]:
        try:
            async for line in response.aiter_lines():
                yield line
        finally:
            try:
                await session.close()
            except Exception:
                pass

    return _lines()


async def _reserve_basic_console_account(directory, *, exclude_tokens: list[str] | None):
    return await directory.reserve(
        pool_candidates=(_BASIC_POOL_ID,),
        mode_id=_BASIC_FEEDBACK_MODE,
        exclude_tokens=exclude_tokens,
        now_s_override=now_s(),
    )


def _normalize_response_model(obj: dict, model: str) -> dict:
    if isinstance(obj, dict):
        obj["model"] = model
        response = obj.get("response")
        if isinstance(response, dict):
            response["model"] = model
    return obj


def _rewrite_response_sse_line(line: str, model: str) -> str:
    if not line.startswith("data:"):
        return line
    data = line[5:].strip()
    if not data or data == "[DONE]" or not data.startswith("{"):
        return line
    try:
        obj = orjson.loads(data)
    except Exception:
        return line
    if isinstance(obj, dict):
        _normalize_response_model(obj, model)
        return f"data: {orjson.dumps(obj).decode()}"
    return line


def extract_response_text(obj: dict) -> str:
    texts: list[str] = []
    direct = obj.get("output_text")
    if direct:
        texts.append(str(direct))
    wrapped = obj.get("response")
    if isinstance(wrapped, dict):
        nested = extract_response_text(wrapped)
        if nested:
            texts.append(nested)
    for item in obj.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
    return "".join(texts)


def extract_response_reasoning(obj: dict) -> str:
    texts: list[str] = []
    wrapped = obj.get("response")
    if isinstance(wrapped, dict):
        nested = extract_response_reasoning(wrapped)
        if nested:
            texts.append(nested)
    for item in obj.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for part in item.get("summary", []) or []:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
    return "".join(texts)


def _append_content_texts(content: Any, texts: list[str]) -> None:
    if isinstance(content, dict):
        text = content.get("text")
        if text:
            texts.append(str(text))
        return
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if text:
            texts.append(str(text))


def _extract_event_text(obj: dict) -> tuple[str, bool]:
    """Return text carried by a console Responses stream event.

    The console endpoint is not perfectly stable about whether text arrives as
    output_text.delta events or only in done/completed payloads. The boolean is
    True when the returned text is a full accumulated value rather than a delta.
    """
    event_type = obj.get("type")
    if event_type == "response.output_text.delta":
        return str(obj.get("delta") or ""), False
    if event_type == "response.output_text.done":
        return str(obj.get("text") or ""), True
    if event_type == "response.content_part.done":
        part = obj.get("part")
        if isinstance(part, dict):
            return str(part.get("text") or ""), True
        return "", True
    if event_type == "response.output_item.done":
        item = obj.get("item")
        if isinstance(item, dict):
            texts: list[str] = []
            _append_content_texts(item.get("content"), texts)
            return "".join(texts), True
        return "", True
    if event_type == "response.completed":
        response = obj.get("response")
        if isinstance(response, dict):
            return extract_response_text(response), True
    return "", False


def _extract_event_reasoning(obj: dict) -> tuple[str, bool]:
    event_type = obj.get("type")
    if event_type == "response.reasoning_summary_text.delta":
        return str(obj.get("delta") or ""), False
    if event_type == "response.reasoning_summary_text.done":
        return str(obj.get("text") or ""), True
    if event_type == "response.reasoning_summary_part.done":
        part = obj.get("part")
        if isinstance(part, dict):
            return str(part.get("text") or ""), True
    if event_type == "response.output_item.done":
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            texts: list[str] = []
            _append_content_texts(item.get("summary"), texts)
            return "".join(texts), True
    if event_type == "response.completed":
        response = obj.get("response")
        if isinstance(response, dict):
            return extract_response_reasoning(response), True
    return "", False


def _delta_from_full(full_text: str, emitted_text: str) -> str:
    if not full_text:
        return ""
    if emitted_text and full_text.startswith(emitted_text):
        return full_text[len(emitted_text):]
    if full_text == emitted_text:
        return ""
    return full_text


async def maybe_create_response(
    *,
    model: str,
    input_val: str | list[Any],
    instructions: str | None,
    stream: bool,
    temperature: float,
    top_p: float,
    max_output_tokens: int | None = None,
    include: list[str] | None = None,
    tools: list[Any] | None = None,
    tool_choice: Any = None,
) -> dict | AsyncGenerator[str, None] | None:
    if not is_console_basic_model(model):
        return None

    from app.dataplane.account import _directory as _acct_dir

    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    payload = _build_console_payload(
        model=model,
        input_val=input_val,
        instructions=instructions,
        stream=stream,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        include=include,
        tools=tools,
        tool_choice=tool_choice,
    )
    timeout_s = get_config().get_float("chat.timeout", 120.0)
    max_retries = selection_max_retries()

    excluded: list[str] = []
    last_exc: UpstreamError | None = None
    for attempt in range(max_retries + 1):
        acct = await _reserve_basic_console_account(directory, exclude_tokens=excluded or None)
        if acct is None:
            if attempt == 0:
                return None
            break

        success = False
        fail_exc: UpstreamError | None = None
        try:
            if stream:
                upstream = await _post_console_stream(acct.token, payload, timeout_s=timeout_s)

                async def _run_stream(lease=acct, upstream_lines=upstream):
                    nonlocal success, fail_exc
                    try:
                        async for raw_line in upstream_lines:
                            line = str(raw_line)
                            if not line.strip():
                                continue
                            yield _rewrite_response_sse_line(line, model) + "\n\n"
                        success = True
                    except UpstreamError as exc:
                        fail_exc = exc
                        raise
                    finally:
                        await directory.release(lease)
                        kind = (
                            FeedbackKind.SUCCESS
                            if success
                            else _feedback_kind_for_status(fail_exc.status)
                            if fail_exc
                            else FeedbackKind.SERVER_ERROR
                        )
                        await directory.feedback(
                            lease.token,
                            kind,
                            _BASIC_FEEDBACK_MODE,
                            now_s_val=now_s(),
                        )

                return _run_stream()

            obj = await _post_console_json(acct.token, payload, timeout_s=timeout_s)
            success = True
            return _normalize_response_model(obj, model)
        except UpstreamError as exc:
            fail_exc = exc
            last_exc = exc
            if exc.status not in {429, 401, 503} or attempt >= max_retries:
                raise
            logger.warning(
                "console basic response retry scheduled: attempt={}/{} status={} token={}...",
                attempt + 1,
                max_retries,
                exc.status,
                acct.token[:8],
            )
        finally:
            if not stream:
                await directory.release(acct)
                kind = (
                    FeedbackKind.SUCCESS
                    if success
                    else _feedback_kind_for_status(fail_exc.status)
                    if fail_exc
                    else FeedbackKind.SERVER_ERROR
                )
                await directory.feedback(
                    acct.token,
                    kind,
                    _BASIC_FEEDBACK_MODE,
                    now_s_val=now_s(),
                )
        excluded.append(acct.token)

    if last_exc is not None:
        raise last_exc
    raise NoBasicConsoleAccount()


def chat_messages_to_response_input(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        if role == "developer":
            role = "system"
        content = msg.get("content") or ""
        parts: list[dict] = []
        if isinstance(content, str):
            if content:
                ptype = "output_text" if role == "assistant" else "input_text"
                parts.append({"type": ptype, "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text = part.get("text") or ""
                    if text:
                        target = "output_text" if role == "assistant" else "input_text"
                        parts.append({"type": target, "text": text})
                elif ptype == "image_url":
                    image_url = part.get("image_url") or {}
                    url = image_url.get("url") if isinstance(image_url, dict) else ""
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
        if parts:
            items.append({"role": role, "content": parts})
    return items


async def maybe_create_chat_completion(
    *,
    model: str,
    messages: list[dict],
    stream: bool,
    emit_think: bool,
    temperature: float,
    top_p: float,
    max_tokens: int | None = None,
    tools: list[Any] | None = None,
    tool_choice: Any = None,
) -> dict | AsyncGenerator[str, None] | None:
    if not is_console_basic_model(model):
        return None

    response_input = chat_messages_to_response_input(messages)
    if not response_input:
        return None

    result = await maybe_create_response(
        model=model,
        input_val=response_input,
        instructions=None,
        stream=stream,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_tokens,
        include=["reasoning.encrypted_content"],
        tools=tools,
        tool_choice=tool_choice,
    )
    if result is None:
        return None

    if not stream:
        assert isinstance(result, dict)
        text = extract_response_text(result)
        reasoning = extract_response_reasoning(result) if emit_think else ""
        usage = result.get("usage")
        chat_usage = None
        if isinstance(usage, dict):
            pt = int(usage.get("input_tokens") or estimate_prompt_tokens(messages))
            ct = int(usage.get("output_tokens") or estimate_tokens(text))
            rt = int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
            chat_usage = build_usage(pt, ct, reasoning_tokens=rt)
        return make_chat_response(
            model,
            text,
            prompt_content=messages,
            reasoning_content=reasoning or None,
            usage=chat_usage,
        )

    assert not isinstance(result, dict)
    response_id = make_response_id()

    async def _run_chat_stream() -> AsyncGenerator[str, None]:
        done = False
        emitted_text = ""
        emitted_reasoning = ""
        async for raw in result:
            for line in str(raw).splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    if not done:
                        yield f"data: {orjson.dumps(make_stream_chunk(response_id, model, '', is_final=True)).decode()}\n\n"
                        yield "data: [DONE]\n\n"
                        done = True
                    continue
                try:
                    obj = orjson.loads(data)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                event_type = obj.get("type")
                text, text_is_full = _extract_event_text(obj)
                if text:
                    delta = _delta_from_full(text, emitted_text) if text_is_full else text
                    if delta:
                        emitted_text += delta
                        yield f"data: {orjson.dumps(make_stream_chunk(response_id, model, delta)).decode()}\n\n"
                reasoning, reasoning_is_full = _extract_event_reasoning(obj)
                if reasoning and emit_think:
                    delta = (
                        _delta_from_full(reasoning, emitted_reasoning)
                        if reasoning_is_full
                        else reasoning
                    )
                    if delta:
                        emitted_reasoning += delta
                        yield f"data: {orjson.dumps(make_thinking_chunk(response_id, model, delta)).decode()}\n\n"
                if event_type == "response.completed" and not done:
                    yield f"data: {orjson.dumps(make_stream_chunk(response_id, model, '', is_final=True)).decode()}\n\n"
                    yield "data: [DONE]\n\n"
                    done = True
        if not done:
            yield f"data: {orjson.dumps(make_stream_chunk(response_id, model, '', is_final=True)).decode()}\n\n"
            yield "data: [DONE]\n\n"

    return _run_chat_stream()


__all__ = [
    "NoBasicConsoleAccount",
    "console_upstream_model",
    "extract_response_text",
    "is_console_basic_model",
    "maybe_create_chat_completion",
    "maybe_create_response",
]
