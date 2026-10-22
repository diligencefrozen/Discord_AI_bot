"""Real discord.py embeds with isolated message transports and permissions."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from deletion_audit import DeletionAuditService, build_deletion_embed, private_destination_reason, _text
from moderation_audit import capture_message_snapshot


def permissions(**values):
    result = {name: False for name in ("administrator", "manage_guild", "manage_messages", "view_channel", "send_messages", "embed_links")}
    result.update(values)
    return SimpleNamespace(**result)


def private_channel():
    everyone = SimpleNamespace(id=1, permissions=permissions(), tags=None)
    operator = SimpleNamespace(id=2, permissions=permissions(manage_messages=True), tags=None)
    bot = SimpleNamespace(id=99)
    guild = SimpleNamespace(id=1, name="test guild", owner_id=10, default_role=everyone, roles=[everyone, operator], me=bot)
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.guild, channel.overwrites = 20, guild, {}
    channel.send = AsyncMock()

    def channel_permissions(target):
        if target is everyone:
            return permissions()
        if target is bot:
            return permissions(view_channel=True, send_messages=True, embed_links=True)
        return permissions(view_channel=True)

    channel.permissions_for.side_effect = channel_permissions
    return channel


def message(content="시321133발", *, message_id=100):
    return SimpleNamespace(id=message_id, content=content,
                           author=SimpleNamespace(id=50, name="author", display_name="작성자", bot=False),
                           guild=SimpleNamespace(id=1, name="운영 서버"),
                           channel=SimpleNamespace(id=11, name="일반 채팅"),
                           attachments=[], stickers=[], embeds=[],
                           created_at=datetime(2026, 10, 22, tzinfo=timezone.utc), delete=AsyncMock())


def raw(message_id=100, **kwargs):
    values = {"guild_id": 1, "channel_id": 11, "message_id": message_id, "cached_message": None}
    values.update(kwargs)
    return SimpleNamespace(**values)


def field(embed, name):
    name = {"탐지 사유": "탐지 정보"}.get(name, name)
    return next(item.value for item in embed.fields if name in item.name)


class EmbedTests(unittest.TestCase):
    def test_original_obfuscation_is_separate_from_matched_root_and_kst_header(self):
        snapshot = capture_message_snapshot(message(), reason_code="profanity", detected_root="시발", delete_origin="bot")
        embed = build_deletion_embed(snapshot, deleted_at=datetime(2026, 10, 22, 10, 23, 45, tzinfo=timezone.utc))
        self.assertEqual(field(embed, "원문"), "시321133발")
        self.assertIn("시발", field(embed, "탐지 사유"))
        self.assertNotIn("시321133발", field(embed, "탐지 사유"))
        self.assertIn("2026-10-22 19:23:45 KST", embed.description)
        self.assertIn("작성자", embed.description)
        self.assertIn("ID 50", embed.description)
        self.assertIn("채널 ID 11", embed.description)
        self.assertIn("봇 직접 삭제", embed.description)
        self.assertIn("자동 검열 · 금칙어", embed.description)
        self.assertIn("100", embed.footer.text)
        self.assertEqual(embed.colour.value, 0xED4245)

    def test_attachment_only_and_uncached_original_are_distinct(self):
        target = message("")
        target.attachments = [SimpleNamespace(id=7, filename="photo.png", content_type="image/png", size=2048,
                                              width=640, height=480, url="https://cdn.discordapp.com/file", is_spoiler=lambda: True)]
        embed = build_deletion_embed(capture_message_snapshot(target))
        self.assertIn("본문 없음", field(embed, "원문"))
        attachment_text = field(embed, "첨부파일")
        for expected in ("photo.png", "image/png", "2.0 KB", "640×480", "스포일러 예", "image"):
            self.assertIn(expected, attachment_text)
        self.assertNotIn("https://", str(embed.to_dict()))
        del target.content
        self.assertIn("원문 확인 불가", field(build_deletion_embed(capture_message_snapshot(target)), "원문"))

    def test_emoji_sticker_gif_video_document_and_embed_metadata(self):
        target = message("안녕 😀 <:wave:1234> <a:dance:5678>")
        target.stickers = [SimpleNamespace(id=7, name="인사", format=SimpleNamespace(name="png"), url="https://media.discordapp.net/stickers/7.png")]
        target.attachments = [SimpleNamespace(id=index, filename=name, content_type=mime, size=1234, width=None, height=None,
                                              url="https://cdn.discordapp.com/file", is_spoiler=lambda: False)
                              for index, name, mime in ((1, "a.gif", "image/gif"), (2, "b.mp4", "video/mp4"), (3, "c.pdf", "application/pdf"))]
        target.embeds = [SimpleNamespace(type="link", title="링크 제목")]
        embed = build_deletion_embed(capture_message_snapshot(target))
        self.assertIn("😀", field(embed, "이모지"))
        self.assertIn("wave", field(embed, "이모지"))
        self.assertIn("움직이는", field(embed, "이모지"))
        self.assertIn("인사", field(embed, "스티커"))
        self.assertIn("ID 7", field(embed, "스티커"))
        self.assertIn("gif", field(embed, "첨부파일"))
        self.assertIn("video", field(embed, "첨부파일"))
        self.assertIn("document", field(embed, "첨부파일"))
        self.assertIn("링크 제목", field(embed, "임베드"))

    def test_user_markup_mentions_urls_and_code_fences_cannot_control_card(self):
        target = message("@everyone <@50> **fake** ```code``` [click](https://evil.test/) https://evil.test/a")
        target.author.display_name = "@everyone **fake**"
        embed = build_deletion_embed(capture_message_snapshot(target))
        rendered = str(embed.to_dict())
        self.assertNotIn("@everyone", rendered)
        self.assertNotIn("<@50>", rendered)
        self.assertNotIn("```", rendered)
        self.assertNotIn("https://evil.test", rendered)
        self.assertNotIn("**fake**", rendered)

    def test_extreme_metadata_is_bounded_with_omission_counts(self):
        target = message("긴 내용" * 5000)
        target.author.display_name = "작성자" * 1000
        target.guild.name = "서버" * 1000
        target.attachments = [SimpleNamespace(filename="파일" * 1000, content_type="x" * 1000, size=1000,
                                              url="https://cdn.discordapp.com/a", is_spoiler=lambda: False) for _ in range(40)]
        target.embeds = [SimpleNamespace(type="link", title="제목" * 1000) for _ in range(30)]
        embed = build_deletion_embed(capture_message_snapshot(target))
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertLessEqual(len(embed.fields), 25)
        self.assertTrue(all(len(item.value) <= 1024 for item in embed.fields))
        self.assertIn("40개", next(item.name for item in embed.fields if "첨부파일" in item.name))
        self.assertIn("표시 생략", field(embed, "첨부파일"))
        self.assertIn("일부 표시", field(embed, "원문"))

    def test_disabled_content_hides_all_user_metadata_bodies(self):
        target = message("secret message")
        target.attachments = [SimpleNamespace(filename="private_filename.txt", content_type="text/plain", size=1)]
        embed = build_deletion_embed(capture_message_snapshot(target, reason_code="profanity", detected_root="secret root", reason_detail="secret detail"), include_content=False)
        rendered = str(embed.to_dict())
        for secret in ("secret message", "private_filename", "secret root", "secret detail"):
            self.assertNotIn(secret, rendered)
        self.assertIn("비활성화", field(embed, "원문"))

    def test_long_plain_input_is_bounded_before_escaping_and_preserves_emoji_joiners(self):
        started = perf_counter()
        rendered = _text("a" * 12000, 960)
        self.assertLess(perf_counter() - started, 0.2)
        self.assertLessEqual(len(rendered), 960)
        self.assertIn("생략", rendered)
        self.assertIn("👩‍💻", _text("👩‍💻\u202ehello\x00"))
        self.assertNotIn("\u202e", _text("👩‍💻\u202ehello\x00"))
        self.assertNotIn("\x00", _text("👩‍💻\u202ehello\x00"))


class PrivacyTests(unittest.TestCase):
    def test_actual_private_text_channel_is_accepted(self):
        self.assertIsNone(private_destination_reason(private_channel(), 1))

    def test_dm_thread_and_foreign_guild_are_rejected(self):
        self.assertEqual(private_destination_reason(SimpleNamespace(), 1), "destination_not_text_channel")
        self.assertEqual(private_destination_reason(Mock(spec=discord.Thread), 1), "destination_not_text_channel")
        self.assertEqual(private_destination_reason(private_channel(), 2), "destination_guild_mismatch")

    def test_public_channel_is_rejected_even_if_bot_is_administrator(self):
        channel = private_channel()
        channel.permissions_for.return_value = permissions(administrator=True, view_channel=True, send_messages=True, embed_links=True)
        channel.permissions_for.side_effect = None
        self.assertEqual(private_destination_reason(channel, 1), "destination_public")

    def test_ordinary_role_can_view_private_channel_is_rejected(self):
        channel = private_channel()
        channel.guild.roles.append(SimpleNamespace(id=3, permissions=permissions(), tags=None))
        self.assertEqual(private_destination_reason(channel, 1), "destination_nonoperator_role")

    def test_member_overwrite_requires_owner_configured_admin_or_operator(self):
        channel = private_channel()
        member = Mock(spec=discord.Member)
        member.id, member.guild_permissions = 70, permissions()
        channel.overwrites = {member: discord.PermissionOverwrite(view_channel=True)}
        self.assertEqual(private_destination_reason(channel, 1), "destination_nonoperator_member")
        self.assertIsNone(private_destination_reason(channel, 1, {70}))
        member.id = channel.guild.owner_id
        self.assertIsNone(private_destination_reason(channel, 1))
        member.id, member.guild_permissions = 70, permissions(manage_guild=True)
        self.assertIsNone(private_destination_reason(channel, 1))

    def test_unknown_and_nonoperator_role_overwrites_fail_closed(self):
        channel = private_channel()
        role = Mock(spec=discord.Role)
        role.id, role.permissions, role.tags = 70, permissions(), None
        channel.overwrites = {role: discord.PermissionOverwrite(view_channel=True)}
        self.assertEqual(private_destination_reason(channel, 1), "destination_nonoperator_role")
        channel.overwrites = {object(): discord.PermissionOverwrite(view_channel=True)}
        self.assertEqual(private_destination_reason(channel, 1), "destination_unknown_overwrite")

    def test_missing_bot_permissions_and_permission_errors_fail_closed(self):
        channel = private_channel()
        original = channel.permissions_for.side_effect
        channel.permissions_for.side_effect = lambda target: permissions(view_channel=True) if target is channel.guild.me else original(target)
        self.assertEqual(private_destination_reason(channel, 1), "destination_bot_permissions")
        channel.permissions_for.side_effect = RuntimeError("private exception text")
        self.assertEqual(private_destination_reason(channel, 1), "destination_permissions_unavailable")

    def test_uncached_user_overwrite_allows_only_explicit_owner_bot_and_configured_ids(self):
        channel = private_channel()
        for user_id in (channel.guild.owner_id, channel.guild.me.id, 491239880005910538):
            channel.overwrites = {discord.Object(id=user_id, type=discord.User): discord.PermissionOverwrite(view_channel=True)}
            self.assertIsNone(private_destination_reason(channel, 1, {491239880005910538}))
        channel.overwrites = {discord.Object(id=123, type=discord.User): discord.PermissionOverwrite(view_channel=True)}
        self.assertEqual(private_destination_reason(channel, 1), "destination_unknown_overwrite")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = private_channel()
        self.service = DeletionAuditService({11: 20, 20: 20}, lambda _id: self.channel, configured_admin_ids=set())

    async def test_direct_delete_snapshots_before_http_and_deduplicates_gateway_race(self):
        target = message()

        async def delete():
            snapshot = self.service.cache.get(1, 100)
            self.assertEqual(snapshot.content, "시321133발")
            self.assertEqual(snapshot.reason.detected_root, "시발")
            await self.service.on_raw_message_delete(raw())

        target.delete.side_effect = delete
        self.assertTrue(await self.service.delete(target, reason="profanity", matched_rule="시발"))
        await self.service.on_message_delete(target)
        self.channel.send.assert_awaited_once()
        kwargs = self.channel.send.call_args.kwargs
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertEqual(field(kwargs["embed"], "원문"), "시321133발")
        self.assertIn("시발", field(kwargs["embed"], "탐지 사유"))

    async def test_successful_http_delete_audits_without_gateway_and_includes_detail(self):
        self.assertTrue(await self.service.delete(message(), reason="spam", detail="동일 메시지 3회 반복"))
        self.channel.send.assert_awaited_once()
        self.assertIn("동일 메시지 3회 반복", field(self.channel.send.call_args.kwargs["embed"], "탐지 사유"))

    async def test_forbidden_http_not_logged_as_deleted_and_body_is_absent_from_runtime_log(self):
        class Forbidden(Exception):
            status = 403
        target = message("sensitive-body")
        target.delete.side_effect = Forbidden("sensitive exception sensitive-body")
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            self.assertFalse(await self.service.delete(target, reason="profanity", matched_rule="secret-root"))
        self.channel.send.assert_not_awaited()
        self.assertNotIn("sensitive", "\n".join(captured.output))
        self.assertNotIn("secret-root", "\n".join(captured.output))

    async def test_failed_http_then_external_delete_never_inherits_bot_reason(self):
        class Forbidden(Exception):
            status = 403
        target = message()
        target.delete.side_effect = Forbidden()
        self.assertFalse(await self.service.delete(target, reason="profanity", matched_rule="시발"))
        await self.service.on_raw_message_delete(raw())
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertNotIn("봇 직접 삭제", embed.description)
        self.assertIn("외부 삭제", embed.description)
        self.assertNotIn("탐지 금칙어", field(embed, "탐지 사유"))

    async def test_gateway_before_failed_http_is_confirmed_external_and_never_false_bot_success(self):
        class Forbidden(Exception):
            status = 403
        target = message()

        async def delete():
            await self.service.on_message_delete(target)
            self.channel.send.assert_not_awaited()
            raise Forbidden()

        target.delete.side_effect = delete
        self.assertFalse(await self.service.delete(target, reason="profanity", matched_rule="시발"))
        self.channel.send.assert_awaited_once()
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertNotIn("봇 직접 삭제", embed.description)
        self.assertNotIn("탐지 금칙어", field(embed, "탐지 사유"))
        self.assertFalse(self.service._attempts)

    async def test_not_found_needs_independent_gateway_confirmation(self):
        class NotFound(Exception):
            status = 404
        target = message()
        target.delete.side_effect = NotFound()
        self.assertFalse(await self.service.delete(target, reason="profanity", matched_rule="시발"))
        self.channel.send.assert_not_awaited()
        await self.service.on_raw_message_delete(raw())
        self.channel.send.assert_awaited_once()

    async def test_external_cached_and_uncached_deletions_are_honest(self):
        self.service.remember(message())
        await self.service.on_raw_message_delete(raw())
        await self.service.on_raw_message_delete(raw(101))
        first, second = (call.kwargs["embed"] for call in self.channel.send.call_args_list)
        self.assertIn("외부 삭제", first.description)
        self.assertIn("실행자 확인 불가", first.description)
        self.assertEqual(field(first, "원문"), "시321133발")
        self.assertIn("원문 확인 불가", field(second, "원문"))
        self.assertIn("추정하지 않습니다", field(second, "탐지 사유"))

    async def test_bots_dm_unrouted_and_log_channel_recursion_are_ignored(self):
        targets = [message() for _ in range(4)]
        targets[0].author.bot = True
        targets[1].guild = None
        targets[2].channel.id = 33
        targets[3].channel.id = 20
        for target in targets:
            self.assertIsNone(self.service.remember(target))
            await self.service.on_message_delete(target)
        for payload in (raw(guild_id=None), raw(channel_id=33), raw(channel_id=20), raw(cached_message=targets[0])):
            await self.service.on_raw_message_delete(payload)
        self.channel.send.assert_not_awaited()

    async def test_audit_delivery_failure_never_prevents_enforcement_and_later_event_retries(self):
        self.channel.send.side_effect = [RuntimeError("private full response"), None]
        target = message()
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            self.assertTrue(await self.service.delete(target, reason="admin_cleanup"))
        self.assertNotIn("private full response", "\n".join(captured.output))
        self.assertFalse(self.service._inflight)
        await self.service.on_message_delete(target)
        await self.service.on_raw_message_delete(raw())
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_public_destination_never_receives_content_and_enforcement_continues(self):
        self.channel.permissions_for.side_effect = None
        self.channel.permissions_for.return_value = permissions(view_channel=True)
        self.assertTrue(await self.service.delete(message(), reason="profanity"))
        self.channel.send.assert_not_awaited()

    async def test_bulk_cached_raw_and_single_events_are_deduplicated(self):
        first, second = message(message_id=100), message("second", message_id=101)
        self.service.remember(second)
        payload = SimpleNamespace(guild_id=1, channel_id=11, message_ids={100, 101, 102}, cached_messages=[first])
        await self.service.on_raw_bulk_message_delete(payload)
        await self.service.on_raw_message_delete(raw(100))
        await self.service.on_message_delete(second)
        self.assertEqual(self.channel.send.await_count, 3)

    async def test_raw_edit_invalidates_stale_body_and_cached_edit_can_restore_current(self):
        self.service.remember(message("old body"))
        self.service.on_raw_message_edit(raw(data={"content": "new body"}))
        await self.service.on_raw_message_delete(raw())
        self.assertIn("원문 확인 불가", field(self.channel.send.call_args.kwargs["embed"], "원문"))
        updated = message("current body", message_id=101)
        self.service.remember(updated)
        await self.service.on_raw_message_delete(raw(101))
        self.assertEqual(field(self.channel.send.call_args.kwargs["embed"], "원문"), "current body")

    async def test_bounded_parallel_delivery_without_network_global_lock(self):
        active = peak = 0

        async def send(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.005)
            active -= 1

        self.channel.send.side_effect = send
        self.service = DeletionAuditService({11: 20}, lambda _id: self.channel, concurrency=2, configured_admin_ids=set())
        await asyncio.gather(*(self.service.on_message_delete(message(message_id=index)) for index in range(1, 9)))
        self.assertEqual(self.channel.send.await_count, 8)
        self.assertEqual(peak, 2)
        self.assertFalse(self.service._inflight)

    async def test_delivery_dedupe_ttl_and_lru_are_bounded(self):
        clock = [0]
        self.service = DeletionAuditService({11: 20}, lambda _id: self.channel, clock=lambda: clock[0], delivery_limit=2,
                                            delivery_ttl=10, configured_admin_ids=set())
        for index in range(1, 5):
            await self.service.on_message_delete(message(message_id=index))
        self.assertEqual(len(self.service._delivered), 2)
        await self.service.on_raw_message_delete(raw(4))
        self.assertEqual(self.channel.send.await_count, 4)
        clock[0] = 11
        await self.service.on_raw_message_delete(raw(4))
        self.assertEqual(self.channel.send.await_count, 5)

    async def test_snapshot_capture_failure_does_not_block_actual_delete(self):
        target = message()
        with patch.object(self.service.cache, "capture", side_effect=RuntimeError("private metadata")):
            self.assertTrue(await self.service.delete(target, reason="profanity"))
        target.delete.assert_awaited_once()

    async def test_pending_attempt_capacity_never_prevents_actual_deletion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.service = DeletionAuditService({11: 20}, lambda _id: self.channel, pending_limit=1, configured_admin_ids=set())
        first, second = message(), message(message_id=101)

        async def delayed_delete():
            entered.set()
            await release.wait()

        first.delete.side_effect = delayed_delete
        pending = asyncio.create_task(self.service.delete(first, reason="profanity"))
        await entered.wait()
        self.assertTrue(await self.service.delete(second, reason="profanity"))
        second.delete.assert_awaited_once()
        self.assertEqual(len(self.service._attempts), 1)
        self.assertNotEqual(self.service.cache.get(1, 101).delete_origin, "bot")
        release.set()
        self.assertTrue(await pending)

    async def test_duplicate_pending_direct_delete_does_not_repeat_http_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        target = message()

        async def delayed_delete():
            entered.set()
            await release.wait()

        target.delete.side_effect = delayed_delete
        pending = asyncio.create_task(self.service.delete(target, reason="profanity"))
        await entered.wait()
        self.assertFalse(await self.service.delete(target, reason="profanity"))
        target.delete.assert_awaited_once()
        release.set()
        self.assertTrue(await pending)

    async def test_raw_edit_while_http_pending_never_publishes_old_original(self):
        entered, release = asyncio.Event(), asyncio.Event()
        target = message("outdated original")

        async def delayed_delete():
            entered.set()
            await release.wait()

        target.delete.side_effect = delayed_delete
        pending = asyncio.create_task(self.service.delete(target, reason="profanity", matched_rule="시발"))
        await entered.wait()
        self.service.on_raw_message_edit(raw(data={"content": "untrusted raw update"}))
        release.set()
        self.assertTrue(await pending)
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertIn("원문 확인 불가", field(embed, "원문"))
        self.assertNotIn("outdated original", str(embed.to_dict()))

    async def test_reliable_edit_while_http_pending_restores_current_original(self):
        entered, release = asyncio.Event(), asyncio.Event()
        target = message("outdated original")

        async def delayed_delete():
            entered.set()
            await release.wait()

        target.delete.side_effect = delayed_delete
        pending = asyncio.create_task(self.service.delete(target, reason="profanity", matched_rule="시발"))
        await entered.wait()
        self.service.on_raw_message_edit(raw(data={"content": "raw update"}))
        self.service.remember(message("latest original"))
        release.set()
        self.assertTrue(await pending)
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertEqual(field(embed, "원문"), "latest original")
        self.assertIn("시발", field(embed, "탐지 사유"))

    async def test_event_observation_time_is_retained_while_delivery_waits_for_capacity(self):
        current = [datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)]
        self.service = DeletionAuditService({11: 20}, lambda _id: self.channel, concurrency=1,
                                            configured_admin_ids=set(), wall_clock=lambda: current[0])
        await self.service._semaphore.acquire()
        pending = asyncio.create_task(self.service.on_message_delete(message()))
        await asyncio.sleep(0)
        self.assertIn((1, 100), self.service._inflight)
        current[0] = datetime(2026, 10, 22, 10, 5, 0, tzinfo=timezone.utc)
        self.service._semaphore.release()
        await pending
        description = self.channel.send.call_args.kwargs["embed"].description
        self.assertIn("삭제 확인 시각", description)
        self.assertIn("2026-10-22 19:00:00 KST", description)
        self.assertNotIn("19:05:00", description)
        self.assertIn("봇 수신 기준", description)

    async def test_http_confirmation_time_is_retained_while_delivery_is_queued(self):
        current = [datetime(2026, 10, 22, 10, 1, 0, tzinfo=timezone.utc)]
        self.service = DeletionAuditService({11: 20}, lambda _id: self.channel, concurrency=1,
                                            configured_admin_ids=set(), wall_clock=lambda: current[0])
        await self.service._semaphore.acquire()
        pending = asyncio.create_task(self.service.delete(message(), reason="profanity"))
        await asyncio.sleep(0)
        self.assertIn((1, 100), self.service._inflight)
        current[0] = datetime(2026, 10, 22, 10, 6, 0, tzinfo=timezone.utc)
        self.service._semaphore.release()
        self.assertTrue(await pending)
        description = self.channel.send.call_args.kwargs["embed"].description
        self.assertIn("2026-10-22 19:01:00 KST", description)
        self.assertNotIn("19:06:00", description)

    async def test_gateway_before_failed_http_retains_first_independent_observation_time(self):
        class Forbidden(Exception):
            status = 403
        current = [datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)]
        self.service = DeletionAuditService({11: 20}, lambda _id: self.channel, configured_admin_ids=set(),
                                            wall_clock=lambda: current[0])
        target = message()

        async def delete():
            await self.service.on_message_delete(target)
            current[0] = datetime(2026, 10, 22, 10, 2, 0, tzinfo=timezone.utc)
            await self.service.on_raw_message_delete(raw())
            raise Forbidden()

        target.delete.side_effect = delete
        self.assertFalse(await self.service.delete(target, reason="profanity"))
        description = self.channel.send.call_args.kwargs["embed"].description
        self.assertIn("2026-10-22 19:00:00 KST", description)
        self.assertNotIn("19:02:00", description)
        self.assertNotIn("봇 직접 삭제", description)

    async def test_config_default_enabled_malformed_disabled_and_zero_disabled(self):
        for raw_value, expected in ((None, True), ("1", True), ("0", False), ("yes", False), ("", False)):
            with self.subTest(raw_value=raw_value), patch.dict("os.environ", {}, clear=True):
                if raw_value is not None:
                    with patch.dict("os.environ", {"DELETION_LOG_INCLUDE_CONTENT": raw_value}):
                        service = DeletionAuditService({11: 20}, lambda _id: self.channel)
                else:
                    service = DeletionAuditService({11: 20}, lambda _id: self.channel)
                self.assertEqual(service.include_content, expected)


if __name__ == "__main__":
    unittest.main()
