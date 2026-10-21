from __future__ import annotations

import base64
import asyncio
import os
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

try:
    import ai_providers
    from ai_providers import (
        AIProviderError, GeminiConfig, GeminiProviderError, GeminiRateLimiter,
        HordeConfig, generate_gemini_text, generate_horde_image, generate_horde_text,
    )
except ModuleNotFoundError:
    # The repository's minimal test runtime may omit optional HTTP extras;
    # CI/deployment installs them from requirements.txt.
    ai_providers = None
    AIProviderError = HordeConfig = generate_horde_image = generate_horde_text = None


class _Response:
    def __init__(self, payload=None, *, content=b"", status_code=200, headers=None):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.text = str(payload)

    @property
    def is_error(self):
        return self.status_code >= 400

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _gemini_response(text):
    return _Response({
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": text}]},
        }],
    })


@unittest.skipIf(ai_providers is None, "httpx is not installed in the minimal test runtime")
class GeminiProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_korean_and_english_use_exact_model_and_secret_header_once(self):
        for prompt, answer in [("안녕 도리봇", "안녕하세요!"), ("Hello Doribot", "Hello!")]:
            with self.subTest(prompt=prompt):
                client = _Client([_gemini_response(answer)])
                options = []

                def factory(**kwargs):
                    options.append(kwargs)
                    return client

                result = await generate_gemini_text(
                    prompt, system_prompt="Keep answers short and match the user's language.",
                    config=GeminiConfig(api_key="test-secret-header"), client_factory=factory,
                    max_tokens=321, temperature=0.4,
                )
                self.assertEqual(result, answer)
                self.assertEqual(len(client.calls), 1)
                method, url, kwargs = client.calls[0]
                self.assertEqual(method, "POST")
                self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent")
                self.assertNotIn("test-secret-header", url)
                self.assertNotIn("?", url)
                self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-secret-header")
                self.assertNotIn("apikey", kwargs["headers"])
                self.assertFalse(kwargs["follow_redirects"])
                self.assertEqual(kwargs["json"]["contents"], [{"role": "user", "parts": [{"text": prompt}]}])
                self.assertEqual(kwargs["json"]["systemInstruction"]["parts"][0]["text"], "Keep answers short and match the user's language.")
                self.assertEqual(kwargs["json"]["generationConfig"], {"maxOutputTokens": 321, "temperature": 0.4})
                self.assertEqual(options[0]["timeout"].read, 30.0)

    async def test_response_joins_public_parts_and_excludes_thoughts(self):
        client = _Client([_Response({"candidates": [{"content": {"parts": [
            {"thought": True, "text": "private reasoning"},
            {"text": "안녕"}, {"text": "하세요!"}, {"text": 7},
        ]}}]})])
        self.assertEqual(await generate_gemini_text(
            "question", config=GeminiConfig(api_key="fake-key"),
            client_factory=lambda **_kwargs: client,
        ), "안녕하세요!")

    async def test_http_errors_are_safe_distinct_and_never_retried(self):
        for status, code in [
            (400, "authentication"), (401, "authentication"), (403, "authentication"),
            (404, "model_unavailable"), (429, "quota"), (500, "server"),
            (503, "server"), (302, "http"), (418, "http"),
        ]:
            with self.subTest(status=status):
                client = _Client([_Response({"error": "private-prompt fake-key https://secret.example/"}, status_code=status)])
                with self.assertLogs(level="INFO") as logs:
                    with self.assertRaises(GeminiProviderError) as caught:
                        await generate_gemini_text(
                            "private-prompt", config=GeminiConfig(api_key="fake-key"),
                            client_factory=lambda **_kwargs: client,
                        )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual([record.event for record in logs.records], ["gemini_request_start", "gemini_request_end"])
                self.assertEqual(logs.records[-1].result, "server" if code == "http" else code)
                self.assertEqual(logs.records[-1].http_status, status)
                for secret in ["private-prompt", "fake-key", "https://", "error"]:
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertNotIn(secret, " ".join(logs.output))

    async def test_timeout_and_network_errors_discard_sensitive_exception_chain(self):
        for exception, code, phase in [
            (ai_providers.httpx.ConnectTimeout("private-prompt fake-key https://secret.example/"), "timeout", "connect"),
            (ai_providers.httpx.ReadTimeout("private-prompt fake-key https://secret.example/"), "timeout", "read"),
            (ai_providers.httpx.WriteTimeout("private-prompt fake-key https://secret.example/"), "timeout", "write"),
            (ai_providers.httpx.PoolTimeout("private-prompt fake-key https://secret.example/"), "timeout", "pool"),
            (ai_providers.httpx.TimeoutException("private-prompt fake-key https://secret.example/"), "timeout", "unspecified"),
            (ai_providers.httpx.ConnectError("private-prompt fake-key https://secret.example/"), "network", None),
        ]:
            with self.subTest(code=code, phase=phase):
                client = _Client([exception])
                with self.assertLogs(level="INFO") as logs, patch.object(ai_providers, "monotonic", side_effect=[100.0, 101.75]):
                    try:
                        await generate_gemini_text(
                            "private-prompt", config=GeminiConfig(api_key="fake-key"),
                            client_factory=lambda **_kwargs: client,
                        )
                    except GeminiProviderError as error:
                        self.assertEqual(error.code, code)
                        rendered = "".join(traceback.format_exception_only(type(error), error))
                        self.assertTrue(error.__suppress_context__)
                        for secret in ["private-prompt", "fake-key", "https://"]:
                            self.assertNotIn(secret, rendered)
                    else:
                        self.fail("expected provider error")
                self.assertEqual(len(client.calls), 1)
                self.assertEqual([record.event for record in logs.records], ["gemini_request_start", "gemini_request_end"])
                end = logs.records[-1]
                self.assertEqual(end.result, code)
                self.assertEqual(end.elapsed_ms, 1750)
                self.assertEqual(getattr(end, "timeout_phase", None), phase)
                self.assertFalse(hasattr(end, "http_status"))
                for record in logs.records:
                    self.assertEqual(record.model, "gemini-3.5-flash-lite")
                    self.assertIsNone(record.exc_info)
                for secret in ["private-prompt", "fake-key", "https://", "ConnectError", "ReadTimeout"]:
                    self.assertNotIn(secret, " ".join(logs.output))

    async def test_empty_blocked_and_malformed_responses_are_safe(self):
        cases = [
            ({}, "empty"),
            ({"candidates": []}, "empty"),
            ({"candidates": [{"content": {"parts": [{"text": "  "}]}}]}, "empty"),
            ({"promptFeedback": {"blockReason": "secret-prompt"}}, "blocked"),
            ({"candidates": [{"finishReason": "SAFETY", "content": {"parts": [{"text": "unsafe partial output"}]}}]}, "blocked"),
            ({"candidates": [{"finishReason": "RECITATION"}]}, "blocked"),
            ({"candidates": [{"finishReason": {"unexpected": "shape"}}]}, "blocked"),
            (ValueError("secret-prompt fake-key"), "invalid_response"),
            ([], "invalid_response"),
        ]
        for payload, code in cases:
            with self.subTest(code=code, payload=type(payload).__name__):
                client = _Client([_Response(payload)])
                with self.assertLogs(level="INFO") as logs:
                    with self.assertRaises(GeminiProviderError) as caught:
                        await generate_gemini_text(
                            "secret-prompt", config=GeminiConfig(api_key="fake-key"),
                            client_factory=lambda **_kwargs: client,
                        )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(logs.records[-1].result, "empty" if code == "invalid_response" else code)
                self.assertEqual(logs.records[-1].http_status, 200)
                self.assertNotIn("secret-prompt", str(caught.exception))
                self.assertNotIn("fake-key", str(caught.exception))
                self.assertNotIn("secret-prompt", " ".join(logs.output))
                self.assertNotIn("fake-key", " ".join(logs.output))
                self.assertEqual(len(client.calls), 1)

    async def test_missing_key_wrong_provider_wrong_model_and_empty_prompt_do_not_call_http(self):
        cases = [
            (GeminiConfig(), "question", "configuration"),
            (GeminiConfig(api_key="fake-key", provider="aihorde"), "question", "configuration"),
            (GeminiConfig(api_key="fake-key", text_model="gemini-2.5-flash"), "question", "configuration"),
            (GeminiConfig(api_key="invalid\nkey"), "question", "configuration"),
            (GeminiConfig(api_key="fake-key"), "  ", "empty_prompt"),
        ]
        for config, prompt, code in cases:
            with self.subTest(code=code, model=config.text_model):
                client = _Client([])
                with self.assertLogs(level="INFO") as logs:
                    with self.assertRaises(GeminiProviderError) as caught:
                        await generate_gemini_text(prompt, config=config, client_factory=lambda **_kwargs: client)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(logs.records[-1].result, "config")
                self.assertFalse(hasattr(logs.records[-1], "http_status"))
                self.assertEqual(client.calls, [])

    def test_env_config_uses_new_text_settings_and_hides_key(self):
        with patch.dict(os.environ, {
            "TEXT_AI_PROVIDER": " GEMINI ", "GEMINI_API_KEY": " fake-key ",
            "GEMINI_TEXT_MODEL": "gemini-3.5-flash-lite",
            "GEMINI_RPM_LIMIT": "12", "GEMINI_RPD_LIMIT": "400",
            "GEMINI_USER_COOLDOWN_SECONDS": "20", "GEMINI_TIMEOUT_SECONDS": "25",
            "AI_PROVIDER": "aihorde", "AIHORDE_API_KEY": "image-only-key",
        }, clear=True):
            config = GeminiConfig.from_env()
        config.validate()
        self.assertEqual(config.provider, "gemini")
        self.assertEqual(config.api_key, "fake-key")
        self.assertEqual(config.text_model, "gemini-3.5-flash-lite")
        self.assertEqual(config.requests_per_minute, 12)
        self.assertEqual(config.requests_per_day, 400)
        self.assertEqual(config.user_cooldown_seconds, 20)
        self.assertEqual(config.timeout_seconds, 25)
        self.assertNotIn("fake-key", repr(config))
        self.assertNotIn("image-only-key", repr(config))

    def test_invalid_numeric_env_configuration_is_explicit_without_echo(self):
        for name, value in [
            ("GEMINI_RPM_LIMIT", "private-prompt"), ("GEMINI_RPM_LIMIT", "0"),
            ("GEMINI_RPD_LIMIT", "-1"), ("GEMINI_TIMEOUT_SECONDS", "nan"),
            ("GEMINI_TIMEOUT_SECONDS", "inf"), ("GEMINI_TIMEOUT_SECONDS", "0"),
            ("GEMINI_TIMEOUT_SECONDS", "-1"), ("GEMINI_TIMEOUT_SECONDS", "private-prompt"),
            ("GEMINI_USER_COOLDOWN_SECONDS", "inf"),
        ]:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {
                "GEMINI_API_KEY": "fake-key", name: value,
            }, clear=True):
                with self.assertRaises(GeminiProviderError) as caught:
                    GeminiConfig.from_env().validate()
                self.assertEqual(caught.exception.code, "configuration")
                self.assertNotIn("private-prompt", str(caught.exception))
                self.assertNotIn("fake-key", str(caught.exception))

    async def test_success_logs_actual_monotonic_latency_without_response_or_token_metadata(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "ANSWER_SENTINEL"}]}}],
            "usageMetadata": {"promptTokenCount": 987654, "sentinel": "TOKEN_SENTINEL"},
        }
        client = _Client([_Response(payload)])
        with self.assertLogs(level="INFO") as logs, patch.object(ai_providers, "monotonic", side_effect=[12.0, 13.125]):
            answer = await generate_gemini_text(
                "PROMPT_SENTINEL", system_prompt="SYSTEM_SENTINEL",
                config=GeminiConfig(api_key="KEY_SENTINEL"), user_id=981234567890,
                client_factory=lambda **_kwargs: client,
            )
        self.assertEqual(answer, "ANSWER_SENTINEL")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([record.event for record in logs.records], ["gemini_request_start", "gemini_request_end"])
        self.assertEqual(logs.records[0].elapsed_ms, 0)
        end = logs.records[-1]
        self.assertEqual(end.result, "success")
        self.assertEqual(end.http_status, 200)
        self.assertEqual(end.elapsed_ms, 1125)
        self.assertFalse(hasattr(end, "timeout_phase"))
        for record in logs.records:
            self.assertEqual(record.model, "gemini-3.5-flash-lite")
            self.assertIsNone(record.exc_info)
        for private in ["PROMPT_SENTINEL", "SYSTEM_SENTINEL", "KEY_SENTINEL", "ANSWER_SENTINEL", "TOKEN_SENTINEL", "987654", "981234567890", "https://"]:
            self.assertNotIn(private, " ".join(logs.output))

    async def test_invalid_configuration_never_logs_configured_values_or_calls_http(self):
        sentinel = "CONFIG_SECRET_SENTINEL"
        configs = [
            GeminiConfig(api_key=sentinel, provider=sentinel),
            GeminiConfig(api_key=sentinel, text_model=sentinel),
            GeminiConfig(api_key=sentinel + "\n"),
            None,
        ]
        for config in configs:
            with self.subTest(config_type=type(config).__name__), patch.dict(os.environ, {
                "GEMINI_API_KEY": sentinel, "GEMINI_TIMEOUT_SECONDS": sentinel,
            }, clear=True):
                client = _Client([])
                with self.assertLogs(level="INFO") as logs:
                    with self.assertRaises(GeminiProviderError) as caught:
                        await generate_gemini_text(
                            "PROMPT_SENTINEL", config=config, user_id=981234567890,
                            client_factory=lambda **_kwargs: client,
                        )
                self.assertEqual(caught.exception.code, "configuration")
                self.assertEqual(client.calls, [])
                self.assertEqual(logs.records[-1].result, "config")
                for record in logs.records:
                    self.assertEqual(record.model, "gemini-3.5-flash-lite")
                    self.assertFalse(hasattr(record, "http_status"))
                for private in [sentinel, "PROMPT_SENTINEL", "981234567890"]:
                    self.assertNotIn(private, " ".join(logs.output))

    async def test_configurable_sixty_second_timeout_reserves_once_and_does_not_retry(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "KEY_SENTINEL", "GEMINI_TIMEOUT_SECONDS": "60",
        }, clear=True):
            config = GeminiConfig.from_env()
        config.validate()
        self.assertEqual(config.timeout_seconds, 60)
        limiter = AsyncMock()
        client = _Client([ai_providers.httpx.ReadTimeout("PRIVATE_NETWORK_SENTINEL")])
        options = []

        def factory(**kwargs):
            options.append(kwargs)
            return client

        with self.assertLogs(level="INFO") as logs:
            with self.assertRaises(GeminiProviderError) as caught:
                await generate_gemini_text(
                    "PROMPT_SENTINEL", config=config, limiter=limiter, user_id=42,
                    client_factory=factory,
                )
        self.assertEqual(caught.exception.code, "timeout")
        limiter.acquire.assert_awaited_once_with(42)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(options[0]["timeout"].connect, 60)
        self.assertEqual(options[0]["timeout"].read, 60)
        self.assertEqual(options[0]["timeout"].write, 60)
        self.assertEqual(options[0]["timeout"].pool, 60)
        self.assertEqual(logs.records[-1].timeout_phase, "read")
        for private in ["KEY_SENTINEL", "PROMPT_SENTINEL", "PRIVATE_NETWORK_SENTINEL"]:
            self.assertNotIn(private, " ".join(logs.output))

    async def test_shared_limiter_rejects_before_http_and_does_not_refund_failure(self):
        config = GeminiConfig(api_key="fake-key", requests_per_minute=1, user_cooldown_seconds=0)
        limiter = GeminiRateLimiter(config)
        client = _Client([_Response({}, status_code=429)])
        for code in ["quota", "minute_limit"]:
            with self.assertRaises(GeminiProviderError) as caught:
                await generate_gemini_text(
                    "question", config=config, limiter=limiter, user_id=9,
                    client_factory=lambda **_kwargs: client,
                )
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(client.calls), 1)


