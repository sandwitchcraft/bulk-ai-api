"""Thin async adapters over the OpenAI-compatible and Anthropic REST APIs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MAX_TOKENS = 4096

# Substrings that mark non-chat models in /models listings.
NON_CHAT_HINTS = (
    "embed", "tts", "whisper", "dall-e", "moderation", "transcribe",
    "audio", "realtime", "image", "imagen", "veo", "aqa", "davinci", "babbage",
)


@dataclass
class Result:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    finish_reason: Optional[str] = None


class APIError(Exception):
    """`kind` is "connect" (endpoint unreachable) or "auth" (key rejected) for failures that
    will hit every request to the provider, else None."""

    def __init__(
        self,
        message: str,
        retryable: bool,
        retry_after: Optional[float] = None,
        kind: Optional[str] = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.kind = kind


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=20.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )


def _headers(provider: dict, key: Optional[str]) -> dict:
    if provider["kind"] == "anthropic":
        return {
            "x-api-key": key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, list) and err:
            err = err[0].get("error", err[0])
        message = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        message = resp.text[:500]
    retry_after = None
    if resp.headers.get("retry-after"):
        try:
            retry_after = float(resp.headers["retry-after"])
        except ValueError:
            pass
    code = resp.status_code
    retryable = code in (408, 409, 425, 429) or code >= 500
    raise APIError(f"HTTP {code}: {message}", retryable, retry_after, "auth" if code == 401 else None)


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    try:
        resp = await client.request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise APIError(f"{type(e).__name__}: {e or 'could not connect'}", True, kind="connect") from e
    except httpx.TransportError as e:
        raise APIError(f"{type(e).__name__}: {e}", retryable=True) from e
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError as e:
        raise APIError(f"Invalid JSON response: {resp.text[:200]}", retryable=True) from e


async def complete(
    client: httpx.AsyncClient,
    provider: dict,
    key: Optional[str],
    model: str,
    prompt: str,
    settings: dict,
) -> Result:
    system = settings.get("system_prompt") or None
    temperature = settings.get("temperature")
    max_tokens = settings.get("max_tokens")
    base = provider["base_url"].rstrip("/")
    headers = _headers(provider, key)

    if provider["kind"] == "anthropic":
        body = {
            "model": model,
            "max_tokens": max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        data = await _request(client, "POST", f"{base}/messages", headers=headers, json=body)
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return Result(text, usage.get("input_tokens"), usage.get("output_tokens"), data.get("stop_reason"))

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens:
        # OpenAI's newer models reject max_tokens; most compatible servers still expect it.
        body["max_completion_tokens" if provider["id"] == "openai" else "max_tokens"] = max_tokens
    data = await _request(client, "POST", f"{base}/chat/completions", headers=headers, json=body)
    choices = data.get("choices") or []
    if not choices:
        raise APIError(f"No choices in response: {str(data)[:300]}", retryable=False)
    choice = choices[0]
    content = (choice.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = data.get("usage") or {}
    return Result(
        content or "",
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        choice.get("finish_reason"),
    )


async def list_models(client: httpx.AsyncClient, provider: dict, key: Optional[str]) -> list:
    base = provider["base_url"].rstrip("/")
    headers = _headers(provider, key)
    params = {"limit": 1000} if provider["kind"] == "anthropic" else None
    data = await _request(client, "GET", f"{base}/models", headers=headers, params=params)
    ids = []
    for m in data.get("data") or data.get("models") or []:
        mid = m.get("id") or m.get("name") or ""
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if mid and not any(h in mid.lower() for h in NON_CHAT_HINTS):
            ids.append(mid)
    return sorted(set(ids))
