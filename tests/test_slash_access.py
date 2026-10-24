"""Self-diagnostics keep Discord command access separate from bot authorization."""

import asyncio
import copy
import datetime
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

import slash_access
from slash_access import slash_access_report, slash_access_report_with_refresh


USER_ID = 491239880005910538
GUILD_ID = 802904099816472616
CHANNEL_ID = 802904099816472619


class SlashAccessTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "SPECIAL_ADMIN_USER_ID": "855749166764654653",
            "SPECIAL_ADMIN_USER_IDS": f"855749166764654653,{USER_ID}",
            "SPECIAL_ADMIN_GUILD_IDS": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=100, me=object())
        self.actor = SimpleNamespace(id=USER_ID, guild=self.guild, display_name="@everyone SECRET_NAME")
        self.actor_permissions = SimpleNamespace(view_channel=True, use_application_commands=True)
        self.bot_permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=True)
        self.channel = SimpleNamespace(id=CHANNEL_ID, guild=self.guild, type=SimpleNamespace(name="text"))
        self.channel.permissions_for = Mock(side_effect=lambda member: self.bot_permissions if member is self.guild.me else self.actor_permissions)

    def report(self):
        return slash_access_report(self.actor, self.guild, self.channel)

    def test_configured_member_needs_no_administrator_role(self):
        report = self.report()
        self.assertIn("내 애플리케이션 명령어 사용: 허용", report)
        self.assertIn("도리봇 /관리 인증: 허용", report)
        self.assertIn(f"서버 ID: {GUILD_ID}", report)
        self.assertIn(f"채널 ID: {CHANNEL_ID}", report)
        self.assertIn("관리자 권한이 필요하지 않습니다", report)
        self.assertIn("원격 게시 상태는 확인하지 못했습니다", report)
        self.assertIn("!ask", report)
        self.assertIn("!img", report)
        self.assertIn("!xp", report)
        self.assertNotIn("SECRET_NAME", report)
        self.assertNotIn("@everyone", report)
        self.assertNotIn("855749166764654653", report)
        self.assertNotIn(str(USER_ID), report)
        self.assertNotIn("!관리", report)
        self.assertLess(len(report), 1000)

    def test_discord_denial_is_separate_from_special_admin_authentication(self):
        self.actor_permissions.use_application_commands = False
        report = self.report()
        self.assertIn("내 애플리케이션 명령어 사용: 거부", report)
        self.assertIn("도리봇 /관리 인증: 허용", report)
        self.assertIn("채널 편집 → 권한 → 고급 권한", report)
        self.assertIn("동기화된 카테고리", report)
        self.assertIn("역할·회원별", report)
        self.assertIn("앱·명령별 허용만으로 채널의 거부는 해제되지 않습니다", report)
        self.assertIn("Administrator는 채널 거부를 우회", report)

    def test_regular_member_and_administrator_role_do_not_get_panel_access(self):
        self.actor.id = 200
        self.actor.guild_permissions = SimpleNamespace(administrator=True)
        report = self.report()
        self.assertIn("내 애플리케이션 명령어 사용: 허용", report)
        self.assertIn("비허용 (special_id_mismatch)", report)

    def test_owner_authorized_without_special_configuration(self):
        self.guild.owner_id = USER_ID
        with patch.dict(os.environ, {"SPECIAL_ADMIN_USER_IDS": "SECRET_INVALID", "SPECIAL_ADMIN_USER_ID": ""}):
            self.assertIn("도리봇 /관리 인증: 허용", self.report())

    def test_malformed_configuration_fails_closed_without_dumping_values(self):
        with patch.dict(os.environ, {"SPECIAL_ADMIN_USER_IDS": "SECRET_INVALID"}):
            report = self.report()
        self.assertIn("비허용 (special_id_invalid)", report)
        self.assertNotIn("SECRET_INVALID", report)
        self.assertIn("내 애플리케이션 명령어 사용: 허용", report)

    def test_guild_scope_denied_is_reported_independently(self):
        with patch.dict(os.environ, {"SPECIAL_ADMIN_GUILD_IDS": "500"}):
            self.assertIn("비허용 (guild_scope_denied)", self.report())

    def test_dm_returns_server_only_hint(self):
        report = slash_access_report(self.actor, None, self.channel)
        self.assertIn("DM에서는", report)
        self.assertIn("!명령진단", report)
        self.channel.permissions_for.assert_not_called()

    def test_other_server_member_or_channel_cannot_be_inspected(self):
        for target in (self.actor, self.channel):
            with self.subTest(target="actor" if target is self.actor else "channel"):
                original = target.guild
                target.guild = SimpleNamespace(id=500)
                self.channel.permissions_for.reset_mock()
                report = self.report()
                self.assertIn("확인 불가 (member_or_channel_unavailable)", report)
                self.assertIn("내 애플리케이션 명령어 사용: 확인 불가", report)
                self.channel.permissions_for.assert_not_called()
                target.guild = original

    def test_invalid_snowflakes_are_not_echoed_or_accepted(self):
        for invalid in (True, 0, -1, 2**64, "SECRET_ID"):
            with self.subTest(invalid=invalid):
                self.guild.id = invalid
                self.channel.id = invalid
                report = self.report()
                self.assertIn("서버 ID: 확인 불가", report)
                self.assertIn("채널 ID: 확인 불가", report)
                self.assertIn("내 채널 보기: 확인 불가", report)
                self.assertNotIn("SECRET_ID", report)

    def test_absent_permissions_do_not_become_allowed(self):
        self.actor_permissions = SimpleNamespace(view_channel=1)
        self.guild.me = None
        report = self.report()
        self.assertIn("내 채널 보기: 확인 불가", report)
        self.assertIn("내 애플리케이션 명령어 사용: 확인 불가", report)
        self.assertIn("봇 채널 보기: 확인 불가 · 메시지 전송: 확인 불가", report)

    def test_permission_lookup_failure_is_unknown_and_does_not_leak_error(self):
        self.channel.permissions_for.side_effect = RuntimeError("SECRET_PERMISSION_ERROR")
        report = self.report()
        self.assertIn("내 채널 보기: 확인 불가", report)
        self.assertIn("내 애플리케이션 명령어 사용: 확인 불가", report)
        self.assertNotIn("SECRET_PERMISSION_ERROR", report)
        self.assertIn("도리봇 /관리 인증: 허용", report)

    def test_bot_permission_denial_reports_real_channel_capability(self):
        self.bot_permissions.view_channel = False
        self.bot_permissions.send_messages = False
        report = self.report()
        self.assertIn("봇 채널 보기: 거부 · 메시지 전송: 거부", report)
        self.assertIn("봇 보기·전송 권한", report)

    def test_threads_check_send_messages_in_threads(self):
        self.channel.type.name = "private_thread"
        self.bot_permissions.send_messages = True
        self.bot_permissions.send_messages_in_threads = False
        report = self.report()
        self.assertIn("봇 채널 보기: 허용 · 스레드 전송: 거부", report)
        self.bot_permissions.send_messages = False
        self.bot_permissions.send_messages_in_threads = True
        self.assertIn("스레드 전송: 허용", self.report())

    def test_authorization_failure_is_sanitized(self):
        with patch("slash_access.admin_authorization_reason", side_effect=RuntimeError("SECRET_AUTH_ERROR")):
            report = self.report()
        self.assertIn("비허용 (authorization_unavailable)", report)
        self.assertNotIn("SECRET_AUTH_ERROR", report)
        with patch("slash_access.admin_authorization_reason", return_value="SECRET_RAW_CONFIGURATION"):
            self.assertNotIn("SECRET_RAW_CONFIGURATION", self.report())


