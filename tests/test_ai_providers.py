from __future__ import annotations

import base64
import os
import unittest
from unittest.mock import patch

try:
    import ai_providers
    from ai_providers import AIProviderError, HordeConfig, generate_horde_image, generate_horde_text
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
        return self.responses.pop(0)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


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
        image_bytes = b"fake-png"
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
        self.assertEqual(client.calls[1][1], "https://example.test/api/v2/generate/check/image-job")
        self.assertEqual(client.calls[2][1], "https://example.test/api/v2/generate/check/image-job")
        self.assertEqual(client.calls[3][1], "https://example.test/api/v2/generate/status/image-job")

    async def test_faulted_job_does_not_fallback_to_another_provider(self):
        client = _Client([
            _Response({"id": "faulted-job"}),
            _Response({"done": True, "faulted": True, "message": "worker failed"}),
        ])
        with self.assertRaisesRegex(AIProviderError, "worker failed"):
            await generate_horde_text(
                "question",
                config=HordeConfig(base_url="https://example.test/api"),
                client_factory=lambda **_kwargs: client,
            )

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


if __name__ == "__main__":
    unittest.main()
