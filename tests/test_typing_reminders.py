"""Exercise real typing timers and JSON state with isolated transports/files."""
from __future__ import annotations

import ast
import asyncio
import datetime
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class TextChannel:
    def __init__(self, channel_id=1, guild_id=1):
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.send = AsyncMock()
        self.messages = []
        self.history_queries = []

    def history(self, **kwargs):
        self.history_queries.append(kwargs)

        async def messages():
            for message in self.messages:
                yield message
        return messages()


class Thread(TextChannel):
    pass


class DMChannel:
    def __init__(self):
        self.id = 1
        self.guild = None


def user(user_id=42, *, bot=False):
    return SimpleNamespace(id=user_id, bot=bot, mention=f"<@{user_id}>")


def typing_functions(path):
    source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    names = {"_load_typing_cooldowns", "_save_typing_cooldowns", "_typing_scope_key",
             "_deliver_typing_reminder", "_send_typing_reminder", "on_typing"}
    constants = {"TYPE_REMINDER_COOLDOWN", "TYPE_REMINDER_CHANNEL_COOLDOWN"}
    nodes = []
    for node in source.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            nodes.append(node)
    # Only the timer delay is replaced; task scheduling, shielding and locks
    # remain real asyncio operations so cancellation races are exercised.
    asynchronous = SimpleNamespace(
        sleep=AsyncMock(), create_task=asyncio.create_task, shield=asyncio.shield,
        current_task=asyncio.current_task, CancelledError=asyncio.CancelledError,
    )
    namespace = {
        "asyncio": asynchronous, "datetime": datetime, "json": json, "os": os,
        "math": math, "logging": logging, "time": SimpleNamespace(time=Mock(return_value=200_000.0)),
        "discord": SimpleNamespace(TextChannel=TextChannel, Thread=Thread, DMChannel=DMChannel),
        "TYPE_REMINDER_COOLDOWN_FILE": path,
        "_last_typing_notice": {}, "_last_typing_channel_notice": {},
        "_typing_notice_send_lock": asyncio.Lock(), "_typing_tasks": {},
        "typing_intro_payload": Mock(return_value={"view": object(), "allowed_mentions": object()}),
        "_metric": Mock(),
    }
    for service in ("_question_interaction_service", "_image_interaction_service", "_growth_interaction_service", "_menu_interaction_service"):
        namespace[service] = AsyncMock()
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    return namespace


class TypingReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "typing_cooldowns.json"
        self.ns = typing_functions(self.path)

    async def timer(self, channel, actor):
        key = (channel.id, actor.id)
        pending = asyncio.create_task(self.ns["_send_typing_reminder"](channel, actor, key, self.ns["time"].time()))
        self.ns["_typing_tasks"][key] = pending
        await asyncio.wait_for(pending, timeout=2)
        return pending

    def write_state(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    async def test_real_policy_is_24_hours_and_exact_boundary_allows_next_notice(self):
        cooldown = self.ns["TYPE_REMINDER_COOLDOWN"]
        self.assertEqual(cooldown, datetime.timedelta(hours=24).total_seconds())
        channel, actor = TextChannel(), user()
        await self.timer(channel, actor)
        initial = self.ns["_last_typing_notice"]["user:42"]
        self.ns["time"].time.return_value = initial + cooldown - 1
        await self.timer(channel, actor)
        self.assertEqual(channel.send.await_count, 1)
        self.ns["time"].time.return_value = initial + cooldown
        await self.timer(channel, actor)
        self.assertEqual(channel.send.await_count, 2)
        self.ns["asyncio"].sleep.assert_has_awaits([unittest.mock.call(5), unittest.mock.call(5)])

    async def test_same_user_is_suppressed_across_channels_threads_and_guilds(self):
        actor = user()
        channels = [TextChannel(1, 1), TextChannel(2, 1), Thread(3, 1), TextChannel(4, 2)]
        for channel in channels:
            await self.timer(channel, actor)
        self.assertEqual([channel.send.await_count for channel in channels], [1, 0, 0, 0])
        self.assertEqual(set(self.ns["_last_typing_notice"]), {"user:42"})
        self.assertEqual(self.ns["_typing_scope_key"](channels[-1], actor), "user:42")

    async def test_users_are_independent_and_original_channel_throttle_remains(self):
        first, second = TextChannel(1), TextChannel(2)
        await self.timer(first, user(42))
        await self.timer(second, user(43))
        await self.timer(first, user(44))
        self.assertEqual([first.send.await_count, second.send.await_count], [1, 1])
        self.assertNotIn("user:44", self.ns["_last_typing_notice"])
        self.ns["time"].time.return_value += self.ns["TYPE_REMINDER_CHANNEL_COOLDOWN"]
        await self.timer(first, user(44))
        self.assertEqual(first.send.await_count, 2)
        self.assertIn("user:44", self.ns["_last_typing_notice"])

    async def test_parallel_same_user_timers_deliver_only_once(self):
        channels = [TextChannel(index, index) for index in range(1, 8)]
        await asyncio.gather(*(self.timer(channel, user()) for channel in channels))
        self.assertEqual(sum(channel.send.await_count for channel in channels), 1)
        self.assertEqual(len(self.ns["_last_typing_notice"]), 1)

    async def test_failed_send_has_no_cooldown_reservation_and_can_retry(self):
        channel = TextChannel()
        channel.send.side_effect = PermissionError("private transport detail")
        with self.assertLogs(level="WARNING") as captured:
            await self.timer(channel, user())
        self.assertEqual(self.ns["_last_typing_notice"], {})
        self.assertEqual(self.ns["_last_typing_channel_notice"], {})
        self.assertFalse(self.path.exists())
        self.assertNotIn("private transport detail", "\n".join(captured.output))
        channel.send.side_effect = None
        await self.timer(channel, user())
        self.assertEqual(channel.send.await_count, 2)
        self.assertIn("user:42", self.ns["_last_typing_notice"])

    async def test_metric_failure_cannot_erase_successfully_saved_cooldown(self):
        channel = TextChannel()
        self.ns["_metric"].side_effect = RuntimeError("metric failure")
        with self.assertLogs(level="WARNING"):
            await self.timer(channel, user())
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved, self.ns["_last_typing_notice"])
        self.assertIn("user:42", saved)
        await self.timer(TextChannel(2), user())
        channel.send.assert_awaited_once()
        self.ns["_metric"].assert_called_once_with("intro_viewed")

    async def test_delay_and_requester_message_prevent_unwanted_notice(self):
        channel = TextChannel()
        channel.messages = [SimpleNamespace(author=user())]
        await self.timer(channel, user())
        self.ns["asyncio"].sleep.assert_awaited_once_with(5)
        channel.send.assert_not_awaited()
        self.assertEqual(self.ns["_last_typing_notice"], {})
        self.assertEqual(channel.history_queries[0]["limit"], 1)

    async def test_on_typing_does_not_schedule_during_user_cooldown_or_for_bots_and_dms(self):
        created = Mock(wraps=asyncio.create_task)
        self.ns["asyncio"].create_task = created
        self.ns["_last_typing_notice"]["user:42"] = self.ns["time"].time()
        when = datetime.datetime.now(datetime.timezone.utc)
        await self.ns["on_typing"](TextChannel(99, 99), user(), when)
        await self.ns["on_typing"](TextChannel(), user(44, bot=True), when)
        await self.ns["on_typing"](DMChannel(), user(45), when)
        created.assert_not_called()
        self.assertEqual(self.ns["_typing_tasks"], {})

    async def test_normal_task_cleans_its_own_tracking_entry(self):
        await self.timer(TextChannel(), user())
        self.assertEqual(self.ns["_typing_tasks"], {})

    async def test_cancelled_old_timer_cannot_remove_replacement_tracking(self):
        entered = asyncio.Event()

        async def delay(_seconds):
            entered.set()
            await asyncio.Event().wait()

        self.ns["asyncio"].sleep.side_effect = delay
        channel, actor = TextChannel(), user()
        key = (channel.id, actor.id)
        old = asyncio.create_task(self.ns["_send_typing_reminder"](channel, actor, key, self.ns["time"].time()))
        self.ns["_typing_tasks"][key] = old
        await asyncio.wait_for(entered.wait(), timeout=2)
        old.cancel()
        replacement = object()
        self.ns["_typing_tasks"][key] = replacement
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(old, timeout=2)
        self.assertIs(self.ns["_typing_tasks"][key], replacement)
        channel.send.assert_not_awaited()

    async def test_repeated_typing_replaces_timer_without_losing_new_task(self):
        entered = asyncio.Event()

        async def delay(_seconds):
            entered.set()
            await asyncio.Event().wait()

        self.ns["asyncio"].sleep.side_effect = delay
        channel, actor = TextChannel(), user()
        key = (channel.id, actor.id)
        when = datetime.datetime.now(datetime.timezone.utc)
        await self.ns["on_typing"](channel, actor, when)
        old = self.ns["_typing_tasks"][key]
        await asyncio.wait_for(entered.wait(), timeout=2)
        await self.ns["on_typing"](channel, actor, when)
        replacement = self.ns["_typing_tasks"][key]
        self.assertIsNot(old, replacement)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(old, timeout=2)
        self.assertIs(self.ns["_typing_tasks"][key], replacement)
        replacement.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(replacement, timeout=2)

    async def test_cancellation_during_send_keeps_worker_locked_until_state_is_persisted(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first_channel, other_channel = TextChannel(1), TextChannel(2)

        async def accepted_send(**_kwargs):
            entered.set()
            await release.wait()

        first_channel.send.side_effect = accepted_send
        actor = user()
        key = (first_channel.id, actor.id)
        first = asyncio.create_task(self.ns["_send_typing_reminder"](first_channel, actor, key, self.ns["time"].time()))
        self.ns["_typing_tasks"][key] = first
        await asyncio.wait_for(entered.wait(), timeout=2)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertTrue(self.ns["_typing_notice_send_lock"].locked())
        self.assertEqual(self.ns["_last_typing_notice"], {})
        second = asyncio.create_task(self.timer(other_channel, actor))
        await asyncio.sleep(0)

        def metric_after_save(_event):
            self.assertTrue(self.ns["_typing_notice_send_lock"].locked())
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.ns["_last_typing_notice"])

        self.ns["_metric"].side_effect = metric_after_save
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=2)
        await asyncio.wait_for(second, timeout=2)
        first_channel.send.assert_awaited_once()
        other_channel.send.assert_not_awaited()
        self.assertIn("user:42", json.loads(self.path.read_text(encoding="utf-8")))
        self.assertFalse(self.ns["_typing_notice_send_lock"].locked())

    async def test_cancellation_and_failed_send_preserve_original_cancellation_without_cooldown(self):
        entered, release = asyncio.Event(), asyncio.Event()
        channel = TextChannel()

        async def failed_send(**_kwargs):
            entered.set()
            await release.wait()
            raise PermissionError("private transport detail")

        channel.send.side_effect = failed_send
        key = (channel.id, 42)
        pending = asyncio.create_task(self.ns["_send_typing_reminder"](channel, user(), key, self.ns["time"].time()))
        self.ns["_typing_tasks"][key] = pending
        await asyncio.wait_for(entered.wait(), timeout=2)
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertLogs(level="WARNING") as captured:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=2)
        self.assertEqual(self.ns["_last_typing_notice"], {})
        self.assertFalse(self.path.exists())
        self.assertFalse(self.ns["_typing_notice_send_lock"].locked())
        self.assertNotIn("private transport detail", "\n".join(captured.output))
        self.assertNotIn(key, self.ns["_typing_tasks"])

    async def test_successful_save_creates_directory_and_round_trips_after_restart(self):
        self.ns["_last_typing_notice"] = {"user:42": 123.5, "user:43": 234.5}
        self.ns["_save_typing_cooldowns"]()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        restarted = typing_functions(self.path)
        self.assertEqual(restarted["_load_typing_cooldowns"](), self.ns["_last_typing_notice"])

    async def test_legacy_channels_raw_ids_and_current_ids_merge_newest_per_user(self):
        self.write_state({"1:10:42": 100, "2:20:42": 300, "user:42": 200,
                          "00042": 400, "3:30:43": 500, "user:00043": 450})
        loaded = self.ns["_load_typing_cooldowns"]()
        self.assertEqual(loaded, {"user:42": 400, "user:43": 500})
        self.ns["_last_typing_notice"] = loaded
        self.ns["time"].time.return_value = 400 + self.ns["TYPE_REMINDER_COOLDOWN"] - 1
        channel = TextChannel(900, 900)
        await self.timer(channel, user())
        channel.send.assert_not_awaited()

    async def test_invalid_entries_do_not_drop_other_users_and_nonfinite_values_are_rejected(self):
        self.write_state({
            "user:42": 123, "user:43": "456.5", "1:2:44": 789,
            "user:45": None, "user:46": True, "user:47": False,
            "user:48": math.nan, "user:49": math.inf, "user:50": -math.inf,
            "user:51": "NaN", "user:52": {}, "user:53": -1, "user:54": 0,
            "user:0": 123, "user:-1": 123, "user:１２": 123,
            "user:not-an-id": 123, "bad:channel:42": 999, "user:": 999,
            f"user:{2**64}": 123, "user:" + "9" * 5000: 123,
        })
        self.assertEqual(self.ns["_load_typing_cooldowns"](), {"user:42": 123, "user:43": 456.5, "user:44": 789})

    async def test_missing_nonobject_and_malformed_files_are_safe(self):
        self.assertEqual(self.ns["_load_typing_cooldowns"](), {})
        for invalid in (None, [], ["user:42"], "secret raw value", 42):
            with self.subTest(invalid=invalid):
                self.write_state(invalid)
                with self.assertLogs(level="WARNING") as captured:
                    self.assertEqual(self.ns["_load_typing_cooldowns"](), {})
                self.assertNotIn("secret raw value", "\n".join(captured.output))
        self.path.write_text("{ broken", encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.ns["_load_typing_cooldowns"](), {})

    async def test_invalid_save_preserves_previous_file_and_cleans_temporary_file(self):
        for invalid in (math.nan, object()):
            with self.subTest(invalid=type(invalid).__name__):
                self.write_state({"user:42": 123})
                before = self.path.read_bytes()
                self.ns["_last_typing_notice"] = {"user:42": invalid}
                with self.assertLogs(level="WARNING"):
                    self.ns["_save_typing_cooldowns"]()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.path.with_suffix(".tmp").exists())

    async def test_replace_failure_preserves_previous_file_and_cleans_temporary_file(self):
        self.write_state({"user:42": 123})
        before = self.path.read_bytes()
        self.ns["_last_typing_notice"] = {"user:42": 456}
        with patch.object(os, "replace", side_effect=OSError("private filesystem detail")):
            with self.assertLogs(level="WARNING") as captured:
                self.ns["_save_typing_cooldowns"]()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        self.assertNotIn("private filesystem detail", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
