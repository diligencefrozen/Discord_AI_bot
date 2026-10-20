from __future__ import annotations

import asyncio
import unittest

from bot_ui import SlashCommandRegistrationState


class SlashCommandRegistrationStateTests(unittest.TestCase):
    def test_failed_sync_is_retryable_without_duplicate_registration(self) -> None:
        state = SlashCommandRegistrationState()
        registrations: list[int] = []
        sync_attempts = 0

        def register() -> None:
            registrations.append(1)

        async def sync() -> list[object]:
            nonlocal sync_attempts
            sync_attempts += 1
            if sync_attempts == 1:
                raise RuntimeError("temporary gateway failure")
            return []

        async def exercise() -> tuple[bool, bool]:
            first = await state.ensure(register, sync)
            second = await state.ensure(register, sync)
            return first, second

        first, second = asyncio.run(exercise())
        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(registrations, [1])
        self.assertEqual(sync_attempts, 2)
        self.assertTrue(state.synced)
        self.assertIsNone(state.last_error)

    def test_successful_sync_is_not_repeated(self) -> None:
        state = SlashCommandRegistrationState()
        registrations = 0
        sync_attempts = 0

        def register() -> None:
            nonlocal registrations
            registrations += 1

        async def sync() -> list[object]:
            nonlocal sync_attempts
            sync_attempts += 1
            return []

        async def exercise() -> tuple[bool, bool]:
            first = await state.ensure(register, sync)
            second = await state.ensure(register, sync)
            return first, second

        first, second = asyncio.run(exercise())
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(registrations, 1)
        self.assertEqual(sync_attempts, 1)


if __name__ == "__main__":
    unittest.main()
