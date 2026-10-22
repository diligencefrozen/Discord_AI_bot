"""Run the real setup/ready functions without login, tokens or user files."""
from __future__ import annotations

import ast
import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot_ui

try:
    import discord
    from discord.ext import commands
except ModuleNotFoundError:
    discord = commands = None


def lifecycle():
    source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    names = {"Doribot", "_slash_sync_requested", "_register_slash_commands_once", "_register_intro_views_once", "on_ready"}
    nodes = []
    for node in source.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    namespace = {
        "commands": commands, "os": os, "logging": logging, "asyncio": asyncio,
        "register_slash_commands": bot_ui.register_slash_commands,
        "admin_configuration_diagnostics": bot_ui.admin_configuration_diagnostics,
        "SLASH_COMMAND_NAMES": frozenset(("시작", "질문", "그림", "내정보", "관리")),
        "_slash_registration_state": bot_ui.SlashCommandRegistrationState(),
        "_intro_view_registered": False, "_runtime_tasks_started": True,
        "_intro_view": lambda: bot_ui.IntroView(), "TypingIntroView": bot_ui.TypingIntroView,
        "load_xp_data": Mock(), "load_achievements_data": Mock(),
    }
    for name in ("_start_interaction_service", "_question_interaction_service", "_image_interaction_service", "_growth_interaction_service", "_menu_interaction_service", "_admin_action_service", "_admin_test_service"):
        namespace[name] = AsyncMock()
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    bot = namespace["Doribot"](command_prefix="!", intents=discord.Intents.default(), application_id=123)
    namespace["bot"] = bot
    async def remote_sync():
        return [SimpleNamespace(name=command.name) for command in bot.tree.get_commands()]
    bot.tree.sync = AsyncMock(side_effect=remote_sync)
    bot.tree.fetch_commands = AsyncMock(return_value=[])
    return namespace