class RealSlashAccessPermissionTests(unittest.TestCase):
    """Exercise installed discord.py's role/member overwrite resolution offline."""

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "SPECIAL_ADMIN_USER_ID": "855749166764654653",
            "SPECIAL_ADMIN_USER_IDS": f"855749166764654653,{USER_ID}",
            "SPECIAL_ADMIN_GUILD_IDS": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = discord.Client(intents=discord.Intents.none())
        self.state = self.client._connection
        self.member_role_id = GUILD_ID + 1
        self.admin_role_id = GUILD_ID + 2
        self.base = discord.Permissions(
            view_channel=True, send_messages=True, read_message_history=True,
            use_application_commands=True, attach_files=True,
        )
        self.guild = discord.Guild(state=self.state, data={
            "id": str(GUILD_ID), "owner_id": "100", "name": "Offline test guild",
            "roles": [
                {"id": str(GUILD_ID), "name": "@everyone", "permissions": str(self.base.value)},
                {"id": str(self.member_role_id), "name": "Member", "permissions": "0"},
                {"id": str(self.admin_role_id), "name": "Admin", "permissions": "8"},
            ],
        })
        self.actor = self.member(USER_ID, [self.member_role_id])
        self.administrator = self.member(200, [self.member_role_id, self.admin_role_id])
        self.owner = self.member(100, [self.member_role_id])
        self.channel_data = {
            "id": str(CHANNEL_ID), "type": 0, "name": "offline-test", "position": 0,
            "permission_overwrites": [
                self.overwrite(GUILD_ID, 0, use_application_commands=False, attach_files=False),
                self.overwrite(self.member_role_id, 0, use_application_commands=False, manage_messages=False),
            ],
        }
        self.channel = discord.TextChannel(state=self.state, guild=self.guild, data=self.channel_data)

    def member(self, user_id, roles):
        return discord.Member(state=self.state, guild=self.guild, data={
            "user": {"id": str(user_id), "username": "Offline member", "discriminator": "0", "avatar": None},
            "roles": [str(role_id) for role_id in roles], "flags": 0,
        })

    @staticmethod
    def overwrite(target_id, target_type, **permissions):
        allow, deny = discord.PermissionOverwrite(**permissions).pair()
        return {"id": str(target_id), "type": target_type, "allow": str(allow.value), "deny": str(deny.value)}

    def test_configured_admin_id_and_base_permission_do_not_bypass_channel_denials(self):
        self.assertTrue(self.actor.guild_permissions.use_application_commands)
        self.assertFalse(self.actor.guild_permissions.administrator)
        self.assertFalse(self.channel.permissions_for(self.actor).use_application_commands)
        report = slash_access_report(self.actor, self.guild, self.channel)
        self.assertIn("내 애플리케이션 명령어 사용: 거부", report)
        self.assertIn("도리봇 /관리 인증: 허용", report)
        self.assertIn("채널 편집 → 권한 → 고급 권한", report)

    def test_clearing_only_application_command_denials_preserves_other_permissions(self):
        before = self.channel.permissions_for(self.actor)
        command_bit = discord.Permissions(use_application_commands=True).value
        for overwrite in self.channel_data["permission_overwrites"]:
            overwrite["deny"] = str(int(overwrite["deny"]) & ~command_bit)
        repaired = discord.TextChannel(state=self.state, guild=self.guild, data=self.channel_data)
        after = repaired.permissions_for(self.actor)
        self.assertFalse(before.use_application_commands)
        self.assertTrue(after.use_application_commands)
        self.assertEqual(before.value ^ after.value, command_bit)
        self.assertTrue(after.view_channel)
        self.assertTrue(after.send_messages)
        self.assertFalse(after.attach_files)
        self.assertFalse(after.manage_messages)
        report = slash_access_report(self.actor, self.guild, repaired)
        self.assertIn("내 애플리케이션 명령어 사용: 허용", report)
        self.assertIn("도리봇 /관리 인증: 허용", report)
        self.assertNotIn("채널의 거부는 해제되지 않습니다", report)

    def test_clearing_everyone_denial_alone_leaves_member_role_denial_effective(self):
        command_bit = discord.Permissions(use_application_commands=True).value
        everyone = self.channel_data["permission_overwrites"][0]
        everyone["deny"] = str(int(everyone["deny"]) & ~command_bit)
        channel = discord.TextChannel(state=self.state, guild=self.guild, data=self.channel_data)
        self.assertFalse(channel.permissions_for(self.actor).use_application_commands)

    def test_administrator_and_owner_bypass_channel_denials_without_granting_bot_auth(self):
        for member in (self.administrator, self.owner):
            with self.subTest(member_id=member.id):
                self.assertTrue(self.channel.permissions_for(member).use_application_commands)
                report = slash_access_report(member, self.guild, self.channel)
                self.assertIn("내 애플리케이션 명령어 사용: 허용", report)
                if member is self.administrator:
                    self.assertIn("비허용 (special_id_mismatch)", report)
                else:
                    self.assertIn("도리봇 /관리 인증: 허용", report)

    def test_member_specific_denial_overrides_restored_role_command_permission(self):
        self.channel_data["permission_overwrites"] = [
            self.overwrite(self.member_role_id, 0, use_application_commands=True),
            self.overwrite(USER_ID, 1, use_application_commands=False),
        ]
        channel = discord.TextChannel(state=self.state, guild=self.guild, data=self.channel_data)
        self.assertFalse(channel.permissions_for(self.actor).use_application_commands)
        self.assertIn("역할·회원별", slash_access_report(self.actor, self.guild, channel))


