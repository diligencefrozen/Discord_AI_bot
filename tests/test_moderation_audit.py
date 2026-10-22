from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import io
from itertools import repeat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from moderation_audit import (
    MessageAuditCache,
    SnapshotLimits,
    attachment_kind,
    capture_message_snapshot,
    safe_audit_text,
)


def message(**overrides):
    values = dict(
        id=123, guild=SimpleNamespace(id=456, name="운영 서버"),
        channel=SimpleNamespace(id=789, name="일반 채팅"),
        author=SimpleNamespace(id=42, name="account", display_name="서버 별명"),
        content="시321133발", created_at=datetime(2026, 10, 22, 10, tzinfo=timezone.utc),
        attachments=[], stickers=[], embeds=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def attachment(**overrides):
    values = dict(id=700, filename="evidence.png", content_type="image/png", size=2048,
                  width=640, height=480, url="https://cdn.discordapp.com/attachments/1/evidence.png",
                  proxy_url="https://media.discordapp.net/attachments/1/evidence.png", is_spoiler=lambda: False)
    values.update(overrides)
    return SimpleNamespace(**values)


class SnapshotTests(unittest.TestCase):
    def test_content_is_exact_original_and_separate_from_matched_rule(self):
        source = message()
        snapshot = capture_message_snapshot(source, reason_code="profanity", detected_root="시발",
                                            reason_detail="obfuscated root", delete_origin="bot")
        self.assertEqual(snapshot.content, "시321133발")
        self.assertEqual(snapshot.original_length, 8)
        self.assertTrue(snapshot.content_available)
        self.assertEqual(snapshot.reason_code, "profanity")
        self.assertEqual(snapshot.detected_root, "시발")
        self.assertEqual(snapshot.matched_rule, "시발")
        self.assertEqual(snapshot.reason.matched_rule, "시발")
        self.assertEqual(snapshot.reason.detail, "obfuscated root")
        self.assertEqual(snapshot.delete_origin, "bot")
        self.assertEqual((snapshot.guild_id, snapshot.channel_id, snapshot.author_id), (456, 789, 42))
        self.assertEqual((snapshot.guild_name, snapshot.channel_name, snapshot.author_name), ("운영 서버", "일반 채팅", "서버 별명"))

    def test_snapshots_are_frozen_and_detached_from_mutating_discord_objects(self):
        image = attachment()
        sticker = SimpleNamespace(id=801, name="기쁨", format=SimpleNamespace(name="png"), url="https://cdn.discordapp.com/stickers/801.png")
        embed = SimpleNamespace(type="rich", title="링크 제목")
        source = message(attachments=[image], stickers=[sticker], embeds=[embed])
        snapshot = capture_message_snapshot(source)
        source.content = "changed"
        source.author.display_name = "changed"
        image.filename = "changed.exe"
        sticker.name = "changed"
        embed.title = "changed"
        source.attachments.clear()
        self.assertEqual(snapshot.content, "시321133발")
        self.assertEqual(snapshot.author_name, "서버 별명")
        self.assertEqual(snapshot.attachments[0].filename, "evidence.png")
        self.assertEqual(snapshot.stickers[0].name, "기쁨")
        self.assertEqual(snapshot.embeds[0].title, "링크 제목")
        with self.assertRaises(FrozenInstanceError):
            snapshot.content = "changed"
        with self.assertRaises(FrozenInstanceError):
            snapshot.attachments[0].filename = "changed"

    def test_attachments_only_is_empty_content_not_unknown_original(self):
        present = capture_message_snapshot(message(content="", attachments=[attachment()]))
        self.assertTrue(present.content_available)
        self.assertEqual(present.original_length, 0)
        self.assertEqual(len(present.attachments), 1)
        unavailable = capture_message_snapshot(SimpleNamespace(id=123, guild_id=456, channel_id=789))
        self.assertFalse(unavailable.content_available)
        self.assertIsNone(unavailable.original_length)
        self.assertFalse(unavailable.attachments_available)
        self.assertIsNone(unavailable.attachment_count)
        self.assertEqual((unavailable.guild_id, unavailable.channel_id), (456, 789))

    def test_complete_attachment_metadata_and_spoiler(self):
        snapshot = capture_message_snapshot(message(attachments=[attachment(is_spoiler=lambda: True)]))
        item = snapshot.attachments[0]
        self.assertEqual((item.id, item.filename, item.content_type, item.size), (700, "evidence.png", "image/png", 2048))
        self.assertEqual((item.width, item.height, item.kind), (640, 480, "image"))
        self.assertTrue(item.spoiler)
        self.assertIn("cdn.discordapp.com", item.url)
        self.assertIn("media.discordapp.net", item.proxy_url)
        self.assertEqual(snapshot.attachment_count, 1)
        self.assertTrue(snapshot.attachments_available)

    def test_throwing_spoiler_helper_keeps_content_and_attachment_metadata(self):
        for error in (AttributeError("unsupported helper"), NotImplementedError("unsupported helper")):
            def broken_helper():
                raise error

            with self.subTest(error=type(error).__name__):
                snapshot = capture_message_snapshot(message(attachments=[attachment(
                    filename="SPOILER_evidence.png", is_spoiler=broken_helper)]))
                self.assertEqual(snapshot.content, "시321133발")
                item = snapshot.attachments[0]
                self.assertEqual((item.id, item.content_type, item.size, item.width, item.height),
                                 (700, "image/png", 2048, 640, 480))
                self.assertEqual(item.kind, "image")
                self.assertIn("cdn.discordapp.com", item.url)
                self.assertTrue(item.spoiler)
                explicit = capture_message_snapshot(message(attachments=[attachment(
                    is_spoiler=broken_helper, spoiler=True)]))
                self.assertTrue(explicit.attachments[0].spoiler)

    def test_attachment_classification_uses_bounded_captured_metadata(self):
        snapshot = capture_message_snapshot(message(attachments=[attachment(
            filename="x" * 100_000 + ".mp4", content_type="")]),
            limits=SnapshotLimits(max_name_chars=8))
        self.assertEqual(snapshot.attachments[0].filename, "xxxxxxxx")
        self.assertEqual(snapshot.attachments[0].kind, "other")
        self.assertIn("attachments[0].filename", snapshot.truncated_fields)

    def test_dictionary_sticker_items_and_embed_metadata(self):
        source = message(sticker_items=[dict(id="801", name="곰", format_type=2)],
                         embeds=[dict(type="video", title="참조 동영상")])
        snapshot = capture_message_snapshot(source)
        sticker = snapshot.stickers[0]
        self.assertEqual((sticker.id, sticker.name, sticker.format, sticker.url), (801, "곰", "2", ""))
        self.assertEqual((snapshot.sticker_count, snapshot.embed_count), (1, 1))
        self.assertEqual((snapshot.embeds[0].kind, snapshot.embeds[0].title), ("video", "참조 동영상"))

    def test_unicode_sequences_and_static_animated_custom_emoji(self):
        content = "😀😀 👩🏽‍💻 🇰🇷 1️⃣ ❤️ <:bear:12345> <a:dance:67890>"
        snapshot = capture_message_snapshot(message(content=content))
        self.assertEqual([item.text for item in snapshot.emojis],
                         ["😀", "👩🏽‍💻", "🇰🇷", "1️⃣", "❤️", "<:bear:12345>", "<a:dance:67890>"])
        custom, animated = snapshot.emojis[-2:]
        self.assertEqual((custom.kind, custom.name, custom.id, custom.animated), ("custom", "bear", 12345, False))
        self.assertTrue(custom.url.endswith("/12345.png"))
        self.assertTrue(animated.animated)
        self.assertTrue(animated.url.endswith("/67890.gif"))
        self.assertEqual(snapshot.content, content)

    def test_safe_display_prevents_everyone_here_user_and_role_mentions(self):
        original = "@everyone @here <@42> <@!42> <@&42> 보존"
        snapshot = capture_message_snapshot(message(content=original))
        display = safe_audit_text(snapshot.content)
        for token in ("@everyone", "@here", "<@42>", "<@!42>", "<@&42>"):
            self.assertNotIn(token, display)
        self.assertEqual(snapshot.content, original)

    def test_content_urls_and_sensitive_metadata_never_appear_in_repr_or_io(self):
        output, error = io.StringIO(), io.StringIO()
        with patch("builtins.open", side_effect=AssertionError("disk access")), redirect_stdout(output), redirect_stderr(error):
            snapshot = capture_message_snapshot(message(content="unique-secret", attachments=[attachment()],
                stickers=[dict(id=801, name="secret-sticker")], embeds=[dict(type="rich", title="secret-title")]),
                reason_code="private-code", detected_root="private-root", reason_detail="private-detail")
            cache = MessageAuditCache()
            cache.remember(snapshot)
            representations = repr([snapshot, snapshot.reason, *snapshot.attachments, *snapshot.stickers, *snapshot.embeds])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "")
        for secret in ("unique-secret", "evidence.png", "discordapp.com", "secret-sticker", "secret-title", "private-code", "private-root", "private-detail"):
            self.assertNotIn(secret, representations)

    def test_timestamps_are_aware_utc_and_no_author_of_external_deletion_is_inferred(self):
        local = datetime(2026, 10, 22, 19, tzinfo=timezone(timedelta(hours=9)))
        snapshot = capture_message_snapshot(message(created_at=local), captured_at=local, delete_origin="external")
        self.assertEqual(snapshot.created_at, datetime(2026, 10, 22, 10, tzinfo=timezone.utc))
        self.assertEqual(snapshot.captured_at.utcoffset(), timedelta(0))
        self.assertEqual(snapshot.delete_origin, "external")
        self.assertFalse(hasattr(snapshot, "deleted_by"))
        naive = capture_message_snapshot(message(created_at=local.replace(tzinfo=None)))
        self.assertEqual(naive.created_at.utcoffset(), timedelta(0))
        self.assertEqual(capture_message_snapshot(message()).delete_origin, "unknown")
        with self.assertRaises(ValueError):
            capture_message_snapshot(message(), delete_origin="moderator")

    def test_content_and_collection_limits_include_original_counts_and_truncation(self):
        limits = SnapshotLimits(max_content_chars=12, max_attachments=1, max_stickers=1, max_embeds=1, max_emojis=1)
        snapshot = capture_message_snapshot(message(content="😀🐻" + "x" * 100,
            attachments=[attachment(), attachment()], stickers=[dict(id=1), dict(id=2)], embeds=[dict(type="rich"), dict(type="video")]), limits=limits)
        self.assertEqual(len(snapshot.content), 12)
        self.assertEqual(snapshot.original_length, 102)
        self.assertEqual((snapshot.attachment_count, snapshot.sticker_count, snapshot.embed_count), (2, 2, 2))
        self.assertEqual((len(snapshot.attachments), len(snapshot.stickers), len(snapshot.embeds), len(snapshot.emojis)), (1, 1, 1, 1))
        self.assertEqual(set(snapshot.truncated_fields), {"content", "attachments", "stickers", "embeds", "emojis"})

    def test_names_urls_reasons_and_aggregate_user_text_are_bounded(self):
        limits = SnapshotLimits(max_name_chars=8, max_url_chars=40, max_total_chars=900)
        snapshot = capture_message_snapshot(message(content="x" * 4000,
            attachments=[attachment(filename="name" * 1000, content_type="image/png" + "x" * 1000,
                                    url="https://cdn.example.com/" + "x" * 4000)]),
            reason_code="c" * 1000, detected_root="r" * 1000, reason_detail="d" * 1000, limits=limits)
        self.assertLessEqual(len(snapshot.content), 900 - limits.reason_reservation)
        self.assertLessEqual(len(snapshot.author_name), 8)
        self.assertLessEqual(len(snapshot.attachments[0].filename), 8)
        self.assertLessEqual(len(snapshot.attachments[0].url), 40)
        self.assertEqual(len(snapshot.reason.code), 64)
        self.assertEqual(len(snapshot.reason.detected_root), 128)
        self.assertEqual(len(snapshot.reason.detail), 512)
        self.assertIn("reason.code", snapshot.truncated_fields)
        self.assertIn("attachments[0].filename", snapshot.truncated_fields)

    def test_unbounded_iterables_are_read_only_to_limit_plus_one(self):
        snapshot = capture_message_snapshot(message(attachments=repeat(attachment())), limits=SnapshotLimits(max_attachments=2))
        self.assertEqual(len(snapshot.attachments), 2)
        self.assertIsNone(snapshot.attachment_count)
        self.assertIn("attachments", snapshot.truncated_fields)

    def test_non_web_urls_and_invalid_numeric_metadata_are_not_retained(self):
        snapshot = capture_message_snapshot(message(attachments=[attachment(url="file:///private/key", proxy_url="javascript:bad()",
            size=-4, width=True, height="not-an-integer")]))
        item = snapshot.attachments[0]
        self.assertEqual((item.url, item.proxy_url), ("", ""))
        self.assertEqual((item.size, item.width, item.height), (None, None, None))


