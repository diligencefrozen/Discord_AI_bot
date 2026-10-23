"""Transport regressions for the V7 public deletion incident.

These tests exercise the real audit service and real discord.py embeds.  A send
mock represents the Discord API boundary; passing them does not verify a live
Discord channel or Northflank deployment.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from deletion_audit import DeletionAuditService

try:
    from tests.test_deletion_permissions_v7 import permission_overwrite, real_channel_pair
except ModuleNotFoundError:
    from test_deletion_permissions_v7 import permission_overwrite, real_channel_pair


def _permissions(**overrides):
    values = dict(view_channel=False, send_messages=False, embed_links=False,
                  administrator=False, manage_guild=False, manage_messages=False)
    values.update(overrides)
    return SimpleNamespace(**values)


def public_pair(*, guild_id=1, source_id=11, destination_id=20):
    """Small effective-permission fixture; the permission-model suite uses REST models."""
    everyone = Mock(spec=discord.Role)
    everyone.id = guild_id
    everyone.permissions = _permissions(view_channel=True)
    bot = Mock(spec=discord.Member)
    bot.id, bot.roles = 99, [everyone]
    guild = SimpleNamespace(id=guild_id, name=f"guild-{guild_id}", owner_id=10,
                            default_role=everyone, roles=[everyone], me=bot,
                            get_member=lambda member_id: bot if member_id == bot.id else None)
    bot.guild = guild
    source, destination = (Mock(spec=discord.TextChannel) for _ in range(2))
    for channel, channel_id in ((source, source_id), (destination, destination_id)):
        channel.id, channel.name, channel.guild = channel_id, f"channel-{channel_id}", guild
        channel.overwrites = {}
        channel.send = AsyncMock()

        def permissions_for(target, channel=channel):
            if target is bot:
                return _permissions(view_channel=True, send_messages=True, embed_links=True)
            overwrite = channel.overwrites.get(target)
            view = everyone.permissions.view_channel
            default = channel.overwrites.get(everyone)
            if default is not None and default.view_channel is not None:
                view = default.view_channel
            if overwrite is not None and overwrite.view_channel is not None:
                view = overwrite.view_channel
            return _permissions(view_channel=view)

        channel.permissions_for.side_effect = permissions_for
    guild.text_channels = [source, destination]
    return source, destination


def make_message(source, content="공개 원문 😀", *, message_id=100):
    return SimpleNamespace(id=message_id, content=content, channel=source, guild=source.guild,
                           author=SimpleNamespace(id=50, name="author", display_name="작성자", bot=False),
                           attachments=[], stickers=[], embeds=[],
                           created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
                           delete=AsyncMock())


def raw(source, message_id=100, **overrides):
    values = dict(guild_id=source.guild.id, channel_id=source.id,
                  message_id=message_id, cached_message=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def embed_field(embed, needle):
    return next(item.value for item in embed.fields if needle in item.name)


class V7PublicDeletionPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source, self.destination = public_pair()
        self.channels = {self.source.id: self.source, self.destination.id: self.destination}
        self.service = self.new_service()

    def new_service(self, **kwargs):
        defaults = dict(enabled=True, include_content=True, all_guild_channels=True)
        defaults.update(kwargs)
        return DeletionAuditService({self.source.id: self.destination.id}, self.channels.get, **defaults)

    def assert_counts(self, *, full=0, metadata=0, restricted=0, failed=0):
        status = self.service.diagnostics(self.source.guild.id)
        self.assertEqual(status["sent_full"], full)
        self.assertEqual(status["sent_metadata_only"], metadata)
        self.assertEqual(status["sent_restricted"], restricted)
        self.assertEqual(status["failed"], failed)
        return status

    async def test_t01_public_delete_sends_original_author_channel_and_full_outcome(self):
        await self.service.on_message_delete(make_message(self.source))
        self.destination.send.assert_awaited_once()
        kwargs = self.destination.send.call_args.kwargs
        embed = kwargs["embed"]
        self.assertEqual(embed_field(embed, "원문"), "공개 원문 😀")
        self.assertIn("작성자", embed.description)
        self.assertIn("50", embed.description)
        self.assertIn(str(self.source.id), embed.description)
        self.assertIn("KST", embed.description)
        self.assertNotIn("열람 범위 제한", embed.title)
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        status = self.assert_counts(full=1)
        self.assertIsNotNone(status["last_full"])

    async def test_public_missing_role_cache_non_view_overwrite_sends_full_not_restricted(self):
        # discord.py returns Object(type=Role), rather than Role, for this
        # uncached overwrite target.  The old public evaluator interpreted it
        # as a bad member overwrite and returned source_permissions_unavailable.
        source, destination, guild = real_channel_pair(source_overwrites=[
            permission_overwrite(404, send_messages=False)])
        target = next(iter(source.overwrites))
        self.assertIsInstance(target, discord.Object)
        self.assertIs(target.type, discord.Role)
        self.assertIsNone(guild.get_role(404))
        channels = {source.id: source, destination.id: destination}
        service = DeletionAuditService({source.id: destination.id}, channels.get,
                                       enabled=True, include_content=True)
        with patch.object(discord.TextChannel, "send", new_callable=AsyncMock) as send:
            await service.on_message_delete(make_message(source, "실물 권한 모델 공개 원문"))
        send.assert_awaited_once()
        embed = send.call_args.kwargs["embed"]
        self.assertEqual(embed_field(embed, "원문"), "실물 권한 모델 공개 원문")
        self.assertNotIn("열람 범위 제한", embed.title)
        status = service.diagnostics(guild)
        self.assertEqual(status["sent_full"], 1)
        self.assertEqual(status["sent_restricted"], 0)

    async def test_t02_real_category_synchronized_public_source_sends_full(self):
        source, destination, guild = real_channel_pair(category_sync=True)
        self.assertTrue(source.permissions_synced)
        channels = {source.id: source, destination.id: destination}
        service = DeletionAuditService({source.id: destination.id}, channels.get,
                                       enabled=True, include_content=True)
        with patch.object(discord.TextChannel, "send", new_callable=AsyncMock) as send:
            await service.on_message_delete(make_message(source, "카테고리 동기화 공개 원문"))
        send.assert_awaited_once()
        self.assertEqual(embed_field(send.call_args.kwargs["embed"], "원문"), "카테고리 동기화 공개 원문")
        self.assertEqual(service.diagnostics(guild)["sent_full"], 1)

    async def test_public_uncached_member_abc_user_overwrite_sends_full(self):
        source, destination, guild = real_channel_pair(source_overwrites=[
            permission_overwrite(44, role=False, view_channel=True)])
        target = next(iter(source.overwrites))
        self.assertIsInstance(target, discord.Object)
        self.assertIs(target.type, discord.abc.User)
        self.assertIsNone(guild.get_member(44))
        channels = {source.id: source, destination.id: destination}
        service = DeletionAuditService({source.id: destination.id}, channels.get,
                                       enabled=True, include_content=True)
        with patch.object(discord.TextChannel, "send", new_callable=AsyncMock) as send:
            await service.on_message_delete(make_message(source, "회원 캐시 없는 공개 원문"))
        send.assert_awaited_once()
        self.assertEqual(embed_field(send.call_args.kwargs["embed"], "원문"), "회원 캐시 없는 공개 원문")
        self.assertEqual(service.diagnostics(guild)["sent_full"], 1)
        self.assertEqual(service.diagnostics(guild)["sent_restricted"], 0)

    async def test_t04_missing_guild_me_cache_rest_member_recovery_still_sends_full(self):
        source, destination, guild = real_channel_pair(cache_bot=False)
        self.assertIsNone(guild.me)
        member = discord.Member(data={
            "user": {"id": "99", "username": "doribot", "discriminator": "0", "avatar": None, "bot": True},
            "roles": ["2"], "flags": 0}, guild=guild, state=guild._state)
        fetch_member = AsyncMock(return_value=member)
        channels = {source.id: source, destination.id: destination}
        service = DeletionAuditService({source.id: destination.id}, channels.get,
                                       enabled=True, include_content=True, bot_member_fetcher=fetch_member)
        with patch.object(discord.TextChannel, "send", new_callable=AsyncMock) as send:
            await service.on_message_delete(make_message(source, "Guild.me 복구 공개 원문"))
        fetch_member.assert_awaited_once_with(guild)
        send.assert_awaited_once()
        self.assertEqual(embed_field(send.call_args.kwargs["embed"], "원문"), "Guild.me 복구 공개 원문")
        self.assertEqual(service.diagnostics(guild)["sent_full"], 1)
        self.assertEqual(service.diagnostics(guild)["sent_restricted"], 0)

    async def test_t06_moderation_delete_race_preserves_original_reason_and_one_full_send(self):
        target = make_message(self.source, "시321133발")

        async def gateway_during_http():
            await self.service.on_raw_message_delete(raw(self.source))
            self.destination.send.assert_not_awaited()

        target.delete.side_effect = gateway_during_http
        self.assertTrue(await self.service.delete(target, reason="profanity", matched_rule="시발"))
        await self.service.on_message_delete(target)
        await self.service.on_raw_message_delete(raw(self.source))
        target.delete.assert_awaited_once()
        self.destination.send.assert_awaited_once()
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertEqual(embed_field(embed, "원문"), "시321133발")
        self.assertIn("시발", embed_field(embed, "탐지"))
        self.assertIn("봇 직접 삭제", embed.description)
        self.assertIn("금칙어", embed.description)
        self.assert_counts(full=1)

    async def test_t07_raw_edit_then_confirmed_edit_deletes_latest_body(self):
        old = make_message(self.source, "1")
        self.service.remember(old)
        edited_at = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
        self.service.on_raw_message_edit(raw(self.source, data={
            "content": "시발", "edited_timestamp": edited_at.isoformat()}))
        current = make_message(self.source, "시발")
        current.edited_at = edited_at
        self.service.remember(current)
        self.assertTrue(await self.service.delete(current, reason="profanity", matched_rule="시발",
                                                 trigger="message_edit", edited_at=edited_at))
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertEqual(embed_field(embed, "원문"), "시발")
        self.assertIn("수정 후 검열", embed.description)
        self.assertIn("시발", embed_field(embed, "탐지"))
        self.assert_counts(full=1)

    async def test_periodic_moderation_uses_same_detailed_delivery_path(self):
        self.assertTrue(await self.service.delete(make_message(self.source, "시321133발"),
                                                 reason="profanity", matched_rule="시발", trigger="history_scan"))
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertIn("정기 재점검", embed.description)
        self.assertEqual(embed_field(embed, "원문"), "시321133발")
        self.assert_counts(full=1)

    async def test_t08_media_and_emoji_survive_transport_without_cdn_url_disclosure(self):
        target = make_message(self.source, "😀 <a:dance:5678>")
        target.attachments = [SimpleNamespace(id=7, filename="animation.gif", content_type="image/gif", size=4096,
                                              width=32, height=32, url="https://cdn.discordapp.com/private-file",
                                              is_spoiler=lambda: False)]
        target.stickers = [SimpleNamespace(id=8, name="인사", format=SimpleNamespace(name="png"))]
        target.embeds = [SimpleNamespace(type="video", title="동영상")]
        await self.service.on_message_delete(target)
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertIn("animation.gif", embed_field(embed, "첨부"))
        self.assertIn("인사", embed_field(embed, "스티커"))
        self.assertIn("dance", embed_field(embed, "이모지"))
        self.assertIn("😀", embed_field(embed, "원문"))
        self.assertNotIn("https://cdn.discordapp.com/private-file", str(embed.to_dict()))
        self.assert_counts(full=1)

    async def test_t09_cached_then_raw_deletion_is_exactly_one_full_delivery(self):
        target = make_message(self.source)
        self.service.remember(target)
        await asyncio.gather(self.service.on_message_delete(target),
                             self.service.on_raw_message_delete(raw(self.source)))
        self.destination.send.assert_awaited_once()
        self.assert_counts(full=1)

    async def test_t10_uncached_raw_is_metadata_only_and_does_not_invent_body(self):
        await self.service.on_raw_message_delete(raw(self.source))
        self.destination.send.assert_awaited_once()
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertIn("원문 확인 불가", embed_field(embed, "원문"))
        self.assertNotIn("공개 원문", str(embed.to_dict()))
        self.assertNotIn("작성자 (ID: 50)", str(embed.to_dict()))
        self.assertIn("100", embed.footer.text)
        self.assert_counts(metadata=1)

    async def test_empty_attachment_message_is_full_not_missing_snapshot(self):
        target = make_message(self.source, "")
        target.attachments = [SimpleNamespace(id=1, filename="photo.png", content_type="image/png", size=100)]
        await self.service.on_message_delete(target)
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertIn("본문 없음", embed_field(embed, "원문"))
        self.assertNotIn("원문 확인 불가", embed_field(embed, "원문"))
        self.assertIn("photo.png", embed_field(embed, "첨부"))
        self.assert_counts(full=1)

    async def test_bulk_combines_cached_snapshot_and_unknown_without_duplicate_sends(self):
        first = make_message(self.source, "cached bulk", message_id=100)
        second = make_message(self.source, "snapshot bulk", message_id=101)
        self.service.remember(second)
        await self.service.on_raw_bulk_message_delete(SimpleNamespace(
            guild_id=self.source.guild.id, channel_id=self.source.id,
            message_ids={100, 101, 102}, cached_messages=[first]))
        await self.service.on_raw_message_delete(raw(self.source, 100))
        await self.service.on_message_delete(second)
        self.assertEqual(self.destination.send.await_count, 3)
        rendered = str([call.kwargs["embed"].to_dict() for call in self.destination.send.call_args_list])
        self.assertIn("cached bulk", rendered)
        self.assertIn("snapshot bulk", rendered)
        self.assertIn("원문 확인 불가", rendered)
        self.assert_counts(full=2, metadata=1)

    async def test_t14_destination_gateway_cache_miss_rest_recovers_full_delivery(self):
        self.channels.pop(self.destination.id)
        fetcher = AsyncMock(return_value=self.destination)
        self.service = self.new_service(channel_fetcher=fetcher)
        await self.service.on_message_delete(make_message(self.source))
        fetcher.assert_awaited_once_with(self.destination.id)
        self.destination.send.assert_awaited_once()
        self.assertEqual(embed_field(self.destination.send.call_args.kwargs["embed"], "원문"), "공개 원문 😀")
        self.assert_counts(full=1)

    async def test_source_and_destination_rest_recover_remembered_explicit_source(self):
        available = dict(self.channels)
        self.channels.clear()
        fetcher = AsyncMock(side_effect=lambda channel_id: available.get(channel_id))
        self.service = self.new_service(channel_fetcher=fetcher)
        target = make_message(self.source)
        self.assertIsNotNone(self.service.remember(target))
        await self.service.on_raw_message_delete(raw(self.source))
        self.assertEqual(set(call.args[0] for call in fetcher.await_args_list), set(available))
        self.destination.send.assert_awaited_once()
        self.assert_counts(full=1)

    async def test_t15_new_public_source_with_empty_resolver_finds_existing_guild_destination(self):
        new_source, _ = public_pair(guild_id=self.source.guild.id, source_id=12,
                                    destination_id=self.destination.id)
        # Keep one actual guild object so comparison is based on the same role model.
        new_source.guild = self.source.guild
        self.source.guild.text_channels.append(new_source)
        available = {new_source.id: new_source, self.destination.id: self.destination}
        self.channels.clear()
        fetcher = AsyncMock(side_effect=lambda channel_id: available.get(channel_id))
        self.service = self.new_service(channel_fetcher=fetcher)
        self.assertIsNotNone(self.service.remember(make_message(new_source, "new channel original")))
        await self.service.on_raw_message_delete(raw(new_source))
        self.destination.send.assert_awaited_once()
        self.assertEqual(embed_field(self.destination.send.call_args.kwargs["embed"], "원문"), "new channel original")
        self.assert_counts(full=1)

    async def test_new_public_raw_source_with_empty_gateway_cache_is_metadata_only(self):
        new_source, _ = public_pair(guild_id=self.source.guild.id, source_id=12,
                                    destination_id=self.destination.id)
        new_source.guild = self.source.guild
        available = {new_source.id: new_source, self.destination.id: self.destination}
        self.channels.clear()
        fetcher = AsyncMock(side_effect=lambda channel_id: available.get(channel_id))
        self.service = self.new_service(channel_fetcher=fetcher)
        await self.service.on_raw_message_delete(raw(new_source))
        self.destination.send.assert_awaited_once()
        self.assertIn("원문 확인 불가", embed_field(self.destination.send.call_args.kwargs["embed"], "원문"))
        self.assert_counts(metadata=1)

    async def test_disabled_content_never_counts_as_full_audit_success(self):
        self.service = self.new_service(include_content=False)
        await self.service.on_message_delete(make_message(self.source, "disabled-sensitive-content"))
        self.destination.send.assert_awaited_once()
        embed = self.destination.send.call_args.kwargs["embed"]
        self.assertNotIn("disabled-sensitive-content", str(embed.to_dict()))
        self.assertIn("비활성화", embed_field(embed, "원문"))
        self.assert_counts(metadata=1)

    async def test_t05_explicit_private_source_retains_legacy_identity_but_hides_original(self):
        self.source.overwrites[self.source.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        target = make_message(self.source, "private-secret-body")
        target.author.display_name = "PRIVATE-AUTHOR-NAME"
        await self.service.on_message_delete(target)
        self.destination.send.assert_awaited_once()
        rendered = str(self.destination.send.call_args.kwargs["embed"].to_dict())
        for legacy in ("PRIVATE-AUTHOR-NAME", "channel-11", "메시지 ID", "100", "KST"):
            self.assertIn(legacy, rendered)
        for secret in ("private-secret-body",):
            self.assertNotIn(secret, rendered)
        status = self.assert_counts(metadata=1)
        self.assertIsNone(status["last_full"])
        self.assertNotEqual(status["last_reason"], "sent_full")

    async def test_t11_t22_forbidden_delivery_preserves_enforcement_and_later_retry(self):
        class Forbidden(Exception):
            status = 403

        self.destination.send.side_effect = [Forbidden("secret-body-and-response"), None]
        target = make_message(self.source, "시321133발")
        with self.assertLogs("deletion_audit", level="WARNING") as captured:
            self.assertTrue(await self.service.delete(target, reason="profanity", matched_rule="시발"))
        target.delete.assert_awaited_once()
        self.assertNotIn("secret-body-and-response", "\n".join(captured.output))
        status = self.assert_counts(failed=1)
        self.assertEqual(status["last_reason"], "delivery_forbidden")
        self.assertNotIn((self.source.guild.id, target.id), self.service._delivered)
        await self.service.on_raw_message_delete(raw(self.source))
        self.assertEqual(self.destination.send.await_count, 2)
        self.assertEqual(embed_field(self.destination.send.call_args.kwargs["embed"], "원문"), "시321133발")
        self.assertIn("봇 직접 삭제", self.destination.send.call_args.kwargs["embed"].description)
        self.assert_counts(full=1, failed=1)

    async def test_t12_rate_limit_retry_obeys_retry_after_before_full_send(self):
        class RateLimited(Exception):
            status = 429
            retry_after = 0.025

        self.destination.send.side_effect = [RateLimited("sensitive api response"), None]
        sleep = AsyncMock()
        self.service = self.new_service(retry_sleep=sleep)
        await self.service.on_message_delete(make_message(self.source))
        self.assertEqual(self.destination.send.await_count, 2)
        sleep.assert_awaited_once()
        self.assertGreaterEqual(sleep.await_args_list[0].args[0], RateLimited.retry_after)
        status = self.assert_counts(full=1)
        self.assertEqual(status["reason_counts"]["rate_limited"], 1)
        self.assertEqual(status["reason_counts"]["retry_scheduled"], 1)

    async def test_t12_persistent_rate_limit_is_bounded_and_counted_failed(self):
        class RateLimited(Exception):
            status = 429
            retry_after = 0.001

        self.destination.send.side_effect = RateLimited()
        self.service = self.new_service(retry_sleep=AsyncMock(), retry_attempts=2)
        await self.service.on_message_delete(make_message(self.source))
        self.assertEqual(self.destination.send.await_count, 3)
        status = self.assert_counts(failed=1)
        self.assertEqual(status["last_reason"], "delivery_rate_limited")
        self.assertEqual(status["reason_counts"]["rate_limited"], 3)
        self.assertEqual(status["reason_counts"]["retry_scheduled"], 2)
        self.assertFalse(self.service._inflight)
        self.assertFalse(self.service._delivered)

    async def test_t12_retry_after_above_resource_bound_is_never_shortened(self):
        class RateLimited(Exception):
            status = 429
            retry_after = 35.0

        self.destination.send.side_effect = RateLimited()
        sleep = AsyncMock()
        self.service = self.new_service(retry_sleep=sleep, max_retry_after=30.0)
        await self.service.on_message_delete(make_message(self.source))
        self.destination.send.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assert_counts(failed=1)

    async def test_t12_bounded_pending_drop_is_visible_and_can_retry_later(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_send(**kwargs):
            entered.set()
            await release.wait()

        self.destination.send.side_effect = blocked_send
        self.service = self.new_service(pending_limit=1, concurrency=1)
        first = asyncio.create_task(self.service.on_message_delete(make_message(self.source, message_id=100)))
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            await self.service.on_message_delete(make_message(self.source, message_id=101))
            status = self.service.diagnostics(self.source.guild.id)
            self.assertGreaterEqual(status["failed"] + status["skipped"], 1)
            self.assertGreaterEqual(status["reason_counts"].get("burst_limit", 0), 1)
            self.assertEqual(status["dropped"], 1)
            self.assertLessEqual(len(self.service._inflight), 1)
            self.assertNotIn((self.source.guild.id, 101), self.service._delivered)
        finally:
            release.set()
            await first
        self.destination.send.side_effect = None
        await self.service.on_raw_message_delete(raw(self.source, 101))
        self.assertEqual(self.destination.send.await_count, 2)
        self.assertEqual(self.service.diagnostics(self.source.guild.id)["sent_full"], 2)

    async def test_pending_moderation_overflow_gateway_keeps_successful_bot_reason(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.service = self.new_service(pending_limit=1)
        first = make_message(self.source, "first pending original", message_id=100)
        second = make_message(self.source, "시321133발", message_id=101)

        async def slow_first_delete():
            entered.set()
            await release.wait()

        async def second_gateway_during_http():
            await self.service.on_raw_message_delete(raw(self.source, second.id))
            # HTTP has not confirmed the second deletion. Do not publish it
            # prematurely as an ordinary external deletion when _attempts fills.
            self.destination.send.assert_not_awaited()

        first.delete.side_effect = slow_first_delete
        second.delete.side_effect = second_gateway_during_http
        task = asyncio.create_task(self.service.delete(first, reason="spam"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            self.assertTrue(await self.service.delete(second, reason="profanity", matched_rule="시발"))
            second.delete.assert_awaited_once()
            self.destination.send.assert_awaited_once()
            embed = self.destination.send.call_args.kwargs["embed"]
            self.assertEqual(embed_field(embed, "원문"), "시321133발")
            self.assertIn("봇 직접 삭제", embed.description)
            self.assertIn("시발", embed_field(embed, "탐지"))
        finally:
            release.set()
            self.assertTrue(await task)
        await self.service.on_raw_message_delete(raw(self.source, second.id))
        self.assertEqual(self.destination.send.await_count, 2)
        self.assert_counts(full=2)

    async def test_t13_t19_log_channels_and_bot_cards_never_recurse(self):
        log_message = make_message(self.destination)
        bot_card = make_message(self.source, "bot audit card", message_id=101)
        bot_card.author.bot = True
        for target in (log_message, bot_card):
            self.assertIsNone(self.service.remember(target))
            await self.service.on_message_delete(target)
            await self.service.on_raw_message_delete(raw(target.channel, target.id, cached_message=target))
        await self.service.on_raw_message_delete(raw(self.destination, 102))
        await self.service.on_raw_bulk_message_delete(SimpleNamespace(
            guild_id=self.source.guild.id, channel_id=self.destination.id,
            message_ids={100, 102}, cached_messages=[log_message]))
        self.destination.send.assert_not_awaited()
        self.assert_counts()

    async def test_t16_four_existing_destination_ids_never_cross_guilds(self):
        exact_routes = ((937715555232780318, 1064823080100306995),
                        (865821307969732648, 1383468537229738156),
                        (1247409483353821335, 1065283543640576103),
                        (1247494689876086804, 1383987919454343269))
        pairs = [public_pair(guild_id=index, source_id=source_id, destination_id=destination_id)
                 for index, (source_id, destination_id) in enumerate(exact_routes, 1)]
        channels = {channel.id: channel for pair in pairs for channel in pair}
        service = DeletionAuditService(dict(exact_routes), channels.get, enabled=True,
                                       include_content=True, all_guild_channels=True)
        for index, (source, destination) in enumerate(pairs, 1):
            await service.on_message_delete(make_message(source, f"guild-{index}-only", message_id=100 + index))
            destination.send.assert_awaited_once()
            rendered = str(destination.send.call_args.kwargs["embed"].to_dict())
            self.assertIn(f"guild-{index}-only", rendered)
            self.assertEqual(service.diagnostics(source.guild.id)["sent_full"], 1)
            self.assertEqual(service.destination_for(source.guild.id, source.id), destination.id)
        for index, (_, destination) in enumerate(pairs, 1):
            rendered = str(destination.send.call_args.kwargs["embed"].to_dict())
            for other in range(1, 5):
                if other != index:
                    self.assertNotIn(f"guild-{other}-only", rendered)

    async def test_misconfigured_cross_guild_route_fails_without_sensitive_send(self):
        _, foreign = public_pair(guild_id=2, source_id=12, destination_id=self.destination.id)
        self.channels[self.destination.id] = foreign
        await self.service.on_message_delete(make_message(self.source, "must-not-cross-guild"))
        foreign.send.assert_not_awaited()
        self.destination.send.assert_not_awaited()
        self.assert_counts(failed=1)
        self.assertEqual(self.service.diagnostics(1)["last_reason"], "destination_guild_mismatch")

    async def test_t17_t18_long_unsafe_content_remains_safe_at_real_send_boundary(self):
        content = "@everyone <@50> ``` ``` [click](https://evil.test/) **fake** 😀 " + "긴 문장" * 5000
        await self.service.on_message_delete(make_message(self.source, content))
        kwargs = self.destination.send.call_args.kwargs
        embed = kwargs["embed"]
        rendered = str(embed.to_dict())
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        for unsafe in ("@everyone", "<@50>", "```", "https://evil.test", "**fake**"):
            self.assertNotIn(unsafe, rendered)
        self.assertIn("일부 표시", embed_field(embed, "원문"))
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assert_counts(full=1)

    async def test_t21_synthetic_send_never_claims_real_deletion_success(self):
        result = await self.service.send_test(self.source.guild.id)
        self.assertEqual(result["reason"], "test_sent")
        self.destination.send.assert_awaited_once()
        self.assertIn("테스트", self.destination.send.call_args.kwargs["embed"].title)
        status = self.assert_counts()
        self.assertIsNone(status["last_full"])
        self.assertEqual(status["public_deletion_status"], "check_required")
        self.assertEqual(status["test_result"], "test_sent")
        self.assertFalse(result["deletion_pipeline_verified"])

    async def test_latest_unknown_permissions_degrades_prior_full_health(self):
        await self.service.on_message_delete(make_message(self.source, message_id=100))
        self.assertEqual(self.service.diagnostics(1)["public_deletion_status"], "confirmed")
        self.source.permissions_for.side_effect = RuntimeError("secret permission response")
        await self.service.on_message_delete(make_message(self.source, message_id=101))
        status = self.assert_counts(full=1, metadata=1)
        self.assertEqual(status["public_deletion_status"], "check_required")
        self.assertEqual(status["last_reason"], "unknown_permissions")
        self.assertIn("unknown_permissions", str(self.destination.send.call_args.kwargs["embed"].to_dict()))
        self.assertIsNotNone(status["last_full"])

    async def test_latest_send_failure_degrades_prior_full_health(self):
        class Forbidden(Exception):
            status = 403

        await self.service.on_message_delete(make_message(self.source, message_id=100))
        self.destination.send.side_effect = Forbidden()
        await self.service.on_message_delete(make_message(self.source, message_id=101))
        status = self.assert_counts(full=1, failed=1)
        self.assertEqual(status["public_deletion_status"], "failed")
        self.assertIsNotNone(status["last_full"])

    async def test_private_metadata_does_not_claim_current_full_content_health(self):
        await self.service.on_message_delete(make_message(self.source, message_id=100))
        self.source.overwrites[self.source.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        await self.service.on_message_delete(make_message(self.source, "private after public", message_id=101))
        status = self.assert_counts(full=1, metadata=1)
        self.assertEqual(status["last_reason"], "private_source")
        self.assertIn("private_source", str(self.destination.send.call_args.kwargs["embed"].to_dict()))
        self.assertEqual(status["public_deletion_status"], "check_required")

    async def test_failed_second_transport_probe_replaces_previous_success_state(self):
        self.assertEqual((await self.service.send_test(1))["reason"], "test_sent")
        self.destination.permissions_for.side_effect = None
        self.destination.permissions_for.return_value = _permissions(view_channel=True, send_messages=True)
        result = await self.service.send_test(1)
        self.assertEqual(result["reason"], "destination_bot_embed_links")
        self.assertEqual(result["result"], "failed")
        self.assertEqual(self.service.diagnostics(1)["test_result"], "destination_bot_embed_links")
        self.destination.send.assert_awaited_once()

    async def test_category_permission_invalidation_refetches_synced_child(self):
        source, destination, _ = real_channel_pair(category_sync=True)
        available = {source.id: source, destination.id: destination}
        fetcher = AsyncMock(side_effect=lambda channel_id: available.get(channel_id))
        service = DeletionAuditService({source.id: destination.id}, lambda _: None, channel_fetcher=fetcher)
        await service._resolve_channel(source.id)
        service.invalidate_channel(source.category_id)
        await service._resolve_channel(source.id)
        self.assertEqual(fetcher.await_count, 2)

    async def test_permission_change_during_retry_after_prevents_original_disclosure(self):
        class RateLimited(Exception):
            status = 429
            retry_after = 0.25

        async def change_acl(_delay):
            self.source.overwrites[self.source.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            self.service.invalidate_channel(self.source.id)

        self.service = self.new_service(retry_sleep=change_acl)
        self.destination.send.side_effect = [RateLimited(), None]
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            await self.service.on_message_delete(make_message(self.source, "must-not-retry-private-content"))
        self.assertEqual(self.destination.send.await_count, 2)
        status = self.assert_counts(metadata=1)
        self.assertEqual(status["last_reason"], "private_source")
        self.assertNotIn("must-not-retry-private-content", str(self.destination.send.call_args.kwargs["embed"].to_dict()))
        self.assertIn("작성자", self.destination.send.call_args.kwargs["embed"].description)
        self.assertNotIn("must-not-retry-private-content", "\n".join(captured.output))
        self.assertIn((1, 100), self.service._delivered)

    async def test_channel_permission_invalidation_refetches_before_next_disclosure(self):
        source, destination, guild = real_channel_pair()
        private_source, _, _ = real_channel_pair(source_overwrites=[
            permission_overwrite(guild.id, view_channel=False)])
        available = {source.id: source, destination.id: destination}
        fetcher = AsyncMock(side_effect=lambda channel_id: available.get(channel_id))
        service = DeletionAuditService({source.id: destination.id}, lambda _: None,
                                       channel_fetcher=fetcher, enabled=True, include_content=True)
        with patch.object(discord.TextChannel, "send", new_callable=AsyncMock) as send:
            await service.on_message_delete(make_message(source, "public before ACL update", message_id=100))
            self.assertEqual(service.diagnostics(1)["sent_full"], 1)
            available[source.id] = private_source
            service.invalidate_channel(source.id)
            await service.on_message_delete(make_message(private_source, "private-after-acl-change", message_id=101))
        self.assertEqual(send.await_count, 2)
        self.assertNotIn("private-after-acl-change", str(send.call_args.kwargs["embed"].to_dict()))
        self.assertEqual(service.diagnostics(1)["sent_full"], 1)
        self.assertEqual(service.diagnostics(1)["sent_metadata_only"], 1)
        self.assertEqual(sum(call.args[0] == source.id for call in fetcher.await_args_list), 2)


if __name__ == "__main__":
    unittest.main()