@unittest.skipIf(ai_providers is None, "httpx is not installed in the minimal test runtime")
class GeminiRateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_reserve_only_available_minute_slots(self):
        limiter = GeminiRateLimiter(GeminiConfig(requests_per_minute=3, user_cooldown_seconds=0))
        results = await asyncio.gather(*(limiter.acquire(user) for user in range(20)), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 3)
        errors = [result for result in results if result is not None]
        self.assertTrue(all(isinstance(result, GeminiProviderError) and result.code == "minute_limit" for result in errors))

    async def test_concurrent_same_user_reserves_only_one_slot(self):
        limiter = GeminiRateLimiter(GeminiConfig(requests_per_minute=100))
        results = await asyncio.gather(*(limiter.acquire(7) for _ in range(20)), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertTrue(all(result is None or result.code == "user_cooldown" for result in results))

    async def test_minute_and_user_cooldown_expire_at_exact_boundary(self):
        now = [100.0]
        config = GeminiConfig(requests_per_minute=2, user_cooldown_seconds=10)
        limiter = GeminiRateLimiter(config, clock=lambda: now[0])
        await limiter.acquire(1)
        with self.assertRaises(GeminiProviderError) as caught:
            await limiter.acquire(1)
        self.assertEqual(caught.exception.code, "user_cooldown")
        now[0] = 110.0
        await limiter.acquire(1)
        with self.assertRaises(GeminiProviderError) as caught:
            await limiter.acquire(2)
        self.assertEqual(caught.exception.code, "minute_limit")
        now[0] = 160.0
        await limiter.acquire(2)

    async def test_day_limit_resets_at_pacific_midnight(self):
        now = [datetime(2026, 10, 22, 6, 59, 59, tzinfo=timezone.utc)]
        limiter = GeminiRateLimiter(
            GeminiConfig(requests_per_day=1, user_cooldown_seconds=0),
            utc_now=lambda: now[0],
        )
        # A fixed Pacific summer offset makes the test independent of Windows
        # IANA data. Production uses America/Los_Angeles including DST.
        limiter._pacific = timezone(timedelta(hours=-7))
        await limiter.acquire(1)
        with self.assertRaises(GeminiProviderError) as caught:
            await limiter.acquire(2)
        self.assertEqual(caught.exception.code, "daily_limit")
        now[0] += timedelta(seconds=1)
        await limiter.acquire(2)

    async def test_missing_time_zone_data_uses_conservative_rolling_day(self):
        now = [100.0]
        with patch.object(ai_providers, "ZoneInfo", side_effect=ai_providers.ZoneInfoNotFoundError):
            limiter = GeminiRateLimiter(
                GeminiConfig(requests_per_day=1, user_cooldown_seconds=0), clock=lambda: now[0]
            )
        self.assertIsNone(limiter._pacific)
        await limiter.acquire(1)
        now[0] = 86499.0
        with self.assertRaises(GeminiProviderError) as caught:
            await limiter.acquire(2)
        self.assertEqual(caught.exception.code, "daily_limit")
        now[0] = 86500.0
        await limiter.acquire(2)


@unittest.skipIf(ai_providers is None, "httpx is not installed in the minimal test runtime")
class HordeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_submit_poll_uses_horde_endpoints_and_minimum_poll(self):
        client = _Client([
            _Response({"id": "text-job"}),
            _Response({"done": False}),
            _Response({"done": True, "generations": [{"text": "hello"}]}),
        ])

        def factory(**_kwargs):
            return client

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        config = HordeConfig(base_url="https://example.test/api", poll_interval_seconds=0.1)
        with patch.object(ai_providers.asyncio, "sleep", new=fake_sleep):
            result = await generate_horde_text("question", config=config, client_factory=factory)

        self.assertEqual(result, "hello")
        self.assertEqual(client.calls[0][0:2], ("POST", "https://example.test/api/v2/generate/text/async"))
        self.assertEqual(client.calls[1][1], "https://example.test/api/v2/generate/text/status/text-job")
        self.assertEqual(client.calls[2][1], "https://example.test/api/v2/generate/text/status/text-job")
        self.assertEqual(slept, [1.0])
        self.assertEqual(client.calls[0][2]["headers"]["apikey"], "0000000000")

    async def test_image_accepts_horde_base64_generation(self):
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(image_bytes).decode()
        client = _Client([
            _Response({"id": "image-job"}),
            _Response({"done": False}),
            _Response({"done": True}),
            _Response({"done": True, "generations": [{"img": encoded}]}),
        ])
        config = HordeConfig(base_url="https://example.test/api")
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with patch.object(ai_providers.asyncio, "sleep", new=fake_sleep):
            result = await generate_horde_image(
                "a bear", config=config, client_factory=lambda **_kwargs: client
            )
        self.assertEqual(result, image_bytes)
        self.assertEqual(slept, [2.0])
        self.assertEqual(client.calls[0][1], "https://example.test/api/v2/generate/async")
        self.assertFalse(client.calls[0][2]["json"]["r2"])
        self.assertEqual(client.calls[1][1], "https://example.test/api/v2/generate/check/image-job")
        self.assertEqual(client.calls[2][1], "https://example.test/api/v2/generate/check/image-job")
        self.assertEqual(client.calls[3][1], "https://example.test/api/v2/generate/status/image-job")

    async def test_faulted_job_does_not_fallback_to_another_provider(self):
        client = _Client([
            _Response({"id": "faulted-job"}),
            _Response({"done": True, "faulted": True, "message": "worker failed"}),
        ])
        with self.assertRaisesRegex(AIProviderError, "generation faulted"):
            await generate_horde_text(
                "question",
                config=HordeConfig(base_url="https://example.test/api"),
                client_factory=lambda **_kwargs: client,
            )
        self.assertEqual(len(client.calls), 2)

    async def test_image_rejects_untrusted_signed_url(self):
        client = _Client([
            _Response({"id": "image-job"}),
            _Response({"done": True}),
            _Response({"generations": [{"img": "https://evil.example/image.png"}]}),
        ])
        with self.assertRaisesRegex(AIProviderError, "untrusted image host"):
            await generate_horde_image(
                "a bear",
                config=HordeConfig(base_url="https://example.test/api"),
                client_factory=lambda **_kwargs: client,
            )
        self.assertEqual(len(client.calls), 3)

    def test_from_env_clamps_poll_interval_and_supports_explicit_key(self):
        with patch.dict(
            os.environ,
            {
                "AI_HORDE_API_KEY": "test-key",
                "HORDE_POLL_INTERVAL_SECONDS": "0.01",
            },
            clear=False,
        ):
            config = HordeConfig.from_env()
        self.assertEqual(config.api_key, "test-key")
        self.assertEqual(config.poll_interval_seconds, 1.0)
        self.assertEqual(config.image_width, 512)
        self.assertEqual(config.image_height, 512)
        self.assertEqual(config.max_image_bytes, 8_000_000)

    def test_legacy_horde_aliases_work_without_using_gemini_key(self):
        with patch.dict(os.environ, {
            "AIHORDE_API_KEY": "image-only-key", "AIHORDE_IMAGE_MODEL": "image-model",
            "GEMINI_API_KEY": "text-only-key",
        }, clear=True):
            config = HordeConfig.from_env()
        self.assertEqual(config.api_key, "image-only-key")
        self.assertEqual(config.image_model, "image-model")
        self.assertNotIn("image-only-key", repr(config))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "text-only-key"}, clear=True):
            self.assertEqual(HordeConfig.from_env().api_key, "0000000000")

    async def test_horde_http_and_fault_errors_do_not_echo_or_log_provider_body(self):
        marker = "private-prompt fake-key https://secret.example/"
        cases = [
            [_Response({"error": marker}, status_code=500)],
            [_Response({"id": "image-job"}), _Response({"faulted": True, "fault": marker, "queue_position": marker})],
        ]
        for responses in cases:
            client = _Client(responses)
            with self.assertLogs(level="INFO") as logs:
                with self.assertRaises(AIProviderError) as caught:
                    await generate_horde_image(
                        "private-prompt", config=HordeConfig(api_key="fake-key"),
                        client_factory=lambda **_kwargs: client,
                    )
            for secret in ["private-prompt", "fake-key", "https://"]:
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, " ".join(logs.output))

    async def test_image_header_and_model_are_horde_only_with_gemini_configured(self):
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        client = _Client([
            _Response({"id": "image-job"}), _Response({"done": True}),
            _Response({"generations": [{"img": encoded}]}),
        ])
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "text-only-key", "AIHORDE_API_KEY": "image-only-key",
            "AIHORDE_IMAGE_MODEL": "horde-image-model", "TEXT_AI_PROVIDER": "gemini",
        }, clear=True):
            result = await generate_horde_image("a bear", client_factory=lambda **_kwargs: client)
        self.assertEqual(result, base64.b64decode(encoded))
        for _method, url, kwargs in client.calls:
            self.assertTrue(url.startswith("https://aihorde.net/api/"))
            self.assertEqual(kwargs["headers"]["apikey"], "image-only-key")
            self.assertNotIn("x-goog-api-key", kwargs["headers"])
            self.assertNotIn("text-only-key", str(kwargs))
        self.assertEqual(client.calls[0][2]["json"]["models"], ["horde-image-model"])


if __name__ == "__main__":
    unittest.main()
