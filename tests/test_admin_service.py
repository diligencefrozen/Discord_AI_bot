from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import admin_service as admin

SPECIAL = 491239880005910538
SECOND = 855749166764654653


def permissions(**changes):
    values = {name: True for name in ("moderate_members", "manage_nicknames", "ban_members", "kick_members",
                                      "manage_messages", "manage_channels", "manage_threads", "view_channel", "read_message_history")}
    values["administrator"] = False
    values.update(changes)
    return SimpleNamespace(**values)


def context(actor_id=10):
    guild = SimpleNamespace(id=100, owner_id=10)

    def member(member_id, role):
        return SimpleNamespace(id=member_id, guild=guild, top_role=role, guild_permissions=permissions(),
                               nick="기존 별명", display_name="테스트 사용자", timed_out_until=None,
                               timeout=AsyncMock(), edit=AsyncMock(), kick=AsyncMock())

    owner, special, second, bot, target, outsider = (member(10, 90), member(SPECIAL, 80), member(SECOND, 80),
                                                    member(99, 100), member(20, 10), member(30, 80))
    members = {item.id: item for item in (owner, special, second, bot, target, outsider)}
    guild.me = bot
    guild.get_member = lambda member_id: members.get(member_id)

    async def fetch(member_id):
        return members.get(member_id)

    guild.fetch_member = AsyncMock(side_effect=fetch)
    guild.fetch_ban, guild.ban, guild.unban = AsyncMock(), AsyncMock(), AsyncMock()
    guild.get_channel = lambda channel_id: None
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.guild = 200, guild
    channel.permissions_for = lambda item: item.guild_permissions
    channel.edit, channel.purge = AsyncMock(), AsyncMock(return_value=[])
    interaction = SimpleNamespace(user=members[actor_id], guild=guild, channel=channel, data={},
                                  response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(),
                                                           edit_message=AsyncMock(), send_modal=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    return interaction, target, members


class AdminServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        for name in ("SPECIAL_ADMIN_USER_ID", "SPECIAL_ADMIN_USER_IDS", "SPECIAL_ADMIN_GUILD_IDS", "MOD_LOG_CHANNEL_ID"):
            os.environ.pop(name, None)
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f"{SPECIAL},{SECOND}"
        self.service = admin.AdminService()
        self.interaction, self.target, self.members = context()

    def tearDown(self):
        self.environment.stop()

    async def request(self, action="timeout", **kwargs):
        kwargs.setdefault("target_id", 20)
        if action == "timeout":
            kwargs.setdefault("value", 5)
        return await self.service.prepare(self.interaction, action, **kwargs)

    async def test_owner_and_both_special_admins_can_prepare_and_execute(self):
        for actor_id in (10, SPECIAL, SECOND):
            self.interaction.user = self.members[actor_id]
            request = await self.request()
            await self.service.execute(self.interaction, request)
        self.assertEqual(self.target.timeout.await_count, 3)
        self.target.timeout.assert_awaited_with(timedelta(minutes=5), reason=f"Doribot timeout; actor={SECOND}")

    async def test_cleanup_uses_shared_deletion_pipeline_with_limits_and_partial_results(self):
        deleted = AsyncMock(side_effect=[True, False])
        self.service.delete_service = deleted
        messages = [SimpleNamespace(id=index, author=SimpleNamespace(id=author))
                    for index, author in enumerate((30, 20, 30, 20, 20), 1)]

        async def history(**kwargs):
            self.assertEqual(kwargs, {"limit": 100})
            for message in messages:
                yield message

        self.interaction.channel.history = history
        request = await self.request("cleanup", value=2)
        text = await self.service.execute(self.interaction, request)
        self.assertIn("1개를 삭제", text)
        self.assertIn("실패/이미 삭제 1개", text)
        self.assertEqual(deleted.await_count, 2)
        self.assertEqual([call.args[0].id for call in deleted.call_args_list], [2, 4])
        self.assertTrue(all(call.kwargs == {"reason": "admin_cleanup"} for call in deleted.call_args_list))
        self.interaction.channel.purge.assert_not_awaited()
        self.assertEqual(self.service.audit[-1]["result"], "partial_failure")

    async def test_unauthorized_dm_and_scoped_admins_fail_before_execution(self):
        for actor_id, guild, scope in ((30, self.interaction.guild, ""), (SPECIAL, None, ""),
                                      (SPECIAL, self.interaction.guild, "101")):
            self.interaction.user = self.members[actor_id]
            self.interaction.guild = guild
            os.environ["SPECIAL_ADMIN_GUILD_IDS"] = scope
            with self.assertRaises(admin.AdminActionError):
                await self.request()
        self.target.timeout.assert_not_awaited()

    async def test_revoked_auth_and_guild_scope_rechecked_on_execute(self):
        self.interaction.user = self.members[SPECIAL]
        request = await self.request()
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(SECOND)
        with self.assertRaises(admin.AdminActionError):
            await self.service.execute(self.interaction, request)
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(SPECIAL)
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "101"
        with self.assertRaises(admin.AdminActionError):
            await self.service.execute(self.interaction, request)
        self.target.timeout.assert_not_awaited()

    async def test_permissions_and_hierarchy_rechecked_after_confirmation(self):
        request = await self.request()
        self.members[99].guild_permissions.moderate_members = False
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.service.execute(self.interaction, request)
        self.assertEqual(caught.exception.code, "bot_permission_missing")
        self.target.timeout.assert_not_awaited()
        self.assertEqual(self.service.audit[-1]["result"], "bot_permission_missing")

    async def test_actor_requires_native_permission_and_higher_role(self):
        self.interaction.user = self.members[SPECIAL]
        self.members[SPECIAL].guild_permissions.moderate_members = False
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request()
        self.assertEqual(caught.exception.code, "actor_permission_missing")
        self.members[SPECIAL].guild_permissions.moderate_members = True
        self.members[SPECIAL].top_role = self.target.top_role
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request()
        self.assertEqual(caught.exception.code, "role_hierarchy")

    async def test_self_bot_owner_other_special_and_discord_admin_are_protected(self):
        for member_id in (10, 99, SPECIAL, SECOND):
            with self.subTest(member_id=member_id):
                with self.assertRaises(admin.AdminActionError) as caught:
                    await self.request(target_id=member_id)
                self.assertEqual(caught.exception.code, "protected_target")
        self.target.guild_permissions.administrator = True
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request()
        self.assertEqual(caught.exception.code, "protected_administrator")

    async def test_equal_bot_role_and_cross_guild_member_fail_closed(self):
        self.target.top_role = self.members[99].top_role
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request()
        self.assertEqual(caught.exception.code, "role_hierarchy")
        self.target.top_role = 10
        self.target.guild = SimpleNamespace(id=101)
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request()
        self.assertEqual(caught.exception.code, "member_missing")

    async def test_target_selection_refetches_and_reports_missing_member(self):
        self.assertIs(await self.service.member(self.interaction, 20), self.target)
        self.interaction.guild.fetch_member.assert_awaited_with(20)
        with self.assertRaises(admin.AdminActionError):
            await self.service.member(self.interaction, 999)

    async def test_timeout_presets_and_maximum_28_days(self):
        for minutes in admin.TIMEOUT_MINUTES:
            await self.request(value=minutes)
        for invalid in (0, -1, 40321, 60.0, True, "60"):
            with self.subTest(value=invalid):
                with self.assertRaises(admin.AdminActionError):
                    await self.request(value=invalid)

    async def test_untimeout_and_nickname_reset_use_discord_native_api(self):
        request = await self.request("untimeout")
        await self.service.execute(self.interaction, request)
        self.target.timeout.assert_awaited_once_with(None, reason="Doribot untimeout; actor=10")
        request = await self.request("reset_nickname")
        self.assertIn("기존 별명", request.preview)
        await self.service.execute(self.interaction, request)
        self.target.edit.assert_awaited_once_with(nick=None, reason="Doribot reset_nickname; actor=10")

    async def test_nickname_before_after_validation_and_no_global_username_edits(self):
        request = await self.request("nickname", value="새 별명")
        self.assertIn("변경 전 서버 별명: 기존 별명", request.preview)
        self.assertIn("변경 후 서버 별명: 새 별명", request.preview)
        await self.service.execute(self.interaction, request)
        self.target.edit.assert_awaited_once_with(nick="새 별명", reason="Doribot nickname; actor=10")
        for value in ("", "가" * 33):
            with self.assertRaises(admin.AdminActionError):
                await self.request("nickname", value=value)

    async def test_ban_unban_and_kick_are_confirmed_permanent_native_actions(self):
        request = await self.request("ban", reason="sensitive reason")
        self.assertIn("자동 해제 없음", request.preview)
        self.interaction.guild.ban.assert_not_awaited()
        await self.service.execute(self.interaction, request)
        self.interaction.guild.ban.assert_awaited_once_with(self.target, reason="Doribot ban; actor=10", delete_message_seconds=0)
        request = await self.request("unban", target_id=999)
        await self.service.execute(self.interaction, request)
        self.assertEqual(self.interaction.guild.unban.call_args.args[0].id, 999)
        request = await self.request("kick")
        await self.service.execute(self.interaction, request)
        self.target.kick.assert_awaited_once()

    async def test_cleanup_limits_match_count_and_avoids_14_day_bulk_limit(self):
        async def purge(**kwargs):
            messages = [SimpleNamespace(author=SimpleNamespace(id=member_id)) for member_id in (30, 20, 20, 30, 20, 20)]
            return [message for message in messages if kwargs["check"](message)]

        self.interaction.channel.purge.side_effect = purge
        request = await self.request("cleanup", value=2)
        result = await self.service.execute(self.interaction, request)
        self.assertIn("2개", result)
        self.assertFalse(self.interaction.channel.purge.call_args.kwargs["bulk"])
        self.assertEqual(self.interaction.channel.purge.call_args.kwargs["limit"], 100)
        with self.assertRaises(admin.AdminActionError):
            await self.request("cleanup", value=51)

    async def test_channel_override_and_history_permission_checked(self):
        self.interaction.channel.permissions_for = lambda item: permissions(manage_messages=False)
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request("cleanup", value=1)
        self.assertEqual(caught.exception.code, "bot_permission_missing")
        self.interaction.channel.permissions_for = lambda item: permissions(read_message_history=False)
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request("cleanup", value=1)
        self.assertEqual(caught.exception.code, "bot_history_permission_missing")

    async def test_slowmode_set_reset_and_channel_binding(self):
        for value in (30, 0):
            request = await self.request("slowmode", target_id=None, value=value)
            await self.service.execute(self.interaction, request)
            self.interaction.channel.edit.assert_awaited_with(slowmode_delay=value, reason="Doribot slowmode; actor=10")
        request = await self.request("slowmode", target_id=None, value=30)
        self.interaction.channel.id = 201
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.service.execute(self.interaction, request)
        self.assertEqual(caught.exception.code, "channel_mismatch")

    async def test_thread_slowmode_requires_manage_threads_for_actor_and_bot(self):
        channel = MagicMock(spec=discord.Thread)
        channel.id, channel.guild = 201, self.interaction.guild
        channel.permissions_for = lambda item: item.guild_permissions
        channel.edit = AsyncMock()
        self.interaction.channel = channel
        self.interaction.user = self.members[SPECIAL]
        self.members[99].guild_permissions.manage_threads = False
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request("slowmode", target_id=None, value=30)
        self.assertEqual(caught.exception.code, "bot_permission_missing")
        self.members[99].guild_permissions.manage_threads = True
        self.members[SPECIAL].guild_permissions.manage_threads = False
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.request("slowmode", target_id=None, value=30)
        self.assertEqual(caught.exception.code, "actor_permission_missing")
        self.members[SPECIAL].guild_permissions.manage_threads = True
        self.members[99].guild_permissions.manage_channels = False
        self.members[SPECIAL].guild_permissions.manage_channels = False
        request = await self.request("slowmode", target_id=None, value=30)
        await self.service.execute(self.interaction, request)
        channel.edit.assert_awaited_once_with(slowmode_delay=30, reason=f"Doribot slowmode; actor={SPECIAL}")

    async def test_thread_manage_threads_is_rechecked_on_execution_for_actor_and_bot(self):
        channel = MagicMock(spec=discord.Thread)
        channel.id, channel.guild = 201, self.interaction.guild
        channel.permissions_for = lambda item: item.guild_permissions
        channel.edit = AsyncMock()
        self.interaction.channel = channel
        self.interaction.user = self.members[SPECIAL]
        for member_id, expected in ((99, "bot_permission_missing"), (SPECIAL, "actor_permission_missing")):
            request = await self.request("slowmode", target_id=None, value=30)
            self.members[member_id].guild_permissions.manage_threads = False
            with self.assertRaises(admin.AdminActionError) as caught:
                await self.service.execute(self.interaction, request)
            self.assertEqual(caught.exception.code, expected)
            self.members[member_id].guild_permissions.manage_threads = True
        channel.edit.assert_not_awaited()

    async def test_duplicate_and_concurrent_confirmation_execute_once(self):
        request = await self.request()
        results = await asyncio.gather(self.service.execute(self.interaction, request),
                                       self.service.execute(self.interaction, request), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, admin.AdminActionError) for result in results), 1)
        self.target.timeout.assert_awaited_once()

    async def test_forged_request_or_other_actor_cannot_claim_confirmation(self):
        request = await self.request()
        with self.assertRaises(admin.AdminActionError):
            await self.service.execute(self.interaction, replace(request, value=60))
        self.interaction.user = self.members[SPECIAL]
        with self.assertRaises(admin.AdminActionError):
            await self.service.execute(self.interaction, request)
        self.interaction.user = self.members[10]
        await self.service.execute(self.interaction, request)
        self.target.timeout.assert_awaited_once()

    async def test_expired_confirmation_and_restart_cannot_execute(self):
        request = await self.request()
        with patch.object(admin, "monotonic", return_value=request.created + 121):
            with self.assertRaises(admin.AdminActionError) as caught:
                await self.service.execute(self.interaction, request)
        self.assertEqual(caught.exception.code, "confirmation_expired")
        with self.assertRaises(admin.AdminActionError):
            await admin.AdminService().execute(self.interaction, request)
        self.target.timeout.assert_not_awaited()

    async def test_discord_errors_are_safe_and_audit_has_only_metadata(self):
        secret = "SECRET nickname prompt reason"
        self.target.timeout.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), secret)
        request = await self.request(reason=secret)
        with self.assertRaises(admin.AdminActionError) as caught:
            await self.service.execute(self.interaction, request)
        self.assertEqual(caught.exception.code, "discord_forbidden")
        self.assertNotIn(secret, caught.exception.message)
        self.assertNotIn(secret, repr(self.service.audit))
        self.assertEqual(set(self.service.audit[0]), {"at", "actor_id", "guild_id", "target_id", "action", "result"})
        self.assertIn("재시작 시 초기화", self.service.recent_log_text(100))
        self.assertNotIn("discord_forbidden", self.service.recent_log_text(101))

    async def test_audit_is_bounded_and_log_delivery_failure_does_not_reexecute(self):
        self.service = admin.AdminService(audit_limit=2)
        os.environ["MOD_LOG_CHANNEL_ID"] = "123"
        channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("SECRET")))
        self.interaction.guild.get_channel = lambda channel_id: channel
        with self.assertLogs(level="WARNING") as logs:
            for _ in range(3):
                await self.service.execute(self.interaction, await self.request())
        self.assertEqual(len(self.service.audit), 2)
        self.assertEqual(self.target.timeout.await_count, 3)
        self.assertNotIn("SECRET", " ".join(logs.output))

    async def test_user_select_modal_confirm_end_to_end(self):
        selector = admin.AdminMemberSelectView(self.interaction, self.service)
        self.interaction.data = {"values": ["20"]}
        await selector.select_member(self.interaction)
        actions = self.interaction.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(actions, admin.AdminMemberActionsView)
        self.interaction.data = {"values": ["nickname"]}
        await actions.select_action(self.interaction)
        modal = self.interaction.response.send_modal.call_args.args[0]
        modal.value_input = SimpleNamespace(value="새 별명")
        modal.reason_input = SimpleNamespace(value="운영 요청")
        await modal.on_submit(self.interaction)
        confirm = self.interaction.response.send_message.call_args.kwargs["view"]
        self.assertIsInstance(confirm, admin.AdminConfirmView)
        self.target.edit.assert_not_awaited()
        await confirm.confirm(self.interaction)
        self.target.edit.assert_awaited_once()
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs["ephemeral"])

    async def test_component_modal_and_confirmation_recheck_actor_guild_and_auth(self):
        request = await self.request()
        confirm = admin.AdminConfirmView(self.interaction, self.service, request)
        modal = admin.AdminActionModal(self.interaction, self.service, "timeout", target_id=20, value=5)
        modal.reason_input = SimpleNamespace(value="reason")
        self.interaction.user = self.members[SPECIAL]
        await confirm.confirm(self.interaction)
        await modal.on_submit(self.interaction)
        self.target.timeout.assert_not_awaited()
        self.interaction.user = self.members[10]
        self.interaction.guild.id = 101
        await confirm.confirm(self.interaction)
        self.target.timeout.assert_not_awaited()

    async def test_cancel_invalidates_confirmation_and_timeout_presets_are_visible(self):
        request = await self.request()
        confirm = admin.AdminConfirmView(self.interaction, self.service, request)
        await confirm.cancel(self.interaction)
        with self.assertRaises(admin.AdminActionError):
            await self.service.execute(self.interaction, request)
        view = admin.AdminTimeoutView(self.interaction, self.service, 20)
        self.assertEqual([int(option.value) for option in view.children[0].options], list(admin.TIMEOUT_MINUTES))

    async def test_actual_admin_routes_are_private_and_unrelated_actions_fall_through(self):
        for action in ("admin:members", "admin:channel", "admin:logs", "admin:settings"):
            self.assertTrue(await admin.handle_admin_action(self.interaction, action, status_text="상태"))
            self.assertTrue(self.interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.assertFalse(await admin.handle_admin_action(self.interaction, "admin:rules"))


if __name__ == "__main__":
    unittest.main()
