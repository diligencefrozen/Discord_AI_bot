"""Gemini text and AI Horde image adapters without automatic fallback."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

try:
    from PIL import Image
except ImportError:  # pragma: no cover - requirements install Pillow in production
    Image = None


class AIProviderError(RuntimeError):
    """A provider request failed or returned an unusable response."""


GEMINI_TEXT_MODEL = "gemini-3.5-flash-lite"


class GeminiProviderError(AIProviderError):
    """A Gemini failure with a stable reason and user-safe Korean message."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GeminiConfig:
    """Text-only settings; these never affect the Horde image adapter.

    Local ceilings are operator-adjustable safeguards, not a promise about
    Google's project-wide free quota. Keep billing disabled in AI Studio.
    """

    provider: str = "gemini"
    api_key: str = field(default="", repr=False)
    text_model: str = GEMINI_TEXT_MODEL
    timeout_seconds: float = 30.0
    requests_per_minute: int = 15
    requests_per_day: int = 500
    user_cooldown_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        def number(name: str, default: float | int, convert: Callable) -> Any:
            try:
                value = convert(os.getenv(name, str(default)))
            except (TypeError, ValueError, OverflowError):
                raise GeminiProviderError(
                    "configuration", f"AI 질문 설정 {name}의 숫자 값을 확인해 주세요."
                ) from None
            return value

        return cls(
            provider=os.getenv("TEXT_AI_PROVIDER", cls.provider).strip().lower(),
            api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            text_model=os.getenv("GEMINI_TEXT_MODEL", cls.text_model).strip(),
            timeout_seconds=number("GEMINI_TIMEOUT_SECONDS", cls.timeout_seconds, float),
            requests_per_minute=number("GEMINI_RPM_LIMIT", cls.requests_per_minute, int),
            requests_per_day=number("GEMINI_RPD_LIMIT", cls.requests_per_day, int),
            user_cooldown_seconds=number(
                "GEMINI_USER_COOLDOWN_SECONDS", cls.user_cooldown_seconds, float
            ),
        )

    def validate(self) -> None:
        """Fail explicitly without including configured values or secrets."""
        if self.provider != "gemini":
            raise GeminiProviderError(
                "configuration", "AI 질문은 TEXT_AI_PROVIDER=gemini 설정이 필요합니다."
            )
        if not self.api_key or any(not 33 <= ord(char) <= 126 for char in self.api_key):
            raise GeminiProviderError(
                "configuration", "AI 질문을 사용하려면 운영자가 GEMINI_API_KEY를 설정해야 합니다."
            )
        if self.text_model != GEMINI_TEXT_MODEL:
            raise GeminiProviderError(
                "configuration", f"GEMINI_TEXT_MODEL은 {GEMINI_TEXT_MODEL}로 설정해 주세요."
            )
        if (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
            or not isinstance(self.requests_per_minute, int) or self.requests_per_minute <= 0
            or not isinstance(self.requests_per_day, int) or self.requests_per_day <= 0
            or not math.isfinite(self.user_cooldown_seconds) or self.user_cooldown_seconds < 0
        ):
            raise GeminiProviderError(
                "configuration", "Gemini 시간 제한과 요청 한도 설정을 확인해 주세요."
            )


