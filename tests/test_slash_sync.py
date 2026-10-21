from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot_ui
from bot_ui import SlashCommandRegistrationState, register_slash_commands


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


class SlashLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_without_sync_then_explicit_sync(self):
        state = SlashCommandRegistrationState()
        register, sync = Mock(), AsyncMock(return_value=[])
        self.assertFalse(await state.ensure(register))
        self.assertTrue(state.commands_registered)
        self.assertFalse(state.synced)
        self.assertEqual(state.sync_attempts, 0)
        self.assertTrue(await state.ensure(register, sync))
        register.assert_called_once_with()
        sync.assert_awaited_once_with()
        self.assertEqual(state.sync_attempts, 1)

    async def test_concurrent_registration_and_sync_run_once(self):
        state = SlashCommandRegistrationState()
        register = Mock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def syncing():
            entered.set()
            await release.wait()

        sync = AsyncMock(side_effect=syncing)
        calls = [asyncio.create_task(state.ensure(register, sync)) for _ in range(6)]
        await entered.wait()
        release.set()
        results = await asyncio.gather(*calls)
        self.assertEqual(results.count(True), 1)
        register.assert_called_once_with()
        sync.assert_awaited_once_with()
        self.assertEqual(state.sync_attempts, 1)

    async def test_concurrent_local_registration_never_syncs(self):
        state = SlashCommandRegistrationState()
        register = Mock()
        self.assertEqual(await asyncio.gather(*(state.ensure(register) for _ in range(5))), [False] * 5)
        register.assert_called_once_with()
        self.assertEqual(state.sync_attempts, 0)

    async def test_registration_error_propagates_and_cannot_sync_partial_tree(self):
        state = SlashCommandRegistrationState()
        register = Mock(side_effect=RuntimeError("local registration failed"))
        sync = AsyncMock()
        with self.assertRaises(RuntimeError):
            await state.ensure(register, sync)
        self.assertFalse(state.commands_registered)
        self.assertFalse(state.synced)
        self.assertEqual(state.sync_attempts, 0)
        sync.assert_not_awaited()
        register.side_effect = None
        self.assertTrue(await state.ensure(register, sync))

    async def test_failed_sync_requires_another_explicit_call_and_preserves_local_registration(self):
        state = SlashCommandRegistrationState()
        register = Mock()
        failure = RuntimeError("remote unavailable")
        sync = AsyncMock(side_effect=[failure, []])
        self.assertFalse(await state.ensure(register, sync))
        self.assertIs(state.last_error, failure)
        self.assertFalse(await state.ensure(register))
        self.assertEqual(sync.await_count, 1)
        self.assertTrue(await state.ensure(register, sync))
        self.assertIsNone(state.last_error)
        self.assertEqual(state.sync_attempts, 2)
        register.assert_called_once_with()
        self.assertFalse(await state.ensure(register, sync))
        self.assertEqual(sync.await_count, 2)

    async def test_cancelled_sync_releases_lock_and_allows_explicit_retry(self):
        state = SlashCommandRegistrationState()
        register = Mock()
        entered = asyncio.Event()

        async def waiting_sync():
            entered.set()
            await asyncio.Event().wait()

        pending = asyncio.create_task(state.ensure(register, waiting_sync))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(state.commands_registered)
        self.assertFalse(state.synced)
        self.assertEqual(state.sync_attempts, 1)
        self.assertTrue(await asyncio.wait_for(state.ensure(register, AsyncMock()), timeout=1))
        self.assertEqual(state.sync_attempts, 2)
        register.assert_called_once_with()


@unittest.skipUnless(bot_ui.DISCORD_AVAILABLE, "requires actual discord.py CommandTree")
class ActualSlashCommandTreeTests(unittest.IsolatedAsyncioTestCase):
    EXPECTED_NAMES = {"시작", "질문", "그림", "내정보", "관리"}

    def make_tree(self):
        client = bot_ui.discord.Client(intents=bot_ui.discord.Intents.none())
        self.addAsyncCleanup(client.close)
        return bot_ui.app_commands.CommandTree(client)

    async def test_registration_is_idempotent_and_retains_five_global_command_objects(self):
        tree = self.make_tree()
        first = register_slash_commands(tree)
        second = register_slash_commands(tree)
        self.assertEqual({command.name for command in tree.get_commands()}, self.EXPECTED_NAMES)
        self.assertEqual(len(tree.get_commands()), 5)
        self.assertEqual(first, second)
        self.assertTrue(all(left is right for left, right in zip(first, second)))
        self.assertEqual(tree.get_commands(guild=bot_ui.discord.Object(id=1)), [])
        self.assertTrue(tree.get_command("관리").guild_only)
        self.assertTrue(all(not tree.get_command(name).guild_only for name in self.EXPECTED_NAMES - {"관리"}))

    async def test_partial_registration_can_retry_without_overwriting_previous_commands(self):
        tree = self.make_tree()
        original_add = tree.add_command
        added = []

        def interrupted_add(command):
            if len(added) == 2:
                raise RuntimeError("registration interrupted")
            original_add(command)
            added.append(command)

        state = SlashCommandRegistrationState()
        sync = AsyncMock(return_value=[])
        with patch.object(tree, "add_command", side_effect=interrupted_add):
            with self.assertRaises(RuntimeError):
                await state.ensure(lambda: register_slash_commands(tree), sync)
        self.assertEqual(len(tree.get_commands()), 2)
        self.assertFalse(state.commands_registered)
        sync.assert_not_awaited()
        self.assertTrue(await state.ensure(lambda: register_slash_commands(tree), sync))
        self.assertEqual({command.name for command in tree.get_commands()}, self.EXPECTED_NAMES)
        self.assertEqual(len(tree.get_commands()), 5)
        self.assertTrue(all(tree.get_command(command.name) is command for command in added))
        sync.assert_awaited_once_with()

    async def test_foreign_name_collision_is_preflighted_before_any_new_commands_are_added(self):
        tree = self.make_tree()

        async def foreign_callback(interaction):
            pass

        foreign = bot_ui.app_commands.Command(name="내정보", description="foreign", callback=foreign_callback)
        tree.add_command(foreign)
        with self.assertRaises(bot_ui.app_commands.CommandAlreadyRegistered):
            register_slash_commands(tree)
        self.assertEqual(tree.get_commands(), [foreign])
        self.assertIs(tree.get_command("내정보"), foreign)

    async def test_restart_or_deploy_creates_fresh_five_command_tree_and_sync_state(self):
        for _ in range(2):
            tree = self.make_tree()
            state = SlashCommandRegistrationState()
            sync = AsyncMock(return_value=[])
            register = lambda: register_slash_commands(tree)
            self.assertFalse(await state.ensure(register))
            self.assertEqual({command.name for command in tree.get_commands()}, self.EXPECTED_NAMES)
            self.assertTrue(await state.ensure(register, sync))
            self.assertFalse(await state.ensure(register, sync))
            self.assertEqual(len(tree.get_commands()), 5)
            sync.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