@unittest.skipIf(discord is None, "requires discord.py")
class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def assert_five_commands(self, bot):
        self.assertEqual({command.name for command in bot.tree.get_commands()}, {"시작", "질문", "그림", "내정보", "관리"})
        self.assertEqual(len(bot.tree.get_commands()), 5)
        self.assertTrue(bot.tree.get_command("관리").guild_only)

    async def test_unset_sync_flag_defaults_to_no_remote_write(self):
        ns = lifecycle()
        try:
            with patch.dict(os.environ):
                os.environ.pop("SLASH_SYNC_ON_STARTUP", None)
                self.assertFalse(ns["_slash_sync_requested"]())
                await ns["bot"].setup_hook()
            self.assert_five_commands(ns["bot"])
            ns["bot"].tree.sync.assert_not_awaited()
            ns["bot"].tree.fetch_commands.assert_not_awaited()
        finally:
            await ns["bot"].close()

    async def test_default_setup_registers_locally_and_reconnect_never_syncs(self):
        ns = lifecycle()
        bot = ns["bot"]
        try:
            with patch.dict(os.environ, {"SLASH_SYNC_ON_STARTUP": "0"}):
                await bot.setup_hook()
                self.assert_five_commands(bot)
                # Both legacy/V2 layouts share dispatch keys; the last view
                # supplies the same callbacks to old and new messages.
                self.assertTrue(bot.persistent_views)
                self.assertTrue(all(view.is_persistent() for view in bot.persistent_views))
                self.assertEqual({item.custom_id for view in bot.persistent_views for item in view.walk_children() if isinstance(item, discord.ui.Button)}, {"ui:question", "ui:image", "ui:growth", "ui:menu"})
                for _ in range(3):
                    await ns["on_ready"]()
                bot.tree.sync.assert_not_awaited()
                ns["load_xp_data"].assert_not_called()
                ns["load_achievements_data"].assert_not_called()
        finally:
            await bot.close()

    async def test_opt_in_setup_syncs_global_once_even_on_concurrent_helpers_and_ready(self):
        ns = lifecycle()
        bot = ns["bot"]
        try:
            with patch.dict(os.environ, {"SLASH_SYNC_ON_STARTUP": "1"}):
                with self.assertLogs(level="INFO") as captured:
                    await bot.setup_hook()
                self.assert_five_commands(bot)
                await asyncio.gather(*(ns["_register_slash_commands_once"](sync=True) for _ in range(3)))
                await ns["on_ready"]()
                bot.tree.sync.assert_awaited_once_with()
                self.assertIn("event=slash_sync_success scope=global count=5", "\n".join(captured.output))
                self.assertIn("application_id=123", "\n".join(captured.output))
        finally:
            await bot.close()

    async def test_failed_startup_sync_does_not_retry_on_ready_and_explicit_retry_is_safe(self):
        ns = lifecycle()
        bot = ns["bot"]
        remote_sync = bot.tree.sync.side_effect
        bot.tree.sync.side_effect = RuntimeError("secret upstream payload")
        try:
            with patch.dict(os.environ, {"SLASH_SYNC_ON_STARTUP": "1"}), self.assertLogs(level="INFO") as captured:
                await bot.setup_hook()
                await ns["on_ready"]()
                await ns["on_ready"]()
            self.assert_five_commands(bot)
            bot.tree.sync.assert_awaited_once()
            self.assertTrue(ns["_slash_registration_state"].commands_registered)
            self.assertFalse(ns["_slash_registration_state"].synced)
            self.assertNotIn("secret upstream payload", "\n".join(captured.output))
            bot.tree.sync.side_effect = remote_sync
            await ns["_register_slash_commands_once"](sync=True)
            self.assertTrue(ns["_slash_registration_state"].synced)
            self.assertEqual(bot.tree.sync.await_count, 2)
            self.assert_five_commands(bot)
        finally:
            await bot.close()

    async def test_fresh_process_state_registers_five_with_one_explicit_sync_per_deploy(self):
        for _ in range(2):
            ns = lifecycle()
            try:
                with patch.dict(os.environ, {"SLASH_SYNC_ON_STARTUP": "1"}):
                    await ns["bot"].setup_hook()
                self.assert_five_commands(ns["bot"])
                ns["bot"].tree.sync.assert_awaited_once_with()
            finally:
                await ns["bot"].close()

    async def test_invalid_sync_flag_never_calls_remote_and_does_not_echo_value(self):
        ns = lifecycle()
        try:
            with patch.dict(os.environ, {"SLASH_SYNC_ON_STARTUP": "secret invalid config"}), self.assertLogs(level="INFO") as captured:
                await ns["bot"].setup_hook()
            ns["bot"].tree.sync.assert_not_awaited()
            self.assert_five_commands(ns["bot"])
            self.assertNotIn("secret invalid config", "\n".join(captured.output))
        finally:
            await ns["bot"].close()

    async def test_incomplete_local_tree_is_never_globally_published(self):
        ns = lifecycle()
        try:
            await ns["_register_slash_commands_once"]()
            ns["bot"].tree.remove_command("관리")
            with self.assertLogs(level="ERROR"):
                await ns["_register_slash_commands_once"](sync=True)
            ns["bot"].tree.sync.assert_not_awaited()
            self.assertFalse(ns["_slash_registration_state"].synced)
        finally:
            await ns["bot"].close()

    async def test_unrelated_remote_commands_are_never_deleted_by_bulk_sync(self):
        ns = lifecycle()
        try:
            ns["bot"].tree.fetch_commands.return_value = [SimpleNamespace(name="other_extension")]
            with self.assertLogs(level="ERROR"):
                await ns["_register_slash_commands_once"](sync=True)
            ns["bot"].tree.sync.assert_not_awaited()
            self.assertFalse(ns["_slash_registration_state"].synced)
        finally:
            await ns["bot"].close()

    async def test_remote_inspection_failure_does_not_attempt_bulk_sync(self):
        ns = lifecycle()
        try:
            ns["bot"].tree.fetch_commands.side_effect = RuntimeError("private transport data")
            with self.assertLogs(level="ERROR") as captured:
                await ns["_register_slash_commands_once"](sync=True)
            ns["bot"].tree.sync.assert_not_awaited()
            self.assertNotIn("private transport data", "\n".join(captured.output))
        finally:
            await ns["bot"].close()

    async def test_application_bot_identity_mismatch_never_publishes(self):
        ns = lifecycle()
        try:
            ns["bot"]._connection.user = SimpleNamespace(id=456)
            with self.assertLogs(level="ERROR") as captured:
                await ns["_register_slash_commands_once"](sync=True)
            ns["bot"].tree.fetch_commands.assert_not_awaited()
            ns["bot"].tree.sync.assert_not_awaited()
            self.assertIn("application_bot_mismatch", "\n".join(captured.output))
        finally:
            await ns["bot"].close()


if __name__ == "__main__":
    unittest.main()