class GeminiRateLimiter:
    """Reserve shared RPM/RPD and user cooldown slots atomically in memory.

    RPD resets at Pacific midnight when the system has IANA time zone data.
    Without it (some Windows installs), a stricter rolling 24-hour window
    avoids resetting early. Restarting clears these local counters; replicas
    and other clients still share Google's actual project quota.
    """

    def __init__(
        self,
        config: GeminiConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._minute_requests: deque[float] = deque()
        self._daily_requests: deque[float] = deque()
        self._users: dict[int | str, float] = {}
        self._day = None
        self._daily_count = 0
        try:
            self._pacific = ZoneInfo("America/Los_Angeles")
        except ZoneInfoNotFoundError:
            self._pacific = None

    async def acquire(self, user_id: int | str | None = None) -> None:
        async with self._lock:
            now = self._clock()
            while self._minute_requests and self._minute_requests[0] <= now - 60:
                self._minute_requests.popleft()
            while self._daily_requests and self._daily_requests[0] <= now - 86400:
                self._daily_requests.popleft()
            self._users = {
                user: last for user, last in self._users.items()
                if now - last < self.config.user_cooldown_seconds
            }
            if self._pacific is not None:
                day = self._utc_now().astimezone(self._pacific).date()
                if day != self._day:
                    self._day, self._daily_count = day, 0
                daily_count = self._daily_count
            else:
                daily_count = len(self._daily_requests)
            if daily_count >= self.config.requests_per_day:
                raise GeminiProviderError(
                    "daily_limit", "AI 질문의 오늘 무료 요청 한도에 도달했어요. 나중에 다시 이용해 주세요."
                )
            if len(self._minute_requests) >= self.config.requests_per_minute:
                raise GeminiProviderError(
                    "minute_limit", "AI 질문의 분당 무료 요청 한도에 도달했어요. 잠시 후 다시 이용해 주세요."
                )
            if user_id is not None and user_id in self._users:
                raise GeminiProviderError(
                    "user_cooldown", "질문을 너무 빠르게 보내고 있어요. 잠시 후 다시 질문해 주세요."
                )
            # A failed upstream request may also count towards its quota.
            # Reserve once before sending; do not refund or automatically retry.
            self._minute_requests.append(now)
            self._daily_requests.append(now)
            self._daily_count += 1
            if user_id is not None:
                self._users[user_id] = now


def _gemini_text(payload: Mapping[str, Any]) -> str:
    feedback = payload.get("promptFeedback")
    if isinstance(feedback, Mapping) and feedback.get("blockReason"):
        raise GeminiProviderError(
            "blocked", "Gemini 안전 정책으로 답변이 차단되었어요. 질문을 바꿔 주세요."
        )
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            reason = candidate.get("finishReason")
            if reason and reason not in ("STOP", "MAX_TOKENS"):
                raise GeminiProviderError(
                    "blocked", "Gemini 안전 정책으로 답변이 차단되었어요. 질문을 바꿔 주세요."
                )
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, Mapping) else None
            if isinstance(parts, list):
                text = "".join(
                    part["text"] for part in parts
                    if isinstance(part, Mapping) and isinstance(part.get("text"), str)
                    and part.get("thought") is not True
                ).strip()
                if text:
                    return text
    raise GeminiProviderError(
        "empty", "Gemini가 답변을 반환하지 않았어요. 질문을 바꿔 다시 이용해 주세요."
    )


