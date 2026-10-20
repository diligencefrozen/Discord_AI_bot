"""Provider adapters for the bot's free AI services.

AI Horde is deliberately the default provider.  The Hugging Face adapter is
kept in :mod:`main` for backwards compatibility and is only used when the
operator explicitly selects it with ``AI_PROVIDER=huggingface``.  There is no
automatic paid-provider fallback in this module.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx


class AIProviderError(RuntimeError):
    """A provider request failed or returned an unusable response."""


@dataclass(frozen=True)
class HordeConfig:
    """Runtime settings for AI Horde.

    The anonymous Horde key is intentionally the documented all-zero key.
    Operators may provide a personal Horde key through ``HORDE_API_KEY`` (or
    the more explicit ``AI_HORDE_API_KEY``) without putting it in source.
    """

    base_url: str = "https://aihorde.net/api"
    api_key: str = "0000000000"
    text_model: str | None = None
    image_model: str | None = None
    timeout_seconds: float = 300.0
    poll_interval_seconds: float = 2.0
    image_width: int = 1024
    image_height: int = 1024
    image_steps: int = 30

    @classmethod
    def from_env(cls) -> "HordeConfig":
        def _positive_float(name: str, default: float) -> float:
            try:
                value = float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        def _positive_int(name: str, default: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        base_url = os.getenv("HORDE_BASE_URL", cls.base_url).strip().rstrip("/")
        api_key = (
            os.getenv("AI_HORDE_API_KEY")
            or os.getenv("HORDE_API_KEY")
            or cls.api_key
        ).strip()
        # Horde's anonymous key is safe to use as a default and carries no
        # account credentials.  Never log the configured value.
        return cls(
            base_url=base_url or cls.base_url,
            api_key=api_key or cls.api_key,
            text_model=os.getenv("HORDE_TEXT_MODEL") or None,
            image_model=os.getenv("HORDE_IMAGE_MODEL") or None,
            timeout_seconds=_positive_float("HORDE_TIMEOUT_SECONDS", cls.timeout_seconds),
            # Polling more frequently than once per second is discouraged by
            # Horde and can trigger rate limiting.  Clamp operator settings.
            poll_interval_seconds=max(
                1.0,
                _positive_float("HORDE_POLL_INTERVAL_SECONDS", cls.poll_interval_seconds),
            ),
            image_width=_positive_int("HORDE_IMAGE_WIDTH", cls.image_width),
            image_height=_positive_int("HORDE_IMAGE_HEIGHT", cls.image_height),
            image_steps=_positive_int("HORDE_IMAGE_STEPS", cls.image_steps),
        )


def _headers(config: HordeConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Client-Agent": "Doribot/1.0 (AI Horde)",
        "apikey": config.api_key,
    }


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if isinstance(payload, Mapping):
        for key in ("message", "error", "fault"):
            if payload.get(key):
                return str(payload[key])
    return str(payload)[:500]


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    json: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    try:
        response = await client.request(method, url, headers=headers, json=json)
    except httpx.HTTPError as exc:
        raise AIProviderError(f"AI Horde network error: {exc}") from exc
    if response.is_error:
        raise AIProviderError(f"AI Horde HTTP {response.status_code}: {_error_text(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIProviderError("AI Horde returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise AIProviderError("AI Horde returned an unexpected response")
    return payload


def _job_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("id")
    if not value:
        raise AIProviderError("AI Horde did not return a job id")
    return str(value)


async def _poll_job(
    client: httpx.AsyncClient,
    url: str,
    *,
    config: HordeConfig,
    headers: Mapping[str, str],
) -> Mapping[str, Any]:
    """Poll a Horde job at least once per second until it completes."""

    deadline = time.monotonic() + config.timeout_seconds
    while True:
        payload = await _request_json(client, "GET", url, headers=headers)
        if payload.get("fault") or payload.get("faulted"):
            detail = payload.get("fault") or payload.get("message") or "generation faulted"
            raise AIProviderError(str(detail))
        if payload.get("done") is True:
            return payload
        if time.monotonic() >= deadline:
            raise AIProviderError("AI Horde request timed out")
        # The minimum is deliberate: Horde asks clients to avoid aggressive
        # polling and this also prevents duplicate work on reconnects.
        await asyncio.sleep(max(1.0, config.poll_interval_seconds))


async def _poll_image_job(
    client: httpx.AsyncClient,
    request_id: str,
    *,
    config: HordeConfig,
    headers: Mapping[str, str],
) -> Mapping[str, Any]:
    """Use Horde's lightweight image check endpoint before fetching results."""

    deadline = time.monotonic() + config.timeout_seconds
    check_url = f"{config.base_url}/v2/generate/check/{request_id}"
    status_url = f"{config.base_url}/v2/generate/status/{request_id}"
    while True:
        payload = await _request_json(client, "GET", check_url, headers=headers)
        if payload.get("fault") or payload.get("faulted"):
            detail = payload.get("fault") or payload.get("message") or "generation faulted"
            raise AIProviderError(str(detail))
        if payload.get("done") is True:
            return await _request_json(client, "GET", status_url, headers=headers)
        if time.monotonic() >= deadline:
            raise AIProviderError("AI Horde image request timed out")
        await asyncio.sleep(max(1.0, config.poll_interval_seconds))


