import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from slash_visibility import EXPECTED_NAMES, SlashVisibilityAudit, slash_sync_mode, summarize_slash_visibility


def command(name, permissions=None):
    return SimpleNamespace(name=name, type=discord.AppCommandType.chat_input,
                           default_permissions=permissions, delete=AsyncMock())


def bot(remote=None):
    local = [command(name) for name in EXPECTED_NAMES]
    return SimpleNamespace(application_id=99, user=SimpleNamespace(id=99), guilds=[],
        tree=SimpleNamespace(get_commands=lambda: local,
                             fetch_commands=AsyncMock(return_value=remote if remote is not None else local),
                             sync=AsyncMock(return_value=local)))


class SlashVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_default_auto_and_invalid_safe_readonly_without_value_leak(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(slash_sync_mode(), "auto")
            for mode in ("auto", "0", "1"):
                os.environ["SLASH_SYNC_ON_STARTUP"] = mode
                self.assertEqual(slash_sync_mode(), mode)
            os.environ["SLASH_SYNC_ON_STARTUP"] = "secret invalid setting"
            with self.assertLogs("slash_visibility", level="WARNING") as logs:
                self.assertEqual(slash_sync_mode(), "0")
            self.assertNotIn("secret invalid", "\n".join(logs.output))

    async def test_auto_healthy_is_readonly_and_repeated_start_audit_is_once(self):
        target, state = bot(), SlashVisibilityAudit()
        await asyncio.gather(*(state.run(target, "auto") for _ in range(5)))
        target.tree.fetch_commands.assert_awaited_once()
        target.tree.sync.assert_not_awaited()
        self.assertEqual(state.last_reason, "already_published")

    async def test_auto_repairs_missing_obsolete_and_restricted_commands(self):
        cases = ([], [command("ai질문")],
                 [command(name, discord.Permissions(administrator=True)) for name in EXPECTED_NAMES],
                 [command(name, discord.Permissions.none()) for name in EXPECTED_NAMES])
        for remote in cases:
            target, state = bot(remote), SlashVisibilityAudit()
            self.assertTrue(await state.run(target, "auto"))
            target.tree.sync.assert_awaited_once()
            self.assertTrue(state.published)

    async def test_readonly_reports_missing_without_repair_and_explicit_one_can_repair(self):
        target, state = bot([]), SlashVisibilityAudit()
        self.assertFalse(await state.run(target, "0"))
        target.tree.sync.assert_not_awaited()
        self.assertEqual(state.last_reason, "read_only_missing")
        self.assertTrue(await state.run(target, "1"))
        target.tree.sync.assert_awaited_once()

    async def test_force_one_audits_then_publishes_exact_local_set_once(self):
        target, state = bot(), SlashVisibilityAudit()
        self.assertTrue(await state.run(target, "1"))
        self.assertFalse(await state.run(target, "1"))
        target.tree.sync.assert_awaited_once()

    async def test_foreign_or_context_commands_block_bulk_deletion(self):
        for foreign in (command("another_extension"), SimpleNamespace(name="질문", type=discord.AppCommandType.user)):
            target, state = bot([foreign]), SlashVisibilityAudit()
            with self.assertLogs("slash_visibility", level="ERROR"):
                self.assertFalse(await state.run(target, "1"))
            target.tree.sync.assert_not_awaited()
            self.assertEqual(state.last_reason, "unexpected_remote_commands")

    async def test_failed_remote_read_or_sync_is_safe_and_only_explicit_retry_runs(self):
        for failing in ("fetch_commands", "sync"):
            target, state = bot([]), SlashVisibilityAudit()
            operation = getattr(target.tree, failing)
            original = operation.side_effect
            operation.side_effect = RuntimeError("secret remote response")
            with self.assertLogs("slash_visibility", level="ERROR") as logs:
                self.assertFalse(await state.run(target, "auto"))
            self.assertNotIn("secret remote", "\n".join(logs.output))
            operation.side_effect = original
            self.assertFalse(await state.run(target, "auto"))
            self.assertTrue(await state.run(target, "1"))

    async def test_application_identity_and_local_tree_guard(self):
        for defect in ("identity", "local", "local_context_menu"):
            target, state = bot(), SlashVisibilityAudit()
            if defect == "identity":
                target.user.id = 100
            elif defect == "local":
                target.tree.get_commands = lambda: [command("질문")]
            else:
                local = list(target.tree.get_commands())
                local.append(SimpleNamespace(name="질문", type=discord.AppCommandType.user))
                target.tree.get_commands = lambda: local
            with self.assertLogs("slash_visibility", level="ERROR"):
                self.assertFalse(await state.run(target, "auto"))
            target.tree.fetch_commands.assert_not_awaited()
            target.tree.sync.assert_not_awaited()

    async def test_guild_alias_cleanup_deletes_only_exact_alias_and_no_foreign_command(self):
        for mode in ("0", "auto", "1"):
            target, state = bot(), SlashVisibilityAudit()
            target.guilds = [SimpleNamespace(id=5)]
            obsolete, foreign = command("ai질문"), command("foreign")
            target.tree.fetch_commands.return_value = [obsolete, foreign]
            await state.audit_guild_aliases(target, mode)
            await state.audit_guild_aliases(target, mode)
            target.tree.fetch_commands.assert_awaited_once()
            self.assertEqual(obsolete.delete.await_count, int(mode != "0"))
            foreign.delete.assert_not_awaited()
            target.tree.sync.assert_not_awaited()

    async def test_guild_aliases_of_other_application_are_protected(self):
        target, state = bot(), SlashVisibilityAudit()
        target.guilds = [SimpleNamespace(id=5)]
        obsolete = command("ai질문")
        obsolete.application_id = 100
        target.tree.fetch_commands.return_value = [obsolete]
        await state.audit_guild_aliases(target, "auto")
        obsolete.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
