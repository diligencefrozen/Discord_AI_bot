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

    async def test_server_context_install_and_age_gates_are_repaired(self):
        for field, value, counter in (
            ("allowed_contexts", discord.app_commands.AppCommandContext(guild=False, dm_channel=True), "context_mismatch_count"),
            ("allowed_contexts", discord.app_commands.AppCommandContext(guild=True, dm_channel=False), "context_mismatch_count"),
            ("allowed_contexts", discord.app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True), "context_mismatch_count"),
            ("allowed_installs", discord.app_commands.AppInstallationType(guild=False, user=True), "install_mismatch_count"),
            ("allowed_installs", discord.app_commands.AppInstallationType(guild=True, user=True), "install_mismatch_count"),
            ("nsfw", True, "age_restricted_count"),
        ):
            with self.subTest(field=field):
                remote = [command(name) for name in EXPECTED_NAMES]
                setattr(next(item for item in remote if item.name == "질문"), field, value)
                self.assertEqual(summarize_slash_visibility(remote)[counter], 1)
                target, state = bot(remote), SlashVisibilityAudit()
                self.assertTrue(await state.run(target, "auto"))
                target.tree.sync.assert_awaited_once()

    async def test_admin_dm_context_is_repaired_without_restricting_members(self):
        remote = [command(name) for name in EXPECTED_NAMES]
        next(item for item in remote if item.name == "관리").allowed_contexts = (
            discord.app_commands.AppCommandContext(guild=True, dm_channel=True))
        target, state = bot(remote), SlashVisibilityAudit()
        self.assertTrue(await state.run(target, "auto"))
        target.tree.sync.assert_awaited_once()

    async def test_readonly_records_bad_context_without_publishing(self):
        remote = [command(name) for name in EXPECTED_NAMES]
        remote[0].allowed_installs = discord.app_commands.AppInstallationType(user=True)
        target, state = bot(remote), SlashVisibilityAudit()
        self.assertFalse(await state.run(target, "0"))
        target.tree.sync.assert_not_awaited()
        self.assertEqual(state.summary["install_mismatch_count"], 1)
        self.assertIn("서버 설치", state.status_text())

    async def test_wrong_local_access_policy_never_publishes(self):
        for field, value in (("default_permissions", discord.Permissions.none()),
                             ("allowed_contexts", discord.app_commands.AppCommandContext(dm_channel=True))):
            target, state = bot([]), SlashVisibilityAudit()
            setattr(target.tree.get_commands()[0], field, value)
            with self.assertLogs("slash_visibility", level="ERROR"):
                self.assertFalse(await state.run(target, "1"))
            target.tree.fetch_commands.assert_not_awaited()
            target.tree.sync.assert_not_awaited()

    async def test_bad_sync_result_is_not_reported_as_published(self):
        target, state = bot([]), SlashVisibilityAudit()
        result = [command(name) for name in EXPECTED_NAMES]
        result[0].allowed_contexts = discord.app_commands.AppCommandContext(dm_channel=True)
        target.tree.sync.return_value = result
        with self.assertLogs("slash_visibility", level="ERROR"):
            self.assertFalse(await state.run(target, "1"))
        self.assertFalse(state.published)

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

    async def test_guild_copy_permissions_are_repaired_in_place_without_deleting_commands(self):
        for mode in ("0", "auto", "1"):
            target, state = bot(), SlashVisibilityAudit()
            await state.run(target, mode)
            target.guilds = [SimpleNamespace(id=802904099816472616)]
            copies = [command(name, discord.Permissions.none()) for name in ("질문", "관리")]
            for item in copies:
                item.application_id = target.application_id
                item.edit = AsyncMock(return_value=command(item.name))
            unrelated = command("extension", discord.Permissions.none())
            unrelated.edit = AsyncMock()
            foreign = command("질문", discord.Permissions.none())
            foreign.application_id, foreign.edit = 100, AsyncMock()
            menu = command("관리", discord.Permissions.none())
            menu.type, menu.edit = discord.AppCommandType.user, AsyncMock()
            target.tree.fetch_commands.return_value = [*copies, unrelated, foreign, menu]
            await state.audit_guild_aliases(target, mode)
            await state.audit_guild_aliases(target, mode)
            for item in copies:
                if mode == "0":
                    item.edit.assert_not_awaited()
                else:
                    item.edit.assert_awaited_once_with(default_member_permissions=None)
                item.delete.assert_not_awaited()
            for item in (unrelated, foreign, menu):
                item.edit.assert_not_awaited()
                item.delete.assert_not_awaited()
            self.assertEqual(state.guild_shadow_count, 2)
            self.assertEqual(state.guild_restricted_count, 2)
            self.assertEqual(state.guild_repaired_count, 0 if mode == "0" else 2)

    async def test_guild_repair_failure_does_not_hide_other_results_or_leak_response(self):
        target, state = bot(), SlashVisibilityAudit()
        await state.run(target, "auto")
        target.guilds = [SimpleNamespace(id=5)]
        failed, healthy = (command(name, discord.Permissions.none()) for name in ("관리", "질문"))
        failed.edit = AsyncMock(side_effect=RuntimeError("SECRET_REMOTE_BODY"))
        healthy.edit = AsyncMock(return_value=command("질문"))
        target.tree.fetch_commands.return_value = [failed, healthy]
        with self.assertLogs("slash_visibility", level="WARNING") as logs:
            await state.audit_guild_aliases(target, "auto")
        self.assertNotIn("SECRET_REMOTE_BODY", "\n".join(logs.output))
        self.assertEqual(state.guild_repaired_count, 1)
        self.assertEqual(state.guild_repair_failed_count, 1)
        failed.delete.assert_not_awaited()
        healthy.edit.assert_awaited_once_with(default_member_permissions=None)

    async def test_failed_global_safety_audit_prevents_guild_default_changes(self):
        target, state = bot([command("foreign")]), SlashVisibilityAudit()
        with self.assertLogs("slash_visibility", level="ERROR"):
            await state.run(target, "auto")
        target.guilds = [SimpleNamespace(id=5)]
        copy = command("관리", discord.Permissions.none())
        copy.edit = AsyncMock()
        target.tree.fetch_commands.return_value = [copy]
        await state.audit_guild_aliases(target, "auto")
        copy.edit.assert_not_awaited()
        copy.delete.assert_not_awaited()

    async def test_alias_delete_failure_cannot_skip_guild_access_repair(self):
        target, state = bot(), SlashVisibilityAudit()
        await state.run(target, "auto")
        target.guilds = [SimpleNamespace(id=5)]
        alias, copy = command("ai질문"), command("질문", discord.Permissions.none())
        alias.delete.side_effect = RuntimeError("SECRET_DELETE_BODY")
        copy.edit = AsyncMock(return_value=command("질문"))
        target.tree.fetch_commands.return_value = [alias, copy]
        with self.assertLogs("slash_visibility", level="WARNING") as logs:
            await state.audit_guild_aliases(target, "auto")
        self.assertNotIn("SECRET_DELETE_BODY", "\n".join(logs.output))
        copy.edit.assert_awaited_once_with(default_member_permissions=None)
        self.assertEqual(state.guild_repaired_count, 1)
        self.assertEqual(state.guild_aliases_removed, 0)


if __name__ == "__main__":
    unittest.main()