class AttachmentKindTests(unittest.TestCase):
    def test_all_requested_file_categories(self):
        for mime, filename, expected in (
            ("image/jpeg", "photo.bin", "image"), ("image/gif", "photo.bin", "gif"),
            ("video/mp4", "photo.png", "video"), ("audio/ogg", "sound", "audio"),
            ("application/pdf", "report", "document"), ("application/zip", "archive", "archive"),
            ("application/x-executable", "report.pdf", "other"),
        ):
            with self.subTest(mime=mime):
                self.assertEqual(attachment_kind(mime, filename), expected)

    def test_missing_and_generic_mime_use_case_insensitive_suffix(self):
        for filename, expected in (("file.PNG", "image"), ("file.GIF", "gif"), ("file.MOV", "video"),
                                   ("file.MP3", "audio"), ("file.DOCX", "document"), ("file.7Z", "archive"), ("file.BIN", "other")):
            with self.subTest(filename=filename):
                self.assertEqual(attachment_kind(None, filename), expected)
                self.assertEqual(attachment_kind("application/octet-stream", filename), expected)
        self.assertEqual(attachment_kind("IMAGE/PNG; charset=utf-8", "file.mp4"), "image")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.cache = MessageAuditCache(max_entries=2, ttl_seconds=10, clock=lambda: self.now)

    def test_guild_message_keys_are_isolated(self):
        first = self.cache.capture(message())
        second = self.cache.capture(message(guild=SimpleNamespace(id=999)))
        self.assertIs(self.cache.get(456, 123), first)
        self.assertIs(self.cache.get(999, 123), second)
        self.assertIsNone(self.cache.get(777, 123))

    def test_lru_capacity_evicts_least_recently_accessed(self):
        first = self.cache.capture(message(id=1))
        self.cache.capture(message(id=2))
        self.assertIs(self.cache.get(456, 1), first)
        self.cache.capture(message(id=3))
        self.assertIsNone(self.cache.get(456, 2))
        self.assertEqual(len(self.cache), 2)

    def test_absolute_ttl_is_not_extended_by_read_or_annotation(self):
        original = self.cache.capture(message())
        self.now = 109.0
        updated = self.cache.annotate(456, 123, reason_code="profanity", detected_root="시발", reason_detail="obfuscated", delete_origin="bot")
        self.assertEqual(updated.reason_code, "profanity")
        self.assertEqual(updated.reason.detail, "obfuscated")
        self.assertEqual(updated.delete_origin, "bot")
        self.assertEqual(original.reason_code, "")
        self.assertEqual(original.delete_origin, "unknown")
        self.assertEqual(updated.content, original.content)
        self.assertIs(self.cache.get(456, 123), updated)
        self.now = 110.0
        self.assertIsNone(self.cache.get(456, 123))
        self.assertEqual(len(self.cache), 0)

    def test_expired_entry_cannot_be_annotated_and_missing_is_not_empty_snapshot(self):
        self.cache.capture(message())
        self.now = 111.0
        self.assertIsNone(self.cache.annotate(456, 123, delete_origin="bot"))
        self.assertIsNone(self.cache.pop(456, 123))

    def test_reason_annotation_obeys_original_limits(self):
        cache = MessageAuditCache(limits=SnapshotLimits(max_reason_detail_chars=5))
        cache.capture(message())
        updated = cache.annotate(456, 123, reason_detail="x" * 1000)
        self.assertEqual(updated.reason.detail, "xxxxx")
        self.assertIn("reason.detail", updated.truncated_fields)

    def test_pop_invalidates_stale_raw_edit_snapshot(self):
        snapshot = self.cache.capture(message())
        self.assertIs(self.cache.pop(456, 123), snapshot)
        self.assertIsNone(self.cache.get(456, 123))
        self.cache.capture(message())
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)

    def test_limits_and_cache_reject_invalid_configuration(self):
        for config in (dict(max_entries=0), dict(max_entries=True), dict(ttl_seconds=0), dict(ttl_seconds=float("nan"))):
            with self.subTest(config=config), self.assertRaises(ValueError):
                MessageAuditCache(**config)
        for config in (dict(max_content_chars=-1), dict(max_attachments=True), dict(max_total_chars=1)):
            with self.subTest(config=config), self.assertRaises(ValueError):
                SnapshotLimits(**config)
        with self.assertRaises(TypeError):
            self.cache.remember(message())
        self.cache.capture(message())
        with self.assertRaises(ValueError):
            self.cache.annotate(456, 123, delete_origin="guessed-moderator")


if __name__ == "__main__":
    unittest.main()