async def generate_gemini_text(
    prompt: str,
    *,
    system_prompt: str = "",
    max_tokens: int = 512,
    temperature: float = 0.3,
    config: GeminiConfig | None = None,
    limiter: GeminiRateLimiter | None = None,
    user_id: int | str | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> str:
    """Make exactly one Gemini text request; never retry or switch providers.

    The caller shares one limiter across every question entry point. Keys are
    sent only in an authentication header, never a query string. Upstream
    response bodies and network exception messages are deliberately discarded.
    """
    config = config or GeminiConfig.from_env()
    config.validate()
    if not prompt.strip():
        raise GeminiProviderError("empty_prompt", "질문 내용을 입력해 주세요.")
    if limiter is not None:
        await limiter.acquire(user_id)
    request: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max(1, min(int(max_tokens), 2048)),
            "temperature": temperature,
        },
    }
    if system_prompt:
        request["systemInstruction"] = {"parts": [{"text": system_prompt}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.text_model}:generateContent"
    headers = {
        "x-goog-api-key": config.api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with client_factory(timeout=httpx.Timeout(config.timeout_seconds)) as client:
            response = await client.request(
                "POST", url, headers=headers, json=request, follow_redirects=False
            )
    except httpx.TimeoutException:
        raise GeminiProviderError(
            "timeout", "Gemini 응답 시간이 초과되었어요. 잠시 후 다시 이용해 주세요."
        ) from None
    except httpx.HTTPError:
        raise GeminiProviderError(
            "network", "Gemini에 연결하지 못했어요. 잠시 후 다시 이용해 주세요."
        ) from None
    status = response.status_code
    if status in {400, 401, 403}:
        raise GeminiProviderError(
            "authentication", "Gemini 키 또는 요청 권한 설정을 운영자가 확인해야 합니다."
        )
    if status == 404:
        raise GeminiProviderError(
            "model_unavailable", "선택한 Gemini 모델을 사용할 수 없어요. 운영자가 모델 접근 권한을 확인해 주세요."
        )
    if status == 429:
        raise GeminiProviderError(
            "quota", "Gemini 무료 요청 한도에 도달했어요. 잠시 후 또는 다음 날 다시 이용해 주세요."
        )
    if status >= 500:
        raise GeminiProviderError(
            "server", "Gemini 서비스에 일시적인 문제가 있어요. 잠시 후 다시 이용해 주세요."
        )
    if status < 200 or status >= 300:
        raise GeminiProviderError("http", "Gemini가 질문을 처리하지 못했어요. 잠시 후 다시 이용해 주세요.")
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise GeminiProviderError("invalid_response", "Gemini 응답을 읽지 못했어요. 잠시 후 다시 이용해 주세요.") from None
    if not isinstance(payload, Mapping):
        raise GeminiProviderError("invalid_response", "Gemini 응답을 읽지 못했어요. 잠시 후 다시 이용해 주세요.")
    return _gemini_text(payload)


@dataclass(frozen=True)
class HordeConfig:
    """Runtime settings for AI Horde.

    The anonymous Horde key is intentionally the documented all-zero key.
    Operators may provide a personal Horde key through ``HORDE_API_KEY`` (or
    the more explicit ``AI_HORDE_API_KEY``) without putting it in source.
    """

    base_url: str = "https://aihorde.net/api"
    api_key: str = field(default="0000000000", repr=False)
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
            or os.getenv("AIHORDE_API_KEY")
            or cls.api_key
        ).strip()
        # Horde's anonymous key is safe to use as a default and carries no
        # account credentials.  Never log the configured value.
        return cls(
            base_url=base_url or cls.base_url,
            api_key=api_key or cls.api_key,
            text_model=os.getenv("HORDE_TEXT_MODEL") or None,
            image_model=os.getenv("HORDE_IMAGE_MODEL") or os.getenv("AIHORDE_IMAGE_MODEL") or None,
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
        raise AIProviderError(f"AI Horde network error during {stage}") from None
    logging.info("AI Horde stage=%s status=%s", stage, response.status_code)
    if response.is_error:
        raise AIProviderError(f"AI Horde HTTP {response.status_code} during {stage}")
    try:
        payload = response.json()
    except ValueError:
        raise AIProviderError("AI Horde returned invalid JSON") from None
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
        if payload.get("is_possible") is False:
            raise AIProviderError(f"AI Horde {stage} request is not possible")
        if payload.get("fault") or payload.get("faulted"):
            raise AIProviderError(f"AI Horde {stage} generation faulted")
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
        if payload.get("is_possible") is False:
            raise AIProviderError("AI Horde image request is not possible")
        if payload.get("fault") or payload.get("faulted"):
            raise AIProviderError("AI Horde image generation faulted")
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
        except Exception:
            raise AIProviderError("AI Horde returned an invalid image") from None
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
            logging.warning("AI Horde stage=image_download reason=untrusted_host")
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
                raise AIProviderError("AI Horde image download failed") from None
            final_url = str(getattr(response, "url", image))
            if not _trusted_image_host(final_url, config.image_hosts):
                logging.warning("AI Horde stage=image_download reason=untrusted_host")
                raise AIProviderError("AI Horde redirected to an untrusted image host")
            if response.is_error or not response.content:
                logging.warning(
                    "AI Horde stage=image_download status=%s",
                    response.status_code,
                )
                raise AIProviderError("AI Horde image download failed")
            try:
                announced_size = int(response.headers.get("content-length", "0"))
            except (TypeError, ValueError):
                announced_size = 0
            if announced_size > config.max_image_bytes:
                raise AIProviderError("AI Horde image exceeded the configured size limit")
            logging.info(
                "AI Horde stage=image_download status=%s bytes=%s",
                response.status_code,
                len(response.content),
            )
            return _validate_image_bytes(response.content, max_bytes=config.max_image_bytes)
    raise AIProviderError("AI Horde returned an unsupported image value")
