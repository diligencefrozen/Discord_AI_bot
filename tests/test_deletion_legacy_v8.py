"""Selective-mode V8 regressions; the registered default is tested separately.

All sends are mocked. These tests do not attest to a deployed Discord channel.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from deletion_audit import DeletionAuditService, build_metadata_deletion_embed
from moderation_audit import capture_message_snapshot
from moderation_rules import find_badroot

try:
    from tests.test_deletion_pipeline_v7 import public_pair, make_message, raw, embed_field
except ModuleNotFoundError:
    from test_deletion_pipeline_v7 import public_pair, make_message, raw, embed_field


class LegacyDeletionV8Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source, self.destination = public_pair()
        self.source.name, self.source.guild.name = "moderator-only", "운영 서버"
        self.channels = {11: self.source, 20: self.destination}
        self.service = self.new_service()

    def new_service(self, *, private_ids="", routes=None, **options):
        defaults = dict(enabled=True, include_content=True, all_guild_channels=True, registered_source_content=False)
        defaults.update(options)
        with patch.dict("os.environ", {"DELETION_PUBLIC_LOG_PRIVATE_FULL_SOURCE_IDS": private_ids}):
            return DeletionAuditService(routes if routes is not None else {11: 20}, self.channels.get, **defaults)

    def private_source(self):
        self.source.overwrites[self.source.guild.default_role] = discord.PermissionOverwrite(view_channel=False)

    def target(self, content="시321133발", message_id=100):
        target = make_message(self.source, content, message_id=message_id)
        target.author.display_name = "Apple Watch"
        target.attachments = [SimpleNamespace(id=444, filename="PRIVATE-FILE.png", content_type="image/png",
                                             size=2048, width=128, height=128, is_spoiler=lambda: False)]
        return target

    def rendered(self):
        return str(self.destination.send.call_args.kwargs["embed"].to_dict())

    def assert_base(self, *, known_author=True):
        card = self.destination.send.call_args.kwargs["embed"]
        for value in ("<#11>", "KST"):
            self.assertIn(value, str(card.to_dict()))
        for label in ("메시지 ID", "채널 ID", "작성자 ID"):
            self.assertNotIn(label, str(card.to_dict()))
        self.assertNotIn("열람 범위 제한", card.title)
        self.assertIn("<@50>" if known_author else "작성자 확인 불가", card.description)
        self.assertEqual(self.destination.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_T01_public_source_sends_legacy_fields_plus_body_once(self):
        await self.service.on_message_delete(self.target())
        self.destination.send.assert_awaited_once()
        self.assert_base()
        self.assertIn("시321133발", self.rendered())
        self.assertEqual(self.service.diagnostics(1)["sent_full"], 1)

    async def test_T02_T03_explicit_private_restores_metadata_hides_body_root_and_file(self):
        self.private_source()
        target = self.target()
        await self.service.delete(target, reason="profanity", matched_rule=find_badroot(target.content), detail="SECRET-DETAIL")
        self.destination.send.assert_awaited_once()
        self.assert_base()
        self.assertIn("<#11>", self.rendered())
        self.assertIn("금칙어", self.rendered())
        self.assertEqual(self.service.diagnostics(1)["last_reason"], "private_source")
        self.assertIn("공개 정책", self.rendered())
        self.assertIn("첨부파일 1개", self.rendered())
        for secret in (target.content, find_badroot(target.content), "PRIVATE-FILE.png", "SECRET-DETAIL"):
            self.assertNotIn(secret, self.rendered())
        state = self.service.diagnostics(1)
        self.assertEqual((state["sent_full"], state["sent_metadata_only"], state["sent_restricted"]), (0, 1, 0))

    async def test_T04_explicit_private_opt_in_discloses_full_detail(self):
        self.private_source()
        self.service = self.new_service(private_ids="11")
        target = self.target()
        await self.service.delete(target, reason="profanity", matched_rule=find_badroot(target.content))
        self.assert_base()
        for detail in (target.content, find_badroot(target.content), "PRIVATE-FILE.png", "image/png"):
            self.assertIn(detail, self.rendered())
        state = self.service.diagnostics(1)
        self.assertEqual((state["sent_full"], state["sent_metadata_only"]), (1, 0))
        self.assertEqual(state["last_reason"], "private_full_opt_in")

    async def test_T05_automatic_private_source_is_skipped_even_if_in_full_allowlist(self):
        self.private_source()
        target = self.target()
        # 12 is a different explicit source; source11 is only auto discovered.
        self.service = self.new_service(routes={12: 20}, private_ids="11")
        await self.service.on_message_delete(target)
        self.destination.send.assert_not_awaited()
        self.assertEqual(self.service.private_full_sources, frozenset())
        state = self.service.diagnostics(1)
        self.assertEqual(state["last_reason"], "private_unrouted_skipped")
        self.assertEqual((state["sent_full"], state["sent_metadata_only"], state["skipped"]), (0, 0, 1))

    async def test_T05_extra_source_is_not_explicit_legacy_consent(self):
        self.private_source()
        with patch.dict("os.environ", {"DELETION_LOG_EXTRA_SOURCE_CHANNEL_IDS": "11"}):
            self.service = self.new_service(routes={12: 20}, private_ids="11", all_guild_channels=False)
        await self.service.on_message_delete(self.target())
        self.destination.send.assert_not_awaited()
        self.assertEqual(self.service.diagnostics(1)["last_reason"], "private_unrouted_skipped")

    async def test_T06_bot_snapshot_reason_and_original_remain_distinct(self):
        target = self.target()
        measured = find_badroot(target.content)
        self.assertEqual(measured, "시발")
        with self.assertLogs("deletion_audit", level="INFO") as console:
            self.assertTrue(await self.service.delete(target, reason="profanity", matched_rule=measured))
        self.assertEqual(embed_field(self.destination.send.call_args.kwargs["embed"], "원문"), target.content)
        self.assertIn(measured, embed_field(self.destination.send.call_args.kwargs["embed"], "탐지 정보"))
        self.assertIn("봇 직접 삭제", self.rendered())
        self.assertNotIn(target.content, "\n".join(console.output))

    async def test_T07_latest_verified_edit_snapshot_wins(self):
        target = self.target("1")
        self.service.remember(target)
        target.content = "시발"
        target.edited_at = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
        self.service.remember(target)
        await self.service.delete(target, reason="profanity", matched_rule="시발", trigger="message_edit", edited_at=target.edited_at)
        self.assertEqual(embed_field(self.destination.send.call_args.kwargs["embed"], "원문"), "시발")
        self.assertIn("수정 후 검열", self.rendered())

    async def test_T08_raw_without_snapshot_preserves_channel_link_not_invented_author(self):
        await self.service.on_raw_message_delete(raw(self.source))
        self.assert_base(known_author=False)
        self.assertIn("원문 확인 불가", self.rendered())
        self.assertNotIn("Apple Watch", self.rendered())
        self.assertEqual(self.service.diagnostics(1)["sent_metadata_only"], 1)

    async def test_T08_raw_missing_source_channel_still_links_explicit_source(self):
        self.channels.pop(11)
        await self.service.on_raw_message_delete(raw(self.source))
        self.assert_base(known_author=False)
        self.assertEqual(self.service.diagnostics(1)["last_reason"], "source_not_found")

    async def test_body_unavailable_does_not_erase_other_consented_details(self):
        for private in (False, True):
            with self.subTest(private=private):
                self.destination.send.reset_mock()
                if private:
                    self.private_source()
                self.service = self.new_service(private_ids="11" if private else "")
                target = self.target()
                del target.content
                await self.service.delete(target, reason="profanity", matched_rule="시발")
                self.assert_base()
                self.assertIn("원문 확인 불가", self.rendered())
                self.assertIn("PRIVATE-FILE.png", self.rendered())
                self.assertIn("시발", self.rendered())
                state = self.service.diagnostics(1)
                self.assertEqual((state["sent_full"], state["sent_metadata_only"]), (0, 1))

    async def test_T09_full_unicode_custom_emoji_sticker_and_media_metadata(self):
        target = self.target("😀 <:example:123> <a:motion:456>")
        target.stickers = [SimpleNamespace(id=77, name="테스트 스티커", format=discord.StickerFormatType.png)]
        target.attachments += [SimpleNamespace(id=445, filename="moving.gif", content_type="image/gif", size=4096)]
        await self.service.on_message_delete(target)
        for value in ("😀", "example", "123", "motion", "456", "테스트 스티커", "PRIVATE-FILE.png", "moving.gif"):
            self.assertIn(value, self.rendered())

    async def test_T10_cached_raw_and_bulk_deduplicate_metadata_delivery(self):
        self.private_source()
        target = self.target()
        await asyncio.gather(self.service.on_message_delete(target), self.service.on_raw_message_delete(raw(self.source, cached_message=target)),
                             self.service.on_raw_bulk_message_delete(SimpleNamespace(
                                 guild_id=1, channel_id=11, message_ids={100}, cached_messages=[target])))
        self.destination.send.assert_awaited_once()
        self.assertEqual(self.service.diagnostics(1)["sent_metadata_only"], 1)

    async def test_T11_destination_cache_miss_uses_fetch_channel(self):
        self.channels.pop(20)
        fetcher = AsyncMock(return_value=self.destination)
        self.service = self.new_service(channel_fetcher=fetcher)
        await self.service.on_message_delete(self.target())
        fetcher.assert_awaited_once_with(20)
        self.destination.send.assert_awaited_once()

    async def test_T12_destination_permission_and_http403_fail_without_body_console(self):
        class Forbidden(Exception):
            status = 403
        self.private_source()
        self.destination.send.side_effect = Forbidden("SECRET-RESPONSE")
        with self.assertLogs("deletion_audit", level="WARNING") as console:
            await self.service.delete(self.target(), reason="profanity", matched_rule="시발")
        state = self.service.diagnostics(1)
        self.assertEqual((state["failed"], state["sent_metadata_only"], state["sent_full"]), (1, 0, 0))
        self.assertEqual(state["last_reason"], "delivery_forbidden")
        self.assertNotIn("SECRET-RESPONSE", "\n".join(console.output))
        self.assertNotIn("시321133발", "\n".join(console.output))

    async def test_T13_constructor_channel_fetcher_remains_supported(self):
        fetcher = AsyncMock()
        self.assertIs(self.new_service(channel_fetcher=fetcher).channel_fetcher, fetcher)

    async def test_T14_own_log_channel_never_reenters_audit(self):
        target = make_message(self.destination, "시발")
        target.author.bot = True
        await self.service.on_message_delete(target)
        await self.service.on_raw_message_delete(raw(self.destination))
        self.destination.send.assert_not_awaited()

    async def test_T15_T16_unknown_permissions_explicit_metadata_automatic_skips(self):
        self.source.overwrites[object()] = discord.PermissionOverwrite(view_channel=False)
        await self.service.on_message_delete(self.target())
        self.assert_base()
        self.assertEqual(self.service.diagnostics(1)["last_reason"], "unknown_permissions")
        self.assertIn("공개 정책", self.rendered())
        self.assertNotIn("PRIVATE-FILE", self.rendered())
        self.destination.send.reset_mock()
        automatic = self.new_service(routes={12: 20})
        await automatic.on_message_delete(self.target(message_id=101))
        self.destination.send.assert_not_awaited()
        self.assertEqual(automatic.diagnostics(1)["last_reason"], "unverified_unrouted_skipped")

    async def test_T17_content_disabled_overrides_opt_in_and_cannot_count_full(self):
        self.private_source()
        self.service = self.new_service(private_ids="11", include_content=False)
        await self.service.delete(self.target(), reason="profanity", matched_rule="시발")
        self.assert_base()
        self.assertIn("비활성화", self.rendered())
        for secret in ("시321133발", "시발", "PRIVATE-FILE.png"):
            self.assertNotIn(secret, self.rendered())
        status = self.service.diagnostics(1)
        self.assertEqual((status["sent_full"], status["sent_metadata_only"]), (0, 1))
        self.assertIsNone(status["last_full"])
        self.assertNotEqual(status["public_deletion_status"], "confirmed")

    async def test_private_metadata_and_opt_in_full_retry_apply_correct_policy(self):
        class RateLimited(Exception):
            status, retry_after = 429, 0.001
        self.private_source()
        for private_ids in ("", "11"):
            with self.subTest(private_ids=private_ids):
                self.destination.send.reset_mock()
                self.destination.send.side_effect = [RateLimited(), None]
                self.service = self.new_service(private_ids=private_ids, retry_sleep=AsyncMock())
                await self.service.on_message_delete(self.target())
                self.assertEqual(self.destination.send.await_count, 2)
                self.assert_base()
                self.assertEqual("시321133발" in self.rendered(), bool(private_ids))
                self.assertEqual(self.service.diagnostics(1)["sent_full"], int(bool(private_ids)))

    async def test_private_full_allowlist_is_strict_subset_and_malformed_fails_closed(self):
        self.private_source()
        for malformed in ('"11"', "11,", "11,no", "0", "１２", str(2 ** 64), "9" * 6000):
            with self.subTest(value=malformed):
                self.destination.send.reset_mock()
                self.service = self.new_service(private_ids=malformed)
                await self.service.on_message_delete(self.target())
                self.assertFalse(self.service.private_full_sources_valid)
                self.assertEqual(self.service.private_full_sources, frozenset())
                self.assertNotIn("시321133발", self.rendered())
                self.assert_base()
        configured = self.new_service(routes={11: 20, 20: 20}, private_ids="11,20,99")
        self.assertEqual(configured.private_full_sources, frozenset({11}))

    async def test_private_opt_in_cannot_override_destination_or_source_identity_failures(self):
        self.private_source()
        self.service = self.new_service(private_ids="11")
        self.source.guild = SimpleNamespace(id=2)
        # Snapshot remains in guild1, but the resolver now returns a foreign source.
        target = self.target()
        target.guild = self.destination.guild
        await self.service.on_message_delete(target)
        self.destination.send.assert_not_awaited()
        self.assertEqual(self.service.diagnostics(1)["last_reason"], "source_guild_mismatch")

    def test_metadata_builder_bounds_and_escapes_only_authorized_base_fields(self):
        target = self.target()
        target.author.display_name = "@everyone ``` **fake** [click](https://evil.test/)" * 100
        snapshot = capture_message_snapshot(target, reason_code="profanity", detected_root="SECRET-ROOT", reason_detail="SECRET-DETAIL")
        card = build_metadata_deletion_embed(snapshot)
        rendered = str(card.to_dict())
        self.assertLessEqual(len(card), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in card.fields))
        for unsafe in ("@everyone", "```", "https://evil.test/", "SECRET-ROOT", "SECRET-DETAIL", "PRIVATE-FILE.png"):
            self.assertNotIn(unsafe, rendered)


if __name__ == "__main__":
    unittest.main()