class FreshSlashAccessTests(unittest.IsolatedAsyncioTestCase):
    """Real discord.py snapshots, with only the three REST transports mocked."""

    member = RealSlashAccessPermissionTests.member
    overwrite = staticmethod(RealSlashAccessPermissionTests.overwrite)

    def setUp(self):
        RealSlashAccessPermissionTests.setUp(self)
        self.fresh_roles = [copy.copy(role) for role in self.guild.roles]
        self.fresh_member = self.member(USER_ID, [self.member_role_id])
        self.fresh_channel = discord.TextChannel(state=self.state, guild=self.guild, data={
            **self.channel_data, "permission_overwrites": [],
        })
        self.roles = AsyncMock(side_effect=lambda: self.fresh_roles)
        self.member_fetch = AsyncMock(side_effect=lambda _id: self.fresh_member)
        self.channel_fetch = AsyncMock(side_effect=lambda _id: self.fresh_channel)
        for name, mock in (("fetch_roles", self.roles), ("fetch_member", self.member_fetch), ("fetch_channel", self.channel_fetch)):
            patcher = patch.object(discord.Guild, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        slot_patch = patch("slash_access._REFRESH_SLOTS", asyncio.Semaphore(2))
        slot_patch.start()
        self.addCleanup(slot_patch.stop)

    async def report(self):
        return await slash_access_report_with_refresh(self.actor, self.guild, self.channel)

    async def test_fresh_allow_compares_cached_denial_without_mutating_permission_caches(self):
        original_roles = self.guild._roles
        original_overwrites = self.channel._overwrites
        original_member_guild = self.fresh_member.guild
        report = await self.report()
        self.assertIn("캐시 · 내 애플리케이션 명령어 사용: 거부", report)
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 허용", report)
        self.assertIn("캐시와 새 조회가 다릅니다", report)
        self.assertIn("명령 목록·실행 복구를 보장하지 않습니다", report)
        self.assertNotIn("거부를 확인하고 해제해야", report)
        self.assertIn("진단 v2 · 검사 시각", report)
        self.assertIn("KST", report)
        self.assertIn("서버 기본 허용", report)
        self.assertIs(self.guild._roles, original_roles)
        self.assertIs(self.channel._overwrites, original_overwrites)
        self.assertIs(self.fresh_member.guild, original_member_guild)
        self.assertFalse(self.channel.permissions_for(self.actor).use_application_commands)
        self.member_fetch.assert_awaited_once_with(USER_ID)
        self.channel_fetch.assert_awaited_once_with(CHANNEL_ID)
        self.assertNotIn(str(USER_ID), report)
        self.assertNotIn("Offline member", report)
        self.assertLess(len(report), 2000)

    async def test_rest_role_values_replace_stale_server_base_in_snapshot_only(self):
        original_base = self.guild.default_role.permissions.value
        for role in self.fresh_roles:
            if role.id == self.guild.id:
                role._permissions &= ~discord.Permissions(use_application_commands=True).value
        report = await self.report()
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 거부", report)
        self.assertIn("서버 기본 거부", report)
        self.assertEqual(self.guild.default_role.permissions.value, original_base)
        self.assertNotIn("캐시와 새 조회가 다릅니다", report)
        self.assertNotIn("거부를 확인하고 해제해야", report)
        self.assertIn("위 새 조회의 권한 단계", report)

    async def test_fresh_member_overwrite_denial_is_separate_from_panel_auth(self):
        self.fresh_channel = discord.TextChannel(state=self.state, guild=self.guild, data={
            **self.channel_data,
            "permission_overwrites": [self.overwrite(USER_ID, 1, use_application_commands=False)],
        })
        report = await self.report()
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 거부", report)
        self.assertIn("회원 우선 거부", report)
        self.assertIn("도리봇 /관리 인증: 허용", report)

    async def test_fresh_view_denial_explains_implicit_command_mask(self):
        self.fresh_channel = discord.TextChannel(state=self.state, guild=self.guild, data={
            **self.channel_data,
            "permission_overwrites": [self.overwrite(USER_ID, 1, view_channel=False)],
        })
        report = await self.report()
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 거부", report)
        self.assertIn("채널 보기 거부", report)
        self.assertIn("서버 기본 허용", report)
        self.assertNotIn("거부를 확인하고 해제해야", report)
        self.assertIn("위 새 조회의 권한 단계", report)

    async def test_fresh_roles_and_timeout_replace_stale_member_data(self):
        self.fresh_member._roles = discord.utils.SnowflakeList([])
        self.fresh_member.timed_out_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
        report = await self.report()
        self.assertIn("타임아웃 적용 중", report)
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 거부", report)
        self.assertFalse(self.actor.is_timed_out())
        self.assertNotIn("거부를 확인하고 해제해야", report)
        self.assertIn("위 새 조회의 권한 단계", report)

    async def test_fresh_member_role_assignment_is_used_instead_of_cached_assignment(self):
        self.fresh_member._roles = discord.utils.SnowflakeList([self.admin_role_id])
        self.fresh_channel = self.channel
        report = await self.report()
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 허용", report)
        self.assertIn("새 조회 근거: Administrator 권한", report)
        self.assertEqual(list(self.actor._roles), [self.member_role_id])

    async def test_current_scope_is_validated_before_any_http(self):
        for target in (self.actor, self.channel):
            original_guild = target.guild
            target.guild = SimpleNamespace(id=500)
            report = await self.report()
            self.assertIn("member_or_channel_unavailable", report)
            target.guild = original_guild
        self.roles.assert_not_awaited()
        self.member_fetch.assert_not_awaited()
        self.channel_fetch.assert_not_awaited()

    async def test_cache_allow_does_not_fetch(self):
        self.channel = self.fresh_channel
        report = await self.report()
        self.assertIn("내 애플리케이션 명령어 사용: 허용", report)
        self.assertNotIn("Discord 새 조회", report)
        self.roles.assert_not_awaited()

    async def test_rest_scope_mismatch_is_unknown_and_never_applied(self):
        self.fresh_member = self.member(123, [self.member_role_id])
        report = await self.report()
        self.assertIn("확인 불가 (snapshot_scope_invalid)", report)
        self.assertNotIn("Discord 새 조회 · 내 앱 명령 사용: 허용", report)

    async def test_missing_role_evidence_is_unknown(self):
        self.fresh_roles = [role for role in self.fresh_roles if role.id != self.member_role_id]
        report = await self.report()
        self.assertIn("확인 불가 (snapshot_roles_incomplete)", report)

    async def test_failures_are_sanitized_and_pending_requests_are_cancelled_on_timeout(self):
        self.roles.side_effect = RuntimeError("SECRET_API_BODY")
        report = await self.report()
        self.assertIn("확인 불가 (refresh_failed)", report)
        self.assertNotIn("SECRET_API_BODY", report)
        self.assertNotIn("TOKEN", report)
        cancelled = asyncio.Event()
        async def slow_roles():
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        self.roles.side_effect = slow_roles
        with patch("slash_access._REFRESH_TIMEOUT_SECONDS", 0.01):
            report = await self.report()
        self.assertIn("확인 불가 (timeout)", report)
        self.assertTrue(cancelled.is_set())

    async def test_api_error_codes_are_fixed_and_do_not_expose_response_bodies(self):
        cases = ((discord.Forbidden, 403, "forbidden"),
                 (discord.NotFound, 404, "not_found"),
                 (discord.HTTPException, 500, "http_error"))
        for error_type, status, code in cases:
            self.roles.side_effect = error_type(SimpleNamespace(status=status, reason="SECRET_REASON"), "SECRET_BODY")
            report = await self.report()
            self.assertIn(f"확인 불가 ({code})", report)
            self.assertNotIn("SECRET", report)

    async def test_only_two_simultaneous_self_refreshes_enter_rest(self):
        active = high_water = 0
        async def roles():
            nonlocal active, high_water
            active += 1
            high_water = max(high_water, active)
            try:
                await asyncio.sleep(0.01)
                return self.fresh_roles
            finally:
                active -= 1
        self.roles.side_effect = roles
        await asyncio.gather(*(self.report() for _ in range(6)))
        self.assertEqual(high_water, 2)

    async def test_thread_fetches_and_uses_fresh_parent_instead_of_cached_parent(self):
        thread_id = CHANNEL_ID + 2
        cached_thread = discord.Thread(state=self.state, guild=self.guild, data={
            "id": str(thread_id), "parent_id": str(CHANNEL_ID), "owner_id": str(USER_ID),
            "name": "Offline thread", "type": 11, "message_count": 1, "member_count": 1,
            "thread_metadata": {"archived": False, "auto_archive_duration": 1440,
                                "archive_timestamp": "2026-10-24T00:00:00+00:00", "locked": False},
        })
        self.guild._channels[self.channel.id] = self.channel
        self.channel = cached_thread
        self.channel_fetch.side_effect = lambda value: cached_thread if value == thread_id else self.fresh_channel
        report = await self.report()
        self.assertIn("Discord 새 조회 · 내 앱 명령 사용: 허용", report)
        self.assertEqual(self.channel_fetch.await_count, 2)
        self.assertFalse(cached_thread.permissions_for(self.actor).use_application_commands)

    async def test_bot_identity_is_validated_without_author_identity_dump(self):
        bot_id = 900000000000000001
        self.state.user = SimpleNamespace(id=bot_id)
        self.guild._members[bot_id] = self.member(bot_id, [])
        report = await self.report()
        self.assertIn(f"응답 봇 ID: {bot_id}", report)
        self.assertNotIn(str(USER_ID), report)


if __name__ == "__main__":
    unittest.main()
