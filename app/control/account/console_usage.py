"""Console route usage helpers.

Basic console models do not have upstream usage/quota probes.  These helpers
keep lightweight per-account counters in AccountRecord.ext and expose virtual
quota windows for the runtime selector so console traffic is balanced per
console model family without changing the original grok.com routes.
"""

from __future__ import annotations

from typing import Any


EXT_KEY = "console_usage"
VIRTUAL_TOTAL = 32767
VIRTUAL_WINDOW_SECONDS = 315_360_000

KEY_REASONING = "reasoning"
KEY_GROK_4_3 = "grok_4_3"
KEY_MULTI_AGENT = "multi_agent"

MODE_BY_KEY = {
    KEY_REASONING: 5,
    KEY_MULTI_AGENT: 5,
    KEY_GROK_4_3: 5,
}

LABEL_BY_KEY = {
    KEY_REASONING: "Reasoning",
    KEY_GROK_4_3: "4.3",
    KEY_MULTI_AGENT: "Multi-Agent",
}


def console_usage_key_for_model(model: str) -> str:
    if "4.3" in model:
        return KEY_GROK_4_3
    if "multi-agent" in model or "heavy" in model:
        return KEY_MULTI_AGENT
    return KEY_REASONING


def console_mode_id_for_model(model: str) -> int:
    return MODE_BY_KEY[console_usage_key_for_model(model)]


def empty_console_usage() -> dict[str, dict[str, int]]:
    return {key: {"success": 0, "fail": 0} for key in MODE_BY_KEY}


def normalize_console_usage(ext: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    raw = (ext or {}).get(EXT_KEY)
    out = empty_console_usage()
    if not isinstance(raw, dict):
        return out
    for key in out:
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        out[key] = {
            "success": max(0, int(item.get("success") or 0)),
            "fail": max(0, int(item.get("fail") or 0)),
        }
    return out


def console_success_count(ext: dict[str, Any] | None, key: str) -> int:
    usage = normalize_console_usage(ext)
    return int((usage.get(key) or {}).get("success") or 0)


def virtual_remaining_from_count(count: int) -> int:
    return max(1, VIRTUAL_TOTAL - min(max(0, int(count)), VIRTUAL_TOTAL - 1))


def increment_console_usage(
    ext: dict[str, Any] | None,
    key: str,
    *,
    success: bool,
) -> dict[str, dict[str, int]]:
    usage = normalize_console_usage(ext)
    bucket = usage.setdefault(key, {"success": 0, "fail": 0})
    counter = "success" if success else "fail"
    bucket[counter] = max(0, int(bucket.get(counter) or 0)) + 1
    return usage