def _generation_text(payload: Mapping[str, Any]) -> str:
    generations = payload.get("generations")
    if isinstance(generations, list):
        for item in generations:
            if isinstance(item, Mapping) and item.get("text"):
                return str(item["text"])
    if payload.get("text"):
        return str(payload["text"])
    raise AIProviderError("AI Horde returned no text generation")


async def generate_horde_text(
    prompt: str,
    *,
    system_prompt: str = "",
    max_tokens: int = 512,
    temperature: float = 0.3,
    config: HordeConfig | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> str:
    """Submit and poll one AI Horde text generation."""

    config = config or HordeConfig.from_env()
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    request: dict[str, Any] = {
        "prompt": full_prompt,
        "params": {
            "max_length": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
        },
    }
    if config.text_model:
        request["models"] = [config.text_model]
    timeout = httpx.Timeout(config.timeout_seconds)
    async with client_factory(timeout=timeout) as client:
        headers = _headers(config)
        submitted = await _request_json(
            client,
            "POST",
            f"{config.base_url}/v2/generate/text/async",
            headers=headers,
            json=request,
        )
        result = await _poll_job(
            client,
            f"{config.base_url}/v2/generate/text/status/{_job_id(submitted)}",
            config=config,
            headers=headers,
        )
    return _generation_text(result)


def _decode_image(value: str) -> bytes | None:
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    value = "".join(value.split())
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None


async def generate_horde_image(
    prompt: str,
    *,
    config: HordeConfig | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> bytes:
    """Submit and poll one AI Horde image generation, returning image bytes."""

    config = config or HordeConfig.from_env()
    request: dict[str, Any] = {
        "prompt": prompt,
        "params": {
            "width": config.image_width,
            "height": config.image_height,
            "steps": config.image_steps,
            "cfg_scale": 7.0,
            "n": 1,
        },
        "nsfw": False,
        "censor_nsfw": True,
    }
    if config.image_model:
        request["models"] = [config.image_model]
    timeout = httpx.Timeout(config.timeout_seconds)
    async with client_factory(timeout=timeout) as client:
        headers = _headers(config)
        submitted = await _request_json(
            client,
            "POST",
            f"{config.base_url}/v2/generate/async",
            headers=headers,
            json=request,
        )
        result = await _poll_image_job(
            client,
            _job_id(submitted),
            config=config,
            headers=headers,
        )
        generations = result.get("generations")
        if not isinstance(generations, list) or not generations:
            raise AIProviderError("AI Horde returned no image generation")
        image = generations[0].get("img") if isinstance(generations[0], Mapping) else None
        if not isinstance(image, str) or not image:
            raise AIProviderError("AI Horde returned an invalid image")
        decoded = _decode_image(image)
        if decoded:
            return decoded
        if image.startswith(("http://", "https://")):
            try:
                response = await client.get(image, headers=headers)
            except httpx.HTTPError as exc:
                raise AIProviderError(f"AI Horde image download failed: {exc}") from exc
            if response.is_error or not response.content:
                raise AIProviderError(f"AI Horde image download failed: HTTP {response.status_code}")
            return response.content
    raise AIProviderError("AI Horde returned an unsupported image value")
