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
import io
import logging
import os
import time
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx

try:
    from PIL import Image
except ImportError:  # pragma: no cover - requirements install Pillow in production
    Image = None


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
    timeout_seconds: float = 140.0
    poll_interval_seconds: float = 2.0
    image_width: int = 512
    image_height: int = 512
    image_steps: int = 30
    max_image_bytes: int = 8_000_000
    image_hosts: tuple[str, ...] = ("aihorde.net", "stablehorde.net")

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
            max_image_bytes=_positive_int("HORDE_MAX_IMAGE_BYTES", cls.max_image_bytes),
            image_hosts=tuple(
                host.strip().lower().lstrip(".")
                for host in os.getenv(
                    "HORDE_IMAGE_HOSTS", ",".join(cls.image_hosts)
                ).split(",")
                if host.strip()
            ) or cls.image_hosts,
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
    stage: str = "request",
) -> Mapping[str, Any]:
    try:
        response = await client.request(method, url, headers=headers, json=json)
    except httpx.HTTPError as exc:
        logging.warning("AI Horde stage=%s network_error=%s", stage, type(exc).__name__)
        raise AIProviderError(f"AI Horde network error during {stage}") from exc
    logging.info("AI Horde stage=%s status=%s", stage, response.status_code)
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
    stage: str = "poll",
) -> Mapping[str, Any]:
    """Poll a Horde job at least once per second until it completes."""

    deadline = time.monotonic() + config.timeout_seconds
    while True:
        payload = await _request_json(client, "GET", url, headers=headers, stage=stage)
        logging.info(
            "AI Horde stage=%s done=%s is_possible=%s queue=%s processing=%s",
            stage,
            payload.get("done"),
            payload.get("is_possible"),
            payload.get("queue_position"),
            payload.get("processing"),
        )
        if payload.get("is_possible") is False:
            raise AIProviderError(f"AI Horde {stage} request is not possible")
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
        payload = await _request_json(
            client, "GET", check_url, headers=headers, stage="image_check"
        )
        logging.info(
            "AI Horde stage=image_check done=%s is_possible=%s queue=%s processing=%s",
            payload.get("done"),
            payload.get("is_possible"),
            payload.get("queue_position"),
            payload.get("processing"),
        )
        if payload.get("is_possible") is False:
            raise AIProviderError("AI Horde image request is not possible")
        if payload.get("fault") or payload.get("faulted"):
            detail = payload.get("fault") or payload.get("message") or "generation faulted"
            raise AIProviderError(str(detail))
        if payload.get("done") is True:
            return await _request_json(
                client, "GET", status_url, headers=headers, stage="image_status"
            )
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
            stage="text_submit",
        )
        result = await _poll_job(
            client,
            f"{config.base_url}/v2/generate/text/status/{_job_id(submitted)}",
            config=config,
            headers=headers,
            stage="text_status",
        )
    return _generation_text(result)


def _trusted_image_host(url: str, hosts: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in hosts)


def _validate_image_bytes(data: bytes, *, max_bytes: int) -> bytes:
    if not data:
        raise AIProviderError("AI Horde returned an empty image")
    if len(data) > max_bytes:
        raise AIProviderError("AI Horde image exceeded the configured size limit")

    # Pillow gives us a full container/format check.  The signature fallback
    # keeps the adapter importable in minimal test tooling while production
    # still installs Pillow from requirements.txt.
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise AIProviderError("AI Horde returned an unsupported image format")
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError("AI Horde returned an invalid image") from exc
    elif not (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    ):
        raise AIProviderError("AI Horde returned an invalid image")
    return data


def _decode_image(value: str, *, max_bytes: int) -> bytes | None:
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    value = "".join(value.split())
    if len(value) > ((max_bytes + 2) * 4 // 3):
        raise AIProviderError("AI Horde image exceeded the configured size limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    return _validate_image_bytes(decoded, max_bytes=max_bytes)


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
        # Prefer the inline result so signed worker/R2 hosts do not need to be
        # guessed or added to the allowlist.  URL responses remain supported
        # only for explicitly trusted HTTPS hosts.
        "r2": False,
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
            stage="image_submit",
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
        if image.startswith("data:") or not image.startswith(("http://", "https://")):
            decoded = _decode_image(image, max_bytes=config.max_image_bytes)
            if decoded:
                logging.info("AI Horde stage=image_decode bytes=%s", len(decoded))
                return decoded
            raise AIProviderError("AI Horde returned unsupported image data")
        if not _trusted_image_host(image, config.image_hosts):
            host = urlparse(image).hostname or "unknown"
            logging.warning("AI Horde stage=image_download untrusted_host=%s", host)
            raise AIProviderError("AI Horde returned an untrusted image host")
        if image.startswith(("https://",)):
            try:
                response = await client.get(
                    image,
                    headers={"Accept": "image/*"},
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                logging.warning(
                    "AI Horde stage=image_download network_error=%s",
                    type(exc).__name__,
                )
                raise AIProviderError("AI Horde image download failed") from exc
            final_url = str(getattr(response, "url", image))
            if not _trusted_image_host(final_url, config.image_hosts):
                host = urlparse(final_url).hostname or "unknown"
                logging.warning("AI Horde stage=image_download untrusted_host=%s", host)
                raise AIProviderError("AI Horde redirected to an untrusted image host")
            if response.is_error or not response.content:
                logging.warning(
                    "AI Horde stage=image_download status=%s content_type=%s",
                    response.status_code,
                    response.headers.get("content-type", ""),
                )
                raise AIProviderError("AI Horde image download failed")
            try:
                announced_size = int(response.headers.get("content-length", "0"))
            except (TypeError, ValueError):
                announced_size = 0
            if announced_size > config.max_image_bytes:
                raise AIProviderError("AI Horde image exceeded the configured size limit")
            logging.info(
                "AI Horde stage=image_download status=%s content_type=%s bytes=%s",
                response.status_code,
                response.headers.get("content-type", ""),
                len(response.content),
            )
            return _validate_image_bytes(response.content, max_bytes=config.max_image_bytes)
    raise AIProviderError("AI Horde returned an unsupported image value")
