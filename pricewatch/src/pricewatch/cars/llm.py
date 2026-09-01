"""A very small OpenAI-compatible chat client for the car analyst.

Points at whatever the deployment already runs (`LLM_BASE_URL`/`LLM_MODEL`),
so a self-hosted Hermes needs no second model. Everything here is optional by
construction: `available()` is false when nothing is configured, and every
caller has a deterministic path for when the model is missing, slow, or
answers with something unparseable.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from ..settings import settings

log = logging.getLogger(__name__)

# Reasoning models wrap their scratchpad in these before the real answer.
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S | re.I)


def available() -> bool:
    return bool(settings.pw_analyst_enabled and settings.llm_base_url and settings.llm_model)


def describe() -> dict:
    return {"enabled": settings.pw_analyst_enabled,
            "endpoint": settings.llm_base_url or None,
            "model": settings.llm_model or None,
            "available": available()}


async def chat(messages: list[dict], *, temperature: float = 0.2,
               max_tokens: int = 2000, timeout: float | None = None,
               thinking: bool = False) -> str | None:
    """One completion, or None if the endpoint is unavailable or unhappy.

    Reasoning is switched off by default. None of these jobs is a reasoning
    problem — they are extraction and drafting — and on a self-hosted model the
    scratchpad is most of the completion: the same paragraph costs 510 tokens
    and 94s with thinking on, 226 tokens and 48s with it off. A budget sized
    for the answer alone comes back empty when the model spends it thinking.
    """
    if not available():
        return None
    base = settings.llm_base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if not thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    async def post(body: dict) -> str | None:
        async with httpx.AsyncClient(timeout=timeout or settings.pw_analyst_timeout) as client:
            response = await client.post(f"{base}/chat/completions",
                                         json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"]

    try:
        content = await post(payload)
    except httpx.HTTPStatusError as exc:
        # Not every server understands chat_template_kwargs; retry plainly
        # rather than losing the analyst over a vendor extension.
        if exc.response is not None and exc.response.status_code == 400 and not thinking:
            payload.pop("chat_template_kwargs", None)
            try:
                content = await post(payload)
            except Exception as retry_exc:          # noqa: BLE001
                log.warning("car analyst call failed: %s: %s",
                            type(retry_exc).__name__, retry_exc)
                return None
        else:
            log.warning("car analyst call failed: HTTP %s",
                        exc.response.status_code if exc.response is not None else "?")
            return None
    except Exception as exc:                        # noqa: BLE001 — the analyst is optional
        log.warning("car analyst call failed: %s: %s", type(exc).__name__, exc)
        return None
    cleaned = _THINK_RE.sub("", content or "").strip()
    if not cleaned:
        # Silent empties are worse than errors: the caller sees "no findings"
        # and cannot tell that the model never answered.
        log.warning("car analyst returned no content (max_tokens=%s)", max_tokens)
        return None
    return cleaned


async def chat_json(messages: list[dict], *, temperature: float = 0.1,
                    max_tokens: int = 1500, thinking: bool = False) -> dict | list | None:
    """A completion parsed as JSON, tolerating fenced or chatty output."""
    text = await chat(messages, temperature=temperature, max_tokens=max_tokens,
                      thinking=thinking)
    if not text:
        return None
    return parse_json(text)


def parse_json(text: str):
    """Best-effort JSON out of a model reply.

    Models fence their JSON, prefix it with "Here is", or emit it after a
    reasoning block. Try strict first, then the largest brace/bracket span.
    """
    cleaned = _THINK_RE.sub("", text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception:
                continue
    return _salvage_array(cleaned)


def _salvage_array(text: str):
    """Recover the complete objects from an array the model ran out of room to finish.

    A budget that truncates mid-object otherwise costs the whole batch, when
    most of the entries arrived intact and are perfectly usable.
    """
    start = text.find("[")
    if start < 0:
        return None
    depth, in_string, escape = 0, False, False
    complete: list[int] = []
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                complete.append(index)
    if not complete:
        return None
    try:
        return json.loads(text[start:complete[-1] + 1] + "]")
    except Exception:
        return None
