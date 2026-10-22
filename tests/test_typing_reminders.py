"""Real timer scheduling, concurrent transport and persisted-state regressions."""
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
        self.history = Mock(side_effect=AssertionError("typing must not fetch history"))


class Thread(TextChannel):
    pass


class DMChannel:
    def __init__(self):
        self.id = 1
        self.guild = None


def user(user_id=42, *, bot=False):
    return SimpleNamespace(id=user_id, bot=bot, mention=f"<@{user_id}>")


def message(channel, actor=None, content="안녕하세요"):
    return SimpleNamespace(channel=channel, guild=channel.guild, author=actor or user(), content=content)


def typing_functions(path):
    source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    names = {
        "_typing_intro_delay", "_load_typing_cooldowns", "_save_typing_cooldowns", "_typing_scope_key",
        "_typing_notice_on_cooldown", "_persist_typing_cooldowns", "_deliver_typing_reminder",
        "_send_typing_reminder", "_schedule_typing_reminder", "_observe_typing_message",
        "_typing_message_fallback", "on_typing",
    }
    constants = {"TYPE_REMINDER_COOLDOWN", "TYPING_INTRO_DELAY_SECONDS"}
    nodes = []
    for node in source.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            nodes.append(node)
    asynchronous = SimpleNamespace(
        sleep=AsyncMock(), create_task=asyncio.create_task, shield=asyncio.shield,
        current_task=asyncio.current_task, CancelledError=asyncio.CancelledError, to_thread=asyncio.to_thread,
    )
    namespace = {
        "asyncio": asynchronous, "datetime": datetime, "json": json, "os": os,
        "math": math, "logging": logging, "time": SimpleNamespace(time=Mock(return_value=200_000.0)),
        "discord": SimpleNamespace(TextChannel=TextChannel, Thread=Thread, DMChannel=DMChannel),
        "TYPE_REMINDER_COOLDOWN_FILE": path,
        "_last_typing_notice": {}, "_typing_notice_inflight": set(),
        "_typing_state_save_lock": asyncio.Lock(), "_typing_tasks": {},
        "typing_intro_payload": Mock(return_value={"view": object(), "allowed_mentions": object()}),
        "_metric": Mock(),
    }
    for service in ("_question_interaction_service", "_image_interaction_service", "_growth_interaction_service", "_menu_interaction_service"):
        namespace[service] = AsyncMock()
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    with patch.dict(os.environ, {"TYPING_INTRO_DELAY_SECONDS": "1.3"}):
        exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    return namespace


class TypingReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "typing_cooldowns.json"
        self.ns = typing_functions(self.path)

    async def asyncTearDown(self):
        tasks = list(self.ns["_typing_tasks"].values())
        for task in tasks:
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if isinstance(task, asyncio.Task)), return_exceptions=True)

    async def timer(self, channel, actor):
        key = (channel.guild.id, actor.id)
        pending = asyncio.create_task(self.ns["_send_typing_reminder"](channel, actor, key, self.ns["time"].time()))
        self.ns["_typing_tasks"][key] = pending
        await asyncio.wait_for(pending, timeout=2)
        return pending

    def write_state(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")

    async def drain(self):
        await asyncio.gather(*list(self.ns["_typing_tasks"].values()))

    async def test_delay_default_under_1500ms_and_configuration_validation(self):
        self.assertEqual(self.ns["TYPING_INTRO_DELAY_SECONDS"], 1.3)
        for configured in ("0.1", "1.2", "1.5"):
            with patch.dict(os.environ, {"TYPING_INTRO_DELAY_SECONDS": configured}):
                self.assertEqual(self.ns["_typing_intro_delay"](), float(configured))
        for invalid in ("0", "-1", "1.51", "5", "nan", "inf", "\"1.3\"", "secret-invalid"):
            with patch.dict(os.environ, {"TYPING_INTRO_DELAY_SECONDS": invalid}), self.assertLogs(level="WARNING") as logs:
                self.assertEqual(self.ns["_typing_intro_delay"](), 1.3)
            self.assertNotIn("secret-invalid", "\n".join(logs.output))

    async def test_policy_is_24_hours_and_exact_boundary_allows_next_notice(self):
        cooldown = self.ns["TYPE_REMINDER_COOLDOWN"]
        self.assertEqual(cooldown, datetime.timedelta(hours=24).total_seconds())
        channel = TextChannel()
        await self.timer(channel, user())
        initial = self.ns["_last_typing_notice"]["guild:1:user:42"]
        self.ns["time"].time.return_value = initial + cooldown - 1
        await self.timer(channel, user())
        self.assertEqual(channel.send.await_count, 1)
        self.ns["time"].time.return_value = initial + cooldown
        await self.timer(channel, user())
        self.assertEqual(channel.send.await_count, 2)
        self.ns["asyncio"].sleep.assert_has_awaits([unittest.mock.call(1.3), unittest.mock.call(1.3)])

    async def test_same_user_suppressed_across_channels_threads_but_guilds_independent(self):
        channels = [TextChannel(1, 1), TextChannel(2, 1), Thread(3, 1), TextChannel(4, 2)]
        for channel in channels:
            await self.timer(channel, user())
        self.assertEqual([channel.send.await_count for channel in channels], [1, 0, 0, 1])
        self.assertEqual(set(self.ns["_last_typing_notice"]), {"guild:1:user:42", "guild:2:user:42"})

    async def test_many_users_in_one_channel_all_receive_notice(self):
        channel = TextChannel()
        await asyncio.gather(*(self.timer(channel, user(index)) for index in range(41, 48)))
        self.assertEqual(channel.send.await_count, 7)
        self.assertEqual(len(self.ns["_last_typing_notice"]), 7)
        channel.history.assert_not_called()

    async def test_parallel_same_guild_user_timers_deliver_only_once(self):
        channels = [TextChannel(index, 1) for index in range(1, 8)]
        await asyncio.gather(*(self.timer(channel, user()) for channel in channels))
        self.assertEqual(sum(channel.send.await_count for channel in channels), 1)
        self.assertEqual(len(self.ns["_last_typing_notice"]), 1)

    async def test_slow_send_does_not_block_other_users_or_guilds(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first, other, other_guild = TextChannel(1, 1), TextChannel(1, 1), TextChannel(1, 2)
        async def slow_send(**_kwargs):
            entered.set()
            await release.wait()
        first.send.side_effect = slow_send
        pending = asyncio.create_task(self.timer(first, user()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await asyncio.wait_for(self.timer(other, user(43)), timeout=2)
            await asyncio.wait_for(self.timer(other_guild, user()), timeout=2)
            self.assertFalse(pending.done())
            self.assertFalse(self.ns["_typing_state_save_lock"].locked())
        finally:
            release.set()
            await pending
        self.assertEqual([first.send.await_count, other.send.await_count, other_guild.send.await_count], [1, 1, 1])
        self.assertEqual(json.loads(self.path.read_text()), self.ns["_last_typing_notice"])

    async def test_failed_send_rolls_back_reservation_and_allows_retry(self):
        channel = TextChannel()
        channel.send.side_effect = PermissionError("private transport detail")
        with self.assertLogs(level="WARNING") as logs:
            await self.timer(channel, user())
        self.assertEqual(self.ns["_last_typing_notice"], {})
        self.assertEqual(self.ns["_typing_notice_inflight"], set())
        self.assertFalse(self.path.exists())
        self.assertNotIn("private transport detail", "\n".join(logs.output))
        channel.send.side_effect = None
        await self.timer(channel, user())
        self.assertEqual(channel.send.await_count, 2)

    async def test_metric_failure_keeps_successfully_persisted_cooldown(self):
        channel = TextChannel()
        self.ns["_metric"].side_effect = RuntimeError("private metric failure")
        with self.assertLogs(level="WARNING"):
            await self.timer(channel, user())
        self.assertEqual(json.loads(self.path.read_text()), self.ns["_last_typing_notice"])
        await self.timer(TextChannel(2), user())
        channel.send.assert_awaited_once()
        self.ns["_metric"].assert_called_once_with("intro_viewed")

    async def test_no_history_permission_needed_and_stopping_typing_still_sends_once(self):
        channel = TextChannel()
        await self.ns["on_typing"](channel, user(), None)
        await self.drain()
        self.ns["asyncio"].sleep.assert_awaited_once_with(1.3)
        channel.history.assert_not_called()
        channel.send.assert_awaited_once()
        self.assertEqual(self.ns["_typing_tasks"], {})

    async def test_repeated_typing_keeps_first_timer_even_in_another_channel(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delay(_seconds):
            entered.set()
            await release.wait()
        self.ns["asyncio"].sleep.side_effect = delay
        first, later = TextChannel(1), TextChannel(2)
        await self.ns["on_typing"](first, user(), None)
        original = self.ns["_typing_tasks"][(1, 42)]
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(5):
            await self.ns["on_typing"](later, user(), None)
        self.assertIs(self.ns["_typing_tasks"][(1, 42)], original)
        self.ns["asyncio"].sleep.assert_awaited_once()
        release.set()
        await original
        first.send.assert_awaited_once()
        later.send.assert_not_awaited()

    async def test_cooldown_bots_and_dms_schedule_nothing(self):
        self.ns["_last_typing_notice"]["guild:1:user:42"] = self.ns["time"].time()
        await self.ns["on_typing"](TextChannel(99, 1), user(), None)
        await self.ns["on_typing"](TextChannel(), user(44, bot=True), None)
        await self.ns["on_typing"](DMChannel(), user(45), None)
        self.assertEqual(self.ns["_typing_tasks"], {})

    async def test_sent_message_cancels_timer_and_valid_fallback_sends_once(self):
        entered = asyncio.Event()
        async def delay(_seconds):
            entered.set()
            await asyncio.Event().wait()
        self.ns["asyncio"].sleep.side_effect = delay
        channel = TextChannel()
        await self.ns["on_typing"](channel, user(), None)
        original = self.ns["_typing_tasks"][(1, 42)]
        await asyncio.wait_for(entered.wait(), timeout=2)
        sent = message(channel)
        self.ns["_observe_typing_message"](sent)
        self.ns["_typing_message_fallback"](sent)
        replacement = self.ns["_typing_tasks"][(1, 42)]
        self.assertIsNot(original, replacement)
        await asyncio.gather(original, return_exceptions=True)
        await replacement
        self.ns["_typing_message_fallback"](sent)
        channel.send.assert_awaited_once()
        self.assertEqual(self.ns["_typing_tasks"], {})

    async def test_censored_message_observation_cancels_without_fallback(self):
        entered = asyncio.Event()
        async def delay(_seconds):
            entered.set()
            await asyncio.Event().wait()
        self.ns["asyncio"].sleep.side_effect = delay
        channel = TextChannel()
        await self.ns["on_typing"](channel, user(), None)
        pending = self.ns["_typing_tasks"][(1, 42)]
        await asyncio.wait_for(entered.wait(), timeout=2)
        self.ns["_observe_typing_message"](message(channel, content="censored"))
        with self.assertRaises(asyncio.CancelledError):
            await pending
        channel.send.assert_not_awaited()
        self.assertEqual(self.ns["_last_typing_notice"], {})

    async def test_fallback_skips_commands_bots_dms_empty_content_and_supports_threads(self):
        for item in (message(TextChannel(), content="!unknown"), message(TextChannel(), content=" /관리"),
                     message(TextChannel(), user(bot=True)), message(DMChannel()), message(TextChannel(), content="  ")):
            self.ns["_typing_message_fallback"](item)
        self.assertEqual(self.ns["_typing_tasks"], {})
        thread = Thread()
        self.ns["_typing_message_fallback"](message(thread))
        await self.drain()
        thread.send.assert_awaited_once()
        self.ns["asyncio"].sleep.assert_not_awaited()

    async def test_cancellation_during_send_keeps_reservation_until_success_saved(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first, other = TextChannel(1), TextChannel(2)
        async def accepted_send(**_kwargs):
            entered.set()
            await release.wait()
        first.send.side_effect = accepted_send
        key = (1, 42)
        pending = asyncio.create_task(self.ns["_send_typing_reminder"](first, user(), key, self.ns["time"].time()))
        self.ns["_typing_tasks"][key] = pending
        await asyncio.wait_for(entered.wait(), timeout=2)
        pending.cancel()
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.assertIn("guild:1:user:42", self.ns["_typing_notice_inflight"])
        await self.timer(other, user())
        other.send.assert_not_awaited()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertIn("guild:1:user:42", json.loads(self.path.read_text()))
        self.assertEqual(self.ns["_typing_notice_inflight"], set())

    async def test_cancellation_with_failed_send_preserves_cancellation_and_retry(self):
        entered, release = asyncio.Event(), asyncio.Event()
        channel = TextChannel()
        async def failed_send(**_kwargs):
            entered.set()
            await release.wait()
            raise PermissionError("private transport detail")
        channel.send.side_effect = failed_send
        pending = asyncio.create_task(self.ns["_send_typing_reminder"](channel, user(), (1, 42), self.ns["time"].time()))
        self.ns["_typing_tasks"][(1, 42)] = pending
        await asyncio.wait_for(entered.wait(), timeout=2)
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(self.ns["_last_typing_notice"], {})
        self.assertEqual(self.ns["_typing_notice_inflight"], set())
        self.assertFalse(self.path.exists())
        self.assertNotIn("private transport detail", "\n".join(logs.output))

    async def test_save_round_trips_after_restart_and_suppresses_timer(self):
        self.ns["_last_typing_notice"] = {"guild:1:user:42": 200_000, "guild:2:user:43": 234.5}
        self.ns["_save_typing_cooldowns"]()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        restarted = typing_functions(self.path)
        restarted["_last_typing_notice"] = restarted["_load_typing_cooldowns"]()
        self.assertEqual(restarted["_last_typing_notice"], self.ns["_last_typing_notice"])
        await restarted["on_typing"](TextChannel(), user(), None)
        self.assertEqual(restarted["_typing_tasks"], {})

    async def test_legacy_global_guard_expires_and_channel_keys_become_guild_scoped(self):
        self.write_state({"1:10:42": 100, "1:20:42": 300, "user:42": 200,
                          "00042": 400, "3:30:43": 500, "guild:3:user:00043": 450})
        loaded = self.ns["_load_typing_cooldowns"]()
        self.assertEqual(loaded, {"guild:1:user:42": 300, "user:42": 400, "guild:3:user:43": 500})
        self.ns["_last_typing_notice"] = loaded
        self.ns["time"].time.return_value = 400 + self.ns["TYPE_REMINDER_COOLDOWN"] - 1
        first, second = TextChannel(1, 9), TextChannel(2, 10)
        await self.timer(first, user())
        first.send.assert_not_awaited()
        self.ns["time"].time.return_value += 1
        await self.timer(first, user())
        await self.timer(second, user())
        self.assertEqual([first.send.await_count, second.send.await_count], [1, 1])
        self.assertNotIn("user:42", self.ns["_last_typing_notice"])

    async def test_invalid_entries_do_not_drop_other_users_and_nonfinite_values_rejected(self):
        self.write_state({
            "user:42": 123, "guild:2:user:43": "456.5", "1:2:44": 789,
            "user:45": None, "user:46": True, "user:47": False,
            "user:48": math.nan, "user:49": math.inf, "user:50": -math.inf,
            "user:51": "NaN", "user:52": {}, "user:53": -1, "user:54": 0,
            "user:0": 123, "user:-1": 123, "user:１２": 123,
            "guild:0:user:56": 123, "user:not-an-id": 123, "bad:channel:42": 999,
            f"user:{2**64}": 123, "user:" + "9" * 5000: 123,
        })
        self.assertEqual(self.ns["_load_typing_cooldowns"](), {"user:42": 123, "guild:2:user:43": 456.5, "guild:1:user:44": 789})

    async def test_missing_nonobject_and_malformed_files_are_safe(self):
        self.assertEqual(self.ns["_load_typing_cooldowns"](), {})
        for invalid in (None, [], ["user:42"], "secret raw value", 42):
            self.write_state(invalid)
            with self.assertLogs(level="WARNING") as logs:
                self.assertEqual(self.ns["_load_typing_cooldowns"](), {})
            self.assertNotIn("secret raw value", "\n".join(logs.output))
        self.path.write_text("{ broken", encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.ns["_load_typing_cooldowns"](), {})

    async def test_invalid_save_preserves_previous_file_and_removes_temporary_file(self):
        for invalid in (math.nan, object()):
            self.write_state({"user:42": 123})
            before = self.path.read_bytes()
            self.ns["_last_typing_notice"] = {"user:42": invalid}
            with self.assertLogs(level="WARNING"):
                self.ns["_save_typing_cooldowns"]()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.path.with_suffix(".tmp").exists())

    async def test_replace_failure_preserves_previous_file_and_removes_temporary_file(self):
        self.write_state({"user:42": 123})
        before = self.path.read_bytes()
        self.ns["_last_typing_notice"] = {"user:42": 456}
        with patch.object(os, "replace", side_effect=OSError("private filesystem detail")):
            with self.assertLogs(level="WARNING") as logs:
                self.ns["_save_typing_cooldowns"]()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        self.assertNotIn("private filesystem detail", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
