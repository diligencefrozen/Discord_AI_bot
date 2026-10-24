"""Compact deletion cards retain useful content without empty metadata panels."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from deletion_audit import build_deletion_embed, build_metadata_deletion_embed
from moderation_audit import capture_message_snapshot


DELETED_AT = datetime(2026, 10, 23, 6, 38, 28, tzinfo=timezone.utc)


def snapshot(content="시321133발", **kwargs):
    target = SimpleNamespace(
        id=1563079141299982417, content=content,
        author=SimpleNamespace(id=855749166764654653, display_name="Apple Watch", bot=False),
        guild=SimpleNamespace(id=585087748948623380, name="운영 서버"),
        channel=SimpleNamespace(id=937715555232780318, name="moderator-only"),
        attachments=[], stickers=[], embeds=[],
        created_at=datetime(2026, 10, 23, 6, 38, 27, tzinfo=timezone.utc),
    )
    return capture_message_snapshot(target, reason_code="profanity", detected_root="시발",
                                    delete_origin="bot", **kwargs)


def fields(card):
    return {item.name: item.value for item in card.fields}


class CompactDeletionCardTests(unittest.TestCase):
    def test_text_only_card_prioritizes_body_with_two_fields(self):
        card = build_deletion_embed(snapshot(), deleted_at=DELETED_AT)
        self.assertEqual(card.title, "메시지 삭제 기록")
        self.assertEqual(card.description, "**User:** <@855749166764654653>\n**Channel:** <#937715555232780318>")
        self.assertEqual(list(fields(card)), ["Deleted Content", "삭제 사유"])
        self.assertEqual(fields(card)["Deleted Content"], "시321133발")
        self.assertIn("시발", fields(card)["삭제 사유"])
        self.assertNotIn("시321133발", fields(card)["삭제 사유"])
        self.assertNotIn("없음", str(card.to_dict()))
        self.assertNotIn("0개", str(card.to_dict()))

    def test_identity_links_replace_numeric_id_labels_and_footer_is_server_and_time(self):
        current = snapshot()
        card = build_deletion_embed(current, deleted_at=DELETED_AT)
        visible = card.title + card.description + "".join(fields(card).values()) + card.footer.text
        self.assertEqual(card.footer.text, "운영 서버 | 2026-10-23 15:38:28 KST")
        for value in ("1563079141299982417", "585087748948623380", "메시지 ID", "채널 ID", "작성자 ID"):
            self.assertNotIn(value, visible)
        self.assertEqual(visible.count("<@855749166764654653>"), 1)
        self.assertEqual(visible.count("<#937715555232780318>"), 1)
        self.assertEqual(visible.count("2026-10-23 15:38:28 KST"), 1)
        self.assertNotIn("15:38:27", visible)
        # IDs still exist internally for delivery routing, deduplication and diagnostics.
        self.assertEqual((current.message_id, current.guild_id), (1563079141299982417, 585087748948623380))

    def test_display_name_changes_cannot_change_identity_links_or_inject_mentions(self):
        current = replace(snapshot(), author_name="@everyone **fake** <@999>",
                          channel_name="@here [fake](https://evil.test)")
        card = build_deletion_embed(current, deleted_at=DELETED_AT)
        self.assertEqual(card.description, "**User:** <@855749166764654653>\n**Channel:** <#937715555232780318>")
        for untrusted in ("everyone", "fake", "<@999>", "https://evil.test"):
            self.assertNotIn(untrusted, card.description)

    def test_unknown_or_malformed_identity_never_creates_a_guessed_mention(self):
        for author_id, channel_id in ((0, 0), (-1, -1), (True, True), ("50> @everyone", "11> @here")):
            with self.subTest(author_id=author_id, channel_id=channel_id):
                current = replace(snapshot(), author_id=author_id, channel_id=channel_id,
                                  author_name="", channel_name="")
                card = build_deletion_embed(current, deleted_at=DELETED_AT)
                self.assertEqual(card.description, "**User:** 작성자 확인 불가\n**Channel:** 채널 확인 불가")
                self.assertNotIn("<@", card.description)
                self.assertNotIn("<#", card.description)

    def test_edit_and_scan_context_keeps_relevant_timestamps(self):
        for trigger, expected in (("message_edit", "수정 후 검열"), ("history_scan", "정기 재점검")):
            with self.subTest(trigger=trigger):
                current = replace(snapshot(), trigger=trigger,
                                  edited_at=datetime(2026, 10, 23, 6, 38, 27, tzinfo=timezone.utc))
                card = build_deletion_embed(current, deleted_at=DELETED_AT)
                context = fields(card)["검열 정보"]
                self.assertIn(expected, context)
                self.assertIn("작성 2026-10-23 15:38:27 KST", context)
                self.assertIn("수정 2026-10-23 15:38:27 KST", context)
                self.assertNotIn("검열", card.description)

    def test_unknown_body_is_distinct_from_empty_known_body(self):
        unknown = replace(snapshot(), content="", content_available=False)
        empty = snapshot("")
        self.assertIn("원문 확인 불가", fields(build_deletion_embed(unknown))["Deleted Content"])
        self.assertIn("본문 없음", fields(build_deletion_embed(empty))["Deleted Content"])

    def test_metadata_only_card_does_not_render_empty_sections_or_internal_policy_codes(self):
        card = build_metadata_deletion_embed(snapshot("private body"), deleted_at=DELETED_AT)
        self.assertEqual(list(fields(card)), ["Deleted Content", "삭제 사유"])
        rendered = str(card.to_dict())
        for unwanted in ("private body", "시발", "private_source", "기록 상태", "0개", "확보 범위"):
            self.assertNotIn(unwanted, rendered)
        self.assertIn("원문 비공개", fields(card)["Deleted Content"])

    def test_metadata_only_card_keeps_nonempty_counts_without_private_media_names(self):
        current = replace(snapshot("private body"), attachment_count=2, sticker_count=1)
        card = build_metadata_deletion_embed(current, deleted_at=DELETED_AT)
        self.assertEqual(fields(card)["첨부 정보"], "첨부파일 2개 · 스티커 1개 · 상세 내용 비공개")
        self.assertNotIn("임베드", fields(card)["첨부 정보"])

    def test_known_nonempty_media_without_captured_metadata_is_explicit(self):
        current = replace(snapshot(""), attachment_count=1, attachments_available=False)
        card = build_deletion_embed(current, deleted_at=DELETED_AT)
        self.assertIn("첨부파일 · 1개", fields(card))
        self.assertIn("메타데이터 확보 불가", fields(card)["첨부파일 · 1개"])
        self.assertNotIn("스티커", " ".join(fields(card)))

    def test_hidden_content_cannot_disclose_custom_emoji_names_or_free_form_reason(self):
        current = replace(snapshot("secret <:private:123456>"),
                          reason=replace(snapshot().reason, detail="secret detail", detected_root="secret root"))
        card = build_deletion_embed(current, include_content=False, deleted_at=DELETED_AT)
        for secret in ("secret", "private", "123456"):
            self.assertNotIn(secret, str(card.to_dict()))


if __name__ == "__main__":
    unittest.main()
