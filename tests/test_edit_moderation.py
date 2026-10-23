"""Offline tests of the real edit policy, revision ledger and history worker."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from edit_moderation import EditModerationConfig, EditModerationService, evaluate_policy
from moderation_rules import LINK_REGEX, find_badroot


UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def policy(message):
    return evaluate_policy(message.content, message.channel.id,
                           parent_channel_id=getattr(message.channel, "parent_id", None),
                           link_regex=LINK_REGEX, find_badroot=find_badroot,
                           allowed_link_channel_ids={99}, exempt_profanity_channel_ids={98})


class Channel:
    def __init__(self, channel_id=10, guild=None, messages=()):
        self.id = channel_id
        self.guild = guild or SimpleNamespace(id=1, me=object())
        self.type = SimpleNamespace(name="text")
        self.permissions = SimpleNamespace(view_channel=True, read_message_history=True, manage_messages=True)
        self.messages = list(messages)
        self.fetch_message = AsyncMock()
        self.get_partial_message = Mock(return_value=SimpleNamespace(delete=AsyncMock()))
        self.history_calls = []

    def permissions_for(self, _member):
        return self.permissions

    async def history(self, *, after, before, limit, oldest_first):
        self.history_calls.append((after, before, limit))
        count = 0
        for message in sorted(self.messages, key=lambda item: item.created_at, reverse=True):
            if after < message.created_at < before:
                if count >= limit:
                    break
                count += 1
                yield message


def message(content="hello", *, message_id=123, channel=None, edited=True, created_at=None, author_id=42):
    channel = channel or Channel()
    return SimpleNamespace(id=message_id, content=content, channel=channel, guild=channel.guild,
                           author=SimpleNamespace(id=author_id, bot=False),
                           edited_at=NOW if edited else None,
                           created_at=created_at or NOW - timedelta(hours=1), attachments=[], stickers=[], embeds=[])


def payload(current, *, content=True, cached=True, **updates):
    data = {"id": str(current.id), "channel_id": str(current.channel.id), "edited_timestamp": NOW.isoformat()}
    if content:
        data["content"] = current.content
    data.update(updates)
    return SimpleNamespace(message_id=current.id, channel_id=current.channel.id, guild_id=current.guild.id,
                           cached_message=current if cached else None, data=data)


def service(channels=(), *, config=None, **kwargs):
    channels = list(channels) or [Channel()]
    by_id = {item.id: item for item in channels}
    guilds = {}
    for channel in channels:
        guilds.setdefault(channel.guild.id, channel.guild)
    for guild in guilds.values():
        guild.text_channels = [channel for channel in channels if channel.guild.id == guild.id]
        guild.threads = []
    bot = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=by_id.get,
                          guilds=list(guilds.values()), wait_until_ready=AsyncMock())
    dependencies = {"evaluate": policy, "delete": AsyncMock(return_value=True), "consume_pass": Mock(return_value=False),
                    "has_pass": Mock(return_value=False), "remember": Mock(), "clock": lambda: NOW,
                    "log_channel_ids": {777}}
    dependencies.update(kwargs)
    return EditModerationService(bot, config=config or EditModerationConfig(), **dependencies)


class PurePolicyTests(unittest.TestCase):
    def test_clean_link_profanity_combined_and_normalized_input(self):
        for text, reason in (("1", None), ("시321133발", "profanity"),
                             ("https://example.com", "link"), ("시발 https://example.com", "link+profanity")):
            with self.subTest(text=text):
                original = message(text)
                result = policy(original)
                self.assertEqual(result.reason if result else None, reason)
                self.assertEqual(original.content, text)
                if reason and "profanity" in reason:
                    self.assertEqual(result.matched_rule, find_badroot(text))

    def test_parent_exceptions_and_distinct_rules(self):
        thread = Channel()
        thread.parent_id = 99
        self.assertIsNone(policy(message("https://example.com", channel=thread)))
        self.assertEqual(policy(message("시발", channel=thread)).reason, "profanity")
        thread.parent_id = 98
        self.assertIsNone(policy(message("시발", channel=thread)))
        self.assertEqual(policy(message("https://example.com", channel=thread)).reason, "link")

    def test_defaults_coverage_gap_and_invalid_config(self):
        defaults = EditModerationConfig.from_env({})
        self.assertEqual((defaults.interval_hours, defaults.lookback_hours), (72, 96))
        self.assertEqual((defaults.startup_delay_seconds, defaults.max_messages_per_channel, defaults.concurrency), (45, 2000, 1))
        with self.assertLogs("edit_moderation", level="WARNING") as logs:
            covered = EditModerationConfig.from_env({"EDIT_MODERATION_SCAN_LOOKBACK_HOURS": "48"})
        self.assertEqual(covered.lookback_hours, 96)
        self.assertIn("scan_coverage_gap", " ".join(logs.output))
        for invalid in ("nan", "inf", "-3", "oops", '"72"'):
            with self.subTest(invalid=invalid), self.assertLogs("edit_moderation", level="WARNING"):
                actual = EditModerationConfig.from_env({"EDIT_MODERATION_SCAN_INTERVAL_HOURS": invalid})
                self.assertEqual(actual.interval_hours, 72)
        self.assertGreaterEqual(defaults.ledger_ttl_seconds, (72 + 96) * 3600)


class EditEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_only_edits_share_policy_and_allow_channel_and_counted_pass_exceptions(self):
        for link in ("https://naver.com", "www.naver.com", "discord.gg/example", "discord.com/invite/example"):
            with self.subTest(link=link):
                channel = Channel()
                worker = service([channel])
                before, after = message("ㅇ", channel=channel, edited=False), message(link, channel=channel)
                worker.record_new_outcome(before, "clean")
                self.assertEqual(await worker.on_raw_edit(payload(after)), "deleted")
                self.assertEqual(worker.delete.call_args.kwargs["reason"], "link")
                self.assertIsNone(worker.delete.call_args.kwargs["matched_rule"])
                await worker.on_cached_edit(before, after)
                worker.delete.assert_awaited_once()
        for channel_id, has_pass, expected in ((99, False, "clean"), (10, True, "exempt")):
            with self.subTest(channel_id=channel_id, has_pass=has_pass):
                channel = Channel(channel_id)
                worker = service([channel], consume_pass=Mock(return_value=has_pass))
                current = message("https://naver.com", channel=channel)
                self.assertEqual(await worker.on_raw_edit(payload(current)), expected)
                self.assertEqual(await worker.on_cached_edit(message("ㅇ", channel=channel), current), expected)
                worker.delete.assert_not_awaited()
                self.assertEqual(worker.consume_pass.call_count, int(has_pass))

    async def test_naver_link_preview_partial_edit_fetches_actual_current_body(self):
        channel = Channel()
        worker = service([channel])
        before = message("ㅇ", channel=channel, edited=False)
        worker.record_new_outcome(before, "clean")
        current = message("https://naver.com", channel=channel)
        channel.fetch_message.return_value = current
        raw = payload(before, content=False)
        raw.data = {"id": str(current.id), "channel_id": str(channel.id),
                    "embeds": [{"type": "link", "url": "https://naver.com"}]}
        self.assertEqual(await worker.on_raw_edit(raw), "deleted")
        channel.fetch_message.assert_awaited_once_with(current.id)
        worker.delete.assert_awaited_once()
        self.assertEqual(worker.delete.call_args.args[0].content, "https://naver.com")
        self.assertEqual(worker.delete.call_args.kwargs["reason"], "link")
        self.assertEqual(await worker.on_cached_edit(before, current), "already_deleted")
        worker.delete.assert_awaited_once()

    async def test_preview_url_never_replaces_or_invents_the_message_body(self):
        channel = Channel()
        worker = service([channel])
        current = message("ㅇ", channel=channel)
        channel.fetch_message.return_value = current
        raw = payload(current, content=False)
        raw.data = {"id": str(current.id), "embeds": [{"type": "link", "url": "https://naver.com"}]}
        self.assertEqual(await worker.on_raw_edit(raw), "clean")
        channel.fetch_message.assert_awaited_once()
        worker.delete.assert_not_awaited()
        worker.consume_pass.assert_not_called()

    async def test_current_link_edit_does_not_need_unnecessary_history_permission(self):
        channel = Channel()
        channel.permissions.read_message_history = False
        worker = service([channel])
        current = message("https://naver.com", channel=channel)
        self.assertEqual(await worker.on_raw_edit(payload(current)), "deleted")
        channel.fetch_message.assert_not_awaited()
        worker.delete.assert_awaited_once()

    async def test_missing_body_requires_history_permission_before_fetch(self):
        channel = Channel()
        channel.permissions.read_message_history = False
        worker = service([channel])
        current = message("https://naver.com", channel=channel)
        self.assertEqual(await worker.on_raw_edit(payload(current, content=False)), "history_forbidden")
        channel.fetch_message.assert_not_awaited()
        worker.delete.assert_not_awaited()

    async def test_safe_original_then_edit_deletes_current_body_once(self):
        channel = Channel()
        worker = service([channel])
        before = message("1", channel=channel, edited=False)
        self.assertEqual(await worker.inspect(before, trigger="message_new"), "clean")
        after = message("시발", channel=channel)
        self.assertEqual(await worker.on_cached_edit(before, after), "deleted")
        self.assertEqual(await worker.on_raw_edit(payload(after)), "already_deleted")
        worker.delete.assert_awaited_once()
        current = worker.delete.call_args.args[0]
        self.assertEqual(current.content, "시발")
        self.assertEqual(worker.delete.call_args.kwargs["trigger"], "message_edit")
        self.assertEqual(worker.delete.call_args.kwargs["matched_rule"], find_badroot("시발"))

    async def test_raw_first_cached_second_uncached_content_and_metadata(self):
        channel = Channel()
        worker = service([channel])
        after = message("시321133발 😀", channel=channel)
        raw = payload(after, cached=False, author={"id": "42", "username": "Member"},
                      attachments=[{"id": "1", "filename": "SPOILER_photo.png", "content_type": "image/png", "size": 64}],
                      sticker_items=[{"id": "2", "name": "Bear", "format_type": 1}])
        self.assertEqual(await worker.on_raw_edit(raw), "deleted")
        await worker.on_cached_edit(message("1", channel=channel), after)
        worker.delete.assert_awaited_once()
        current = worker.delete.call_args.args[0]
        self.assertEqual(current.content, after.content)
        self.assertTrue(current.attachments[0].is_spoiler())
        self.assertEqual(current.stickers[0].name, "Bear")
        channel.fetch_message.assert_not_awaited()
        await current.delete()
        channel.get_partial_message.assert_called_once_with(after.id)

    async def test_raw_cached_message_contains_before_content_never_reused(self):
        channel = Channel()
        worker = service([channel])
        old = message("1", channel=channel, edited=False)
        raw = payload(old)
        raw.data["content"] = "시발"
        await worker.on_raw_edit(raw)
        self.assertEqual(worker.delete.call_args.args[0].content, "시발")

    async def test_modern_raw_payload_updated_message_used_with_full_metadata(self):
        channel = Channel()
        worker = service([channel])
        current = message("시발", channel=channel)
        current.attachments = [object()]
        raw = payload(current, cached=False)
        raw.message = current
        self.assertEqual(await worker.on_raw_edit(raw), "deleted")
        self.assertIs(worker.delete.call_args.args[0], current)
        channel.fetch_message.assert_not_awaited()

    async def test_partial_fetch_empty_content_and_embed_only(self):
        channel = Channel()
        worker = service([channel])
        current = message("시발", channel=channel)
        channel.fetch_message.return_value = current
        self.assertEqual(await worker.on_raw_edit(payload(current, content=False, cached=False)), "deleted")
        channel.fetch_message.assert_awaited_once_with(current.id)
        empty = message("", channel=channel, message_id=124)
        self.assertEqual(await worker.on_raw_edit(payload(empty)), "clean")
        raw = payload(empty)
        raw.data = {"id": str(empty.id), "embeds": [{"type": "rich"}]}
        self.assertEqual(await worker.on_raw_edit(raw), "embed_only")
        self.assertEqual(channel.fetch_message.await_count, 1)

    async def test_raw_fetch_failures_classified_and_content_not_logged(self):
        channel = Channel()
        current = message("SECRET_BODY https://secret.invalid", channel=channel)
        for status, expected in ((404, "not_found"), (403, "forbidden"), (429, "rate_limited"), (500, "internal_error")):
            worker = service([channel])
            error = RuntimeError(current.content)
            error.status = status
            channel.fetch_message.side_effect = error
            with self.assertLogs("edit_moderation", level="INFO") as logs:
                result = await worker.on_raw_edit(payload(current, content=False, cached=False))
            self.assertEqual(result, expected)
            self.assertNotIn("SECRET_BODY", " ".join(logs.output))
            self.assertNotIn("https://secret.invalid", " ".join(logs.output))
            worker.delete.assert_not_awaited()

    async def test_fetch_timeout_and_concurrency_bound(self):
        channel = Channel()
        active = maximum = 0
        async def fetch(message_id):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            try:
                await asyncio.sleep(.02)
            finally:
                active -= 1
        channel.fetch_message.side_effect = fetch
        worker = service([channel], config=replace(EditModerationConfig(), fetch_timeout_seconds=.001))
        results = await asyncio.gather(*(worker.on_raw_edit(payload(message(channel=channel, message_id=i), content=False, cached=False)) for i in range(6)))
        self.assertEqual(set(results), {"timeout"})
        self.assertLessEqual(maximum, 2)

    async def test_concurrent_same_revision_and_different_users(self):
        channel = Channel()
        started, release = asyncio.Event(), asyncio.Event()
        async def deletion(_message, **_kwargs):
            started.set()
            await release.wait()
            return True
        worker = service([channel], delete=AsyncMock(side_effect=deletion))
        current = message("시발", channel=channel)
        jobs = [asyncio.create_task(worker.inspect(current)) for _ in range(5)]
        await started.wait()
        another = asyncio.create_task(worker.inspect(message("시발", channel=channel, message_id=124, author_id=43)))
        await asyncio.sleep(0)
        self.assertEqual(worker.delete.await_count, 2)
        release.set()
        await asyncio.gather(*jobs, another)
        self.assertEqual(worker.delete.await_count, 2)
        self.assertEqual(worker._inflight, {})

    async def test_pass_consumed_only_once_across_two_event_orders_and_scan(self):
        channel = Channel()
        worker = service([channel], consume_pass=Mock(return_value=True))
        current = message("시발 https://example.com", channel=channel)
        results = await asyncio.gather(worker.on_raw_edit(payload(current)), worker.on_cached_edit(message("1", channel=channel), current))
        self.assertEqual(results, ["exempt", "exempt"])
        self.assertEqual(await worker.inspect(current, trigger="history_scan"), "exempt")
        worker.consume_pass.assert_called_once_with(42)
        worker.delete.assert_not_awaited()
        # Missing raw timestamp versus the cached timestamp cannot double-spend.
        raw = payload(current)
        raw.data.pop("edited_timestamp")
        channel.fetch_message.return_value = current
        self.assertEqual(await worker.on_raw_edit(raw), "exempt")
        worker.consume_pass.assert_called_once()

    async def test_later_identical_bad_body_is_distinct_from_old_pass_revision(self):
        channel = Channel()
        worker = service([channel], consume_pass=Mock(side_effect=[True, False]))
        original = message("시발", channel=channel)
        self.assertEqual(await worker.inspect(original), "exempt")
        clean = message("hello", channel=channel)
        clean.edited_at = NOW + timedelta(minutes=1)
        self.assertEqual(await worker.inspect(clean), "clean")
        later_bad = message("시발", channel=channel)
        later_bad.edited_at = NOW + timedelta(minutes=2)
        self.assertEqual(await worker.inspect(later_bad), "deleted")
        self.assertEqual(worker.consume_pass.call_count, 2)
        worker.delete.assert_awaited_once()

    async def test_missing_timestamp_raw_old_body_fetches_newer_current_state(self):
        channel = Channel()
        worker = service([channel])
        current = message("hello", channel=channel)
        self.assertEqual(await worker.inspect(current), "clean")
        old_raw = payload(message("시발", channel=channel))
        old_raw.data.pop("edited_timestamp")
        channel.fetch_message.return_value = current
        self.assertEqual(await worker.on_raw_edit(old_raw), "clean")
        channel.fetch_message.assert_awaited_once()
        worker.delete.assert_not_awaited()

    async def test_explicit_new_outcome_bridge_and_no_raw_state(self):
        channel = Channel()
        worker = service([channel])
        current = message("SECRET_BODY 시발 https://secret.invalid", channel=channel)
        worker.record_new_outcome(current, "exempt")
        self.assertEqual(await worker.inspect(current), "exempt")
        self.assertNotIn("SECRET_BODY", repr(worker._outcomes))
        self.assertNotIn("secret.invalid", repr(worker._content_outcomes))
        worker.consume_pass.assert_not_called()

    async def test_old_online_edit_not_limited_by_history_window(self):
        channel = Channel()
        worker = service([channel])
        current = message("시발", channel=channel, created_at=NOW - timedelta(days=90))
        self.assertEqual(await worker.on_raw_edit(payload(current)), "deleted")

    async def test_delete_failures_not_success_retry_after_short_ledger(self):
        clock = [0.0]
        channel = Channel()
        worker = service([channel], delete=AsyncMock(side_effect=[False, True]), monotonic=lambda: clock[0])
        current = message("시발", channel=channel)
        self.assertEqual(await worker.inspect(current), "delete_failed")
        self.assertEqual(await worker.inspect(current), "delete_failed")
        self.assertEqual(worker.delete.await_count, 1)
        clock[0] = 6
        self.assertEqual(await worker.inspect(current), "deleted")
        self.assertEqual(worker.delete.await_count, 2)

    async def test_eligibility_permissions_and_no_fetch_for_excluded(self):
        for kind in ("bot", "dm", "destination", "voice", "permission"):
            channel = Channel(777 if kind == "destination" else 10)
            current = message("시발", channel=channel)
            worker = service([channel])
            if kind == "bot":
                current.author.bot = True
            elif kind == "dm":
                current.guild = None
            elif kind == "voice":
                channel.type.name = "voice"
            elif kind == "permission":
                channel.permissions.manage_messages = False
            self.assertNotEqual(await worker.inspect(current), "deleted")
            worker.delete.assert_not_awaited()
            worker.consume_pass.assert_not_called()
        channel = Channel()
        channel.permissions.view_channel = False
        worker = service([channel])
        await worker.on_raw_edit(payload(message(channel=channel), content=False))
        channel.fetch_message.assert_not_awaited()

    async def test_stale_content_revision_skipped_after_newer_clean_revision(self):
        channel = Channel()
        worker = service([channel])
        newer = message("hello", channel=channel)
        await worker.inspect(newer)
        old = message("시발", channel=channel)
        old.edited_at = NOW - timedelta(minutes=1)
        self.assertEqual(await worker.inspect(old), "stale_revision")
        worker.delete.assert_not_awaited()

    async def test_ledger_ttl_and_size_cap(self):
        clock = [0.0]
        worker = service(config=replace(EditModerationConfig(), ledger_limit=3, ledger_ttl_seconds=10), monotonic=lambda: clock[0])
        for index in range(12):
            worker.record_new_outcome(message(str(index), message_id=index), "clean")
        self.assertLessEqual(len(worker._outcomes), 3)
        self.assertLessEqual(len(worker._content_outcomes), 3)
        clock[0] = 11
        self.assertIsNone(worker.known_outcome(message("11", message_id=11)))

    async def test_overwritten_delete_tombstone_does_not_keep_expired_siblings(self):
        clock = [0.0]
        worker = service(config=replace(EditModerationConfig(), ledger_ttl_seconds=10), monotonic=lambda: clock[0])
        first, second = message("a", message_id=1), message("b", message_id=2)
        worker.record_new_outcome(first, "deleted")
        clock[0] = 1
        worker.record_new_outcome(second, "deleted")
        clock[0] = 5
        worker.record_new_outcome(first, "deleted")
        clock[0] = 12
        self.assertEqual(worker.known_outcome(first), "deleted")
        self.assertIsNone(worker.known_outcome(second))

    async def test_unexpected_policy_exception_never_exposes_content(self):
        worker = service(evaluate=Mock(side_effect=RuntimeError("SECRET_BODY")))
        with self.assertLogs("edit_moderation", level="WARNING") as logs:
            self.assertEqual(await worker.inspect(message("SECRET_BODY")), "internal_error")
        self.assertNotIn("SECRET_BODY", " ".join(logs.output))
        self.assertEqual(worker._inflight, {})


class HistoryScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_scan_checks_link_edits_without_spending_passes(self):
        channel = Channel()
        channel.messages = [message("https://naver.com", channel=channel)]
        worker = service([channel])
        result = await worker.scan_once()
        self.assertEqual(result.deleted, 1)
        self.assertEqual(worker.delete.call_args.kwargs["reason"], "link")
        worker.consume_pass.assert_not_called()
        channel = Channel()
        channel.permissions.read_message_history = False
        worker = service([channel])
        result = await worker.scan_once()
        self.assertEqual(result.forbidden, 1)
        self.assertEqual(channel.history_calls, [])

    async def test_two_phases_cover_96_hours_and_creation_not_edit_time(self):
        channel = Channel()
        recent = message("시발", channel=channel, message_id=1)
        overlap = message("시발", channel=channel, message_id=2, created_at=NOW - timedelta(hours=80))
        outside = message("시발", channel=channel, message_id=3, created_at=NOW - timedelta(days=90))
        channel.messages = [outside, overlap, recent]
        worker = service([channel])
        result = await worker.scan_once()
        self.assertEqual((result.messages_examined, result.deleted), (2, 2))
        self.assertEqual([call.args[0].id for call in worker.delete.await_args_list], [1, 2])
        self.assertEqual(channel.history_calls[0][:2], (NOW - timedelta(hours=48, milliseconds=1), NOW))
        self.assertEqual(channel.history_calls[1][:2], (NOW - timedelta(hours=96, milliseconds=1), NOW - timedelta(hours=48) + timedelta(milliseconds=1)))
        worker.consume_pass.assert_not_called()
        self.assertFalse(result.scan_in_progress)
        self.assertEqual(result.next_run_at, NOW + timedelta(hours=72))

    async def test_utc_midnight_phase_boundary_included_once(self):
        channel = Channel()
        # Exactly the priority split would be excluded by two strict before /
        # after filters without the deliberate millisecond overlap.
        boundary = message("시발", channel=channel, created_at=NOW - timedelta(hours=48))
        channel.messages = [boundary]
        worker = service([channel])
        result = await worker.scan_once()
        self.assertEqual((result.messages_examined, result.deleted), (1, 1))
        worker.delete.assert_awaited_once()

    async def test_scan_never_spends_pass_and_skips_unknown_unedited_violation(self):
        channel = Channel()
        channel.messages = [message("시발", channel=channel, edited=False),
                            message("시발", channel=channel, message_id=124)]
        worker = service([channel], has_pass=Mock(return_value=True))
        result = await worker.scan_once()
        self.assertEqual(result.historical_exemption_unknown, 1)
        worker.consume_pass.assert_not_called()
        worker.delete.assert_not_awaited()
        self.assertEqual(result.deleted, 0)

    async def test_repeated_scan_reports_actual_deletions_only(self):
        channel = Channel()
        channel.messages = [message("시발", channel=channel)]
        worker = service([channel])
        self.assertEqual((await worker.scan_once()).deleted, 1)
        self.assertEqual((await worker.scan_once()).deleted, 0)
        worker.delete.assert_awaited_once()

    async def test_non_utc_clock_is_normalized_before_history_lookup(self):
        channel = Channel()
        worker = service([channel], clock=lambda: NOW.astimezone(timezone(timedelta(hours=-4))))
        result = await worker.scan_once()
        self.assertEqual(result.last_started, NOW)
        self.assertEqual(result.last_started.tzinfo, UTC)
        self.assertEqual(channel.history_calls[0][1], NOW)

    async def test_permission_exclusions_do_not_fetch_hidden_channel_names(self):
        channel = Channel()
        channel.name = "SECRET_HIDDEN_CHANNEL"
        channel.permissions.view_channel = False
        worker = service([channel])
        result = await worker.scan_once()
        self.assertEqual(result.forbidden, 1)
        self.assertEqual(channel.history_calls, [])
        self.assertNotIn(channel.name, worker.status_text(1))
        self.assertNotIn(channel.name, worker.status_text(2))

    async def test_known_exemption_ledger_survives_repeated_scans(self):
        channel = Channel()
        current = message("시발", channel=channel)
        channel.messages = [current]
        worker = service([channel])
        worker.record_new_outcome(current, "exempt")
        for _ in range(3):
            result = await worker.scan_once()
            self.assertEqual(result.deleted, 0)
        worker.delete.assert_not_awaited()
        worker.consume_pass.assert_not_called()

    async def test_large_channel_partial_cap_and_global_budget(self):
        channel = Channel()
        channel.messages = [message("hello", channel=channel, message_id=i) for i in range(3001)]
        worker = service([channel], config=replace(EditModerationConfig(), max_messages_per_channel=2000))
        result = await worker.scan_once()
        self.assertEqual(result.messages_examined, 2000)
        self.assertEqual(result.partial_window, 1)
        channels = [Channel(i + 1) for i in range(20)]
        for item in channels:
            item.messages = [message("hello", channel=item, message_id=item.id * 100 + i) for i in range(3)]
        worker = service(channels, config=replace(EditModerationConfig(), max_messages_total=7, concurrency=3))
        result = await worker.scan_once()
        self.assertEqual(result.messages_examined, 7)
        self.assertGreater(result.partial_window, 0)

    async def test_hundreds_of_channels_yield_and_keep_concurrency_bound(self):
        channels = [Channel(i + 1) for i in range(250)]
        active = maximum = 0
        order = []
        class TrackingChannel(Channel):
            async def history(self, **kwargs):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                order.append(kwargs["after"])
                await asyncio.sleep(0)
                active -= 1
                if False:
                    yield
        channels = [TrackingChannel(item.id, item.guild) for item in channels]
        worker = service(channels, config=replace(EditModerationConfig(), concurrency=2))
        await worker.scan_once()
        self.assertLessEqual(maximum, 2)
        self.assertEqual(len(order), 500)
        self.assertEqual(set(order[:250]), {NOW - timedelta(hours=48, milliseconds=1)})
        self.assertEqual(set(order[250:]), {NOW - timedelta(hours=96, milliseconds=1)})

    async def test_scan_error_metadata_status_guild_isolation_and_no_body(self):
        class FailedChannel(Channel):
            async def history(self, **kwargs):
                error = RuntimeError("SECRET_BODY https://secret.invalid")
                error.status = 429
                raise error
                yield
        channel = FailedChannel()
        worker = service([channel], sleep=AsyncMock())
        with self.assertLogs("edit_moderation", level="WARNING") as logs:
            result = await worker.scan_once()
        self.assertEqual(result.rate_limited, 2)
        self.assertEqual(result.partial_window, 1)
        text = worker.status_text(1)
        self.assertIn("72시간", text)
        self.assertIn("96시간", text)
        self.assertNotIn("SECRET_BODY", text + " ".join(logs.output))
        self.assertIn("검사 0", worker.status_text(2))
        self.assertNotIn(str(channel.id), worker.status_text(2).split("마지막 완료")[0])

    async def test_lifecycle_waits_ready_singleton_reconnect_and_cancellation(self):
        channel = Channel()
        ready, delayed = asyncio.Event(), asyncio.Event()
        durations = []
        async def wait_ready():
            await ready.wait()
        async def fake_sleep(seconds):
            durations.append(seconds)
            if seconds == 45:
                await delayed.wait()
            elif seconds > 100:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(0)
        worker = service([channel], sleep=fake_sleep)
        worker.bot.wait_until_ready.side_effect = wait_ready
        task = worker.start()
        self.assertIs(worker.start(), task)
        await asyncio.sleep(0)
        self.assertEqual(durations, [])
        ready.set()
        await asyncio.sleep(0)
        self.assertEqual(durations, [45])
        self.assertIs(worker.start(), task)
        delayed.set()
        for _ in range(30):
            await asyncio.sleep(0)
            if 72 * 3600 in durations:
                break
        self.assertIn(72 * 3600, durations)
        self.assertEqual(len(channel.history_calls), 2)
        await worker.close()
        self.assertIsNone(worker._task)

    async def test_disabled_scan_and_restart_are_explicit(self):
        worker = service(config=replace(EditModerationConfig(), scan_enabled=False))
        self.assertIsNone(worker.start())
        self.assertIsNone((await worker.scan_once()).last_started)
        self.assertIn("재시작", worker.status_text())
        self.assertEqual(service()._outcomes, {})

    async def test_status_shows_started_next_kst_and_rate_limit_count(self):
        worker = service()
        worker.status.last_started = NOW
        worker.status.next_run_at = NOW + timedelta(hours=72)
        worker.status.rate_limited = 3
        text = worker.status_text()
        self.assertIn("최근 시작: 10-22 21:00 KST", text)
        self.assertIn("다음 예정: 10-25 21:00 KST", text)
        self.assertIn("속도 제한 3", text)
        # An unscanned guild may see the content-free planned scheduler time,
        # while another guild's counters or start timestamp stay private.
        unscanned = worker.status_text(987)
        self.assertIn("최근 시작: 없음", unscanned)
        self.assertIn("다음 예정: 10-25 21:00 KST", unscanned)
        self.assertIn("속도 제한 0", unscanned)
        self.assertLess(len(text), 1000)

    async def test_cancelled_scan_is_interrupted_and_has_no_completion_time(self):
        started = asyncio.Event()
        class WaitingChannel(Channel):
            async def history(self, **kwargs):
                started.set()
                await asyncio.Event().wait()
                if False:
                    yield
        channel = WaitingChannel()
        worker = service([channel])
        task = asyncio.create_task(worker.scan_once())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(worker.status.interrupted)
        self.assertIsNone(worker.status.last_finished)
        self.assertFalse(worker.status.scan_in_progress)
        self.assertIn("중단됨", worker.status_text(1))


if __name__ == "__main__":
    unittest.main()
