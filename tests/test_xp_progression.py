"""Run the production XP functions without starting a gateway or loading secrets."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
import copy
import datetime
import logging
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from bot_storage import PickleStore
from moderation_rules import lookup_exemption, parse_command

try:
    import discord
except ModuleNotFoundError:
    discord = None


SOURCE = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
FUNCTIONS = {
    "load_xp_data", "save_xp_data", "load_achievements_data", "save_achievements_data",
    "init_user_achievements", "check_achievements", "apply_achievement_reward",
    "get_user_achievements", "get_achievement_progress", "is_weekend", "get_today_date",
    "_new_daily_xp", "_migrate_xp_record", "calculate_xp_progression", "reset_daily_xp",
    "add_xp", "get_user_xp", "get_available_rewards", "claim_reward", "_announce_vip_winner",
    "_legacy_exemption_state", "_lookup_legacy_exemption", "_consume_legacy_exemption",
    "on_message", "_growth_embed",
}


def services(day="2026-10-22", hour=14):
    clock = {"date": day, "hour": hour, "timestamp": 1000.0}

    class FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.datetime.fromisoformat(clock["date"])
            return value.replace(hour=clock["hour"], tzinfo=tz)

    namespace = {
        "datetime": SimpleNamespace(datetime=FrozenDatetime),
        "time": SimpleNamespace(time=lambda: clock["timestamp"]),
        "seoul_tz": datetime.timezone(datetime.timedelta(hours=9)),
        "deque": deque, "logging": logging, "discord": discord,
        "user_xp_data": {}, "achievements_data": {},
        "DELETION_AUDIT": SimpleNamespace(remember=Mock()),
        "XP_STORE": Mock(), "ACHIEVEMENTS_STORE": Mock(),
        "lookup_rule_exemption": lookup_exemption,
    }
    bodies = []
    for original in SOURCE.body:
        node = copy.deepcopy(original)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"XP_CONFIG", "ACHIEVEMENTS"}:
                    namespace[target.id] = ast.literal_eval(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
            node.decorator_list = []
            bodies.append(node)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *bodies], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    return namespace, clock


def award(ns, clock, count, user=42, start=1):
    results = []
    for event_id in range(start, start + count):
        results.append(ns["add_xp"](user, event_id=event_id))
        clock["timestamp"] += 5
    return results


class XPProgressionTests(unittest.TestCase):
    def test_first_three_messages_cannot_chain_into_legendary(self):
        for day, expected in (("2026-10-22", 25), ("2026-10-23", 45)):
            ns, clock = services(day)
            for event in range(1, 4):
                award(ns, clock, 1, start=event)
                data = ns["get_user_xp"](42)
                self.assertEqual(data["activity_xp"], expected * event)
                self.assertGreater(data["bonus_xp"], 0)
                self.assertNotIn("vip_winner_date", data)
                self.assertNotIn(4, ns["achievements_data"][42]["stats"]["tiers_reached"])
                self.assertEqual(ns["achievements_data"][42]["stats"]["total_messages"], event)

    def test_legendary_requires_18_weekday_or_10_weekend_messages(self):
        for day, required in (("2026-10-22", 18), ("2026-10-23", 10), ("2026-10-25", 10)):
            ns, clock = services(day)
            award(ns, clock, required - 1)
            self.assertLess(ns["get_user_xp"](42)["activity_xp"], 450)
            self.assertNotIn("vip_winner_date", ns["get_user_xp"](42))
            result = award(ns, clock, 1, start=required)[0]
            data = ns["get_user_xp"](42)
            self.assertEqual(data["activity_xp"], 450)
            self.assertEqual(result[2], 4)
            self.assertEqual(data["vip_winner_date"], day)
            self.assertFalse(data["vip_winner_announced"])
            self.assertEqual(data["claimed"], list(range(5)))
            self.assertEqual(ns["achievements_data"][42]["stats"]["total_messages"], required)
            self.assertEqual("legendary_weekend" in ns["achievements_data"][42]["unlocked"], required == 10)

    def test_explicit_activity_adjustment_processes_every_crossed_tier(self):
        ns, _ = services(hour=10)
        total, leveled, highest, unlocked = ns["add_xp"](42, 900, record_message=False)
        data, stats = ns["get_user_xp"](42), ns["achievements_data"][42]["stats"]
        self.assertEqual((data["activity_xp"], leveled, highest), (900, True, 4))
        self.assertEqual(stats["tiers_reached"], set(range(5)))
        self.assertEqual(stats["rewards_claimed_count"], 5)
        self.assertEqual(stats["total_messages"], 0)
        self.assertEqual(data["claimed"], list(range(5)))
        self.assertTrue({"reach_sapling", "reach_growth", "reach_tree", "reach_legendary", "collector_5"} <= set(unlocked))
        self.assertGreater(total, data["activity_xp"])
        self.assertEqual(len(unlocked), len(set(unlocked)))
        before = copy.deepcopy(data)
        self.assertEqual(ns["add_xp"](42, 0, record_message=False)[1:], (False, None, []))
        self.assertEqual(data, before)

    def test_bonus_only_reward_cannot_promote_or_count_a_message(self):
        ns, _ = services()
        ns["add_xp"](42, 0, record_message=False)
        ns["check_achievements"](42, "reward_claimed")
        data = ns["get_user_xp"](42)
        self.assertEqual(data["activity_xp"], 0)
        self.assertEqual(data["bonus_xp"], 100)
        self.assertEqual(ns["get_available_rewards"](42), [])
        self.assertEqual(ns["achievements_data"][42]["stats"]["total_messages"], 0)

    def test_duplicate_events_and_cooldown_are_not_replayable(self):
        ns, clock = services()
        ns["add_xp"](42, event_id=100)
        clock["timestamp"] += 1
        self.assertFalse(ns["add_xp"](42, event_id=101)[1])
        clock["timestamp"] += 20
        for event_id in (99, 100, 101):
            self.assertEqual(ns["add_xp"](42, event_id=event_id)[1:], (False, None, []))
        self.assertEqual(ns["get_user_xp"](42)["activity_xp"], 25)
        self.assertEqual(ns["achievements_data"][42]["stats"]["total_messages"], 1)
        ns["add_xp"](42, event_id=102)
        self.assertEqual(ns["get_user_xp"](42)["activity_xp"], 50)

    def test_manual_claim_is_validated_and_idempotent(self):
        ns, _ = services()
        ns["user_xp_data"][42] = ns["_new_daily_xp"]("2026-10-22")
        data = ns["user_xp_data"][42]
        data.update(activity_xp=180, bonus_xp=10000, xp=10180)
        for invalid in (-1, 5, True, "2"):
            self.assertFalse(ns["claim_reward"](42, invalid))
        self.assertFalse(ns["claim_reward"](42, 4))
        self.assertTrue(ns["claim_reward"](42, 2))
        count = data["rewards_active"]["2"]["count"]
        self.assertFalse(ns["claim_reward"](42, 2))
        self.assertEqual(data["rewards_active"]["2"]["count"], count)
        self.assertEqual(ns["achievements_data"][42]["stats"]["rewards_claimed_count"], 1)

    def test_midnight_resets_once_and_rejects_expired_manual_claims(self):
        ns, clock = services()
        award(ns, clock, 18)
        clock.update(date="2026-10-23", timestamp=clock["timestamp"] + 1)
        self.assertFalse(ns["claim_reward"](42, 4))
        self.assertEqual(ns["get_user_xp"](42)["rewards_active"], {})
        ns["add_xp"](42, event_id=19)
        data, stats = ns["get_user_xp"](42), ns["achievements_data"][42]["stats"]
        self.assertEqual(data["activity_xp"], 45)
        self.assertEqual(stats["total_messages"], 1)
        self.assertNotIn("vip_winner_date", data)
        before = copy.deepcopy(ns["achievements_data"])
        ns["reset_daily_xp"]()
        ns["reset_daily_xp"]()
        self.assertEqual(ns["achievements_data"], before)

    def test_expired_timed_reward_is_not_an_exemption(self):
        ns, clock = services()
        ns["add_xp"](42, 450, record_message=False)
        self.assertIsNotNone(ns["_lookup_legacy_exemption"](42, "profanity"))
        ns["user_xp_data"][42]["rewards_active"] = {"4": {"expires_at": clock["timestamp"] - 1}}
        self.assertIsNone(ns["_lookup_legacy_exemption"](42, "profanity"))

    def test_hour_based_bonuses_and_user_states_remain_separate(self):
        ns, clock = services(hour=1)
        award(ns, clock, 2, user=42)
        award(ns, clock, 1, user=43)
        self.assertEqual(ns["get_user_xp"](42)["activity_xp"], 50)
        self.assertEqual(ns["get_user_xp"](43)["activity_xp"], 25)
        for uid in (42, 43):
            self.assertTrue({"early_bird", "night_owl"} <= set(ns["achievements_data"][uid]["unlocked"]))
            self.assertNotIn("vip_winner_date", ns["get_user_xp"](uid))
        ns["get_user_xp"](42)["rewards_active"].clear()
        self.assertTrue(ns["get_user_xp"](43)["rewards_active"])

    def test_invalid_adjustments_do_not_mutate_state(self):
        ns, _ = services()
        for amount in (-1, 1.5, True, "450"):
            with self.assertRaises(ValueError):
                ns["add_xp"](42, amount, record_message=False)
        self.assertEqual(ns["user_xp_data"], {})

    def test_one_activity_transaction_flushes_each_ledger_once_in_order(self):
        for amount in (None, 450):
            ns, _ = services()
            calls = []
            ns["XP_STORE"].save.side_effect = lambda _value: calls.append("xp")
            ns["ACHIEVEMENTS_STORE"].save.side_effect = lambda _value: calls.append("achievements")
            ns["add_xp"](42, amount, record_message=amount is None)
            self.assertEqual(calls, ["xp", "achievements"])

    def test_legacy_files_are_backed_up_and_migrated_without_deletion(self):
        ns, clock = services()
        original = {42: {"xp": 1200, "date": "2026-10-22", "last_msg": 990,
                         "claimed": [0, 1, 2, 3, 4], "rewards_active": {"2": {"count": 8}},
                         "vip_winner_date": "2026-10-22", "vip_winner_announced": True}}
        legacy_achievements = {42: {"unlocked": ["first_message", "reach_legendary"],
                                    "stats": {"total_messages": 2, "last_message_date": "2026-10-22"}}}
        with tempfile.TemporaryDirectory() as directory:
            ns["XP_STORE"] = PickleStore(Path(directory) / "daily_xp_data.pkl")
            ns["ACHIEVEMENTS_STORE"] = PickleStore(Path(directory) / "achievements_data.pkl")
            ns["XP_STORE"].save(original)
            ns["ACHIEVEMENTS_STORE"].save(legacy_achievements)
            ns["load_xp_data"]()
            ns["load_achievements_data"]()
            ns["init_user_achievements"](42)
            migrated = ns["get_user_xp"](42)
            self.assertEqual((migrated["xp"], migrated["legacy_xp"], migrated["activity_xp"]), (1200, 1200, 0))
            self.assertEqual(migrated["claimed"], original[42]["claimed"])
            self.assertEqual(migrated["rewards_active"], original[42]["rewards_active"])
            self.assertEqual(ns["achievements_data"][42]["unlocked"], ["first_message", "reach_legendary"])
            backup = PickleStore(str(ns["XP_STORE"].path) + ".bak.1").load()
            self.assertEqual(backup, original)
            ns["load_xp_data"]()
            self.assertEqual(ns["get_user_xp"](42), migrated)

    def test_older_achievement_backup_does_not_duplicate_payout(self):
        ns, clock = services()
        award(ns, clock, 1)
        data = ns["get_user_xp"](42)
        before = copy.deepcopy(data)
        ns["achievements_data"][42]["unlocked"].clear()
        ns["check_achievements"](42)
        self.assertEqual(data, before)

    def test_malformed_legacy_row_cannot_discard_other_users(self):
        ns, _ = services()
        malformed = {"xp": "not-an-integer", "date": "2026-10-22", "operator_note": "preserve"}
        loaded = {42: {"xp": 1200, "date": "2026-10-22"}, 43: malformed,
                  44: {"xp": 25, "date": "2026-10-22"}}
        ns["XP_STORE"].load.return_value = copy.deepcopy(loaded)
        with self.assertLogs(level="WARNING") as logs:
            ns["load_xp_data"]()
        self.assertEqual(set(ns["user_xp_data"]), {42, 43, 44})
        self.assertEqual(ns["user_xp_data"][42]["legacy_xp"], 1200)
        self.assertEqual(ns["user_xp_data"][44]["legacy_xp"], 25)
        self.assertEqual(ns["user_xp_data"][43], malformed)
        self.assertIn("record_rejected", " ".join(logs.output))
        self.assertNotIn("not-an-integer", " ".join(logs.output))


@unittest.skipIf(discord is None, "requires discord.py")
class XPDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_vip_reservation_prevents_concurrent_duplicate_sends(self):
        ns, _ = services()
        ns["add_xp"](42, 450, record_message=False)
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(author=SimpleNamespace(id=42, mention="<@42>", display_avatar=SimpleNamespace(url="https://example.com/avatar.png")), channel=channel)
        async def send(**_kwargs):
            self.assertTrue(ns["get_user_xp"](42)["vip_winner_announced"])
            await asyncio.sleep(0)
        channel.send.side_effect = send
        results = await asyncio.gather(*(ns["_announce_vip_winner"](message) for _ in range(3)))
        self.assertEqual(results.count(True), 1)
        channel.send.assert_awaited_once()
        self.assertEqual(ns["get_user_xp"](42)["vip_announcement_delivery"], "sent")
        self.assertEqual(channel.send.call_args.kwargs["allowed_mentions"].to_dict()["users"], [42])

    async def test_vip_is_per_user_and_failed_delivery_is_not_blindly_retried(self):
        ns, _ = services()
        for uid in (42, 43):
            ns["add_xp"](uid, 450, record_message=False)
        channel = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("private transport details")))
        message = SimpleNamespace(author=SimpleNamespace(id=42, mention="<@42>", display_avatar=SimpleNamespace(url="https://example.com/avatar.png")), channel=channel)
        self.assertFalse(await ns["_announce_vip_winner"](message))
        self.assertFalse(await ns["_announce_vip_winner"](message))
        channel.send.assert_awaited_once()
        self.assertEqual(ns["get_user_xp"](42)["vip_announcement_delivery"], "failed_or_unknown")
        channel.send.side_effect = None
        message.author.id, message.author.mention = 43, "<@43>"
        self.assertTrue(await ns["_announce_vip_winner"](message))
        self.assertEqual(channel.send.await_count, 2)

    async def test_vip_and_message_replay_survive_restart_and_reset_next_day(self):
        ns, clock = services()
        with tempfile.TemporaryDirectory() as directory:
            xp_path, ach_path = Path(directory) / "daily_xp_data.pkl", Path(directory) / "achievements_data.pkl"
            ns["XP_STORE"], ns["ACHIEVEMENTS_STORE"] = PickleStore(xp_path), PickleStore(ach_path)
            ns["add_xp"](42, 450, event_id=100)
            message = SimpleNamespace(author=SimpleNamespace(id=42, mention="<@42>", display_avatar=SimpleNamespace(url="https://example.com/avatar.png")), channel=SimpleNamespace(send=AsyncMock()))
            self.assertTrue(await ns["_announce_vip_winner"](message))
            restored, restored_clock = services()
            restored["XP_STORE"], restored["ACHIEVEMENTS_STORE"] = PickleStore(xp_path), PickleStore(ach_path)
            restored["load_xp_data"]()
            restored["load_achievements_data"]()
            restored_clock["timestamp"] += 30
            before = copy.deepcopy(restored["get_user_xp"](42))
            restored["add_xp"](42, event_id=100)
            self.assertEqual(restored["get_user_xp"](42), before)
            self.assertFalse(await restored["_announce_vip_winner"](message))
            message.channel.send.assert_awaited_once()
            restored_clock["date"] = "2026-10-23"
            restored["add_xp"](42, 450, event_id=101)
            self.assertTrue(await restored["_announce_vip_winner"](message))
            self.assertEqual(message.channel.send.await_count, 2)

    async def test_gateway_filters_commands_bots_dms_blank_and_rejected_messages(self):
        for text, bot_author, guild, approved in (
            ("!xp", False, True, True), ("!unknown", False, True, True),
            ("/unknown", False, True, True), ("hello", True, True, True),
            ("hello", False, False, True), ("   ", False, True, True),
            ("rejected", False, True, False),
        ):
            with self.subTest(text=text, bot=bot_author, guild=guild):
                ns, _ = services()
                ns.update({
                    "bot": SimpleNamespace(user=None, all_commands={"xp": object()}, process_commands=AsyncMock()),
                    "_observe_typing_message": Mock(), "_typing_message_fallback": Mock(),
                    "_plain_admin_message_hint": AsyncMock(return_value=False),
                    "_moderate_message_content": AsyncMock(return_value=approved),
                    "parse_command": parse_command, "BLOCK_MEDIA_USER_IDS": set(),
                    "push_recent_message": Mock(), "SPAM_ENABLED": False,
                    "GAME_CARD_CHANNELS": set(), "LAUGH_KEYWORDS": (), "EMOJI_IMAGES": {},
                    "GAME_WARN_RE": re.compile(r"(?!)"), "HOTKEYWORD_EXCLUDE_CHANNELS": {1},
                    "add_xp": Mock(side_effect=AssertionError("ineligible message awarded XP")),
                })
                message = SimpleNamespace(id=100, content=text, clean_content=text,
                    author=SimpleNamespace(id=42, bot=bot_author),
                    guild=SimpleNamespace(id=1) if guild else None,
                    channel=SimpleNamespace(id=1, send=AsyncMock()), attachments=[], mentions=[], reference=None)
                await ns["on_message"](message)
                ns["add_xp"].assert_not_called()

    async def test_gateway_awards_only_after_spam_and_media_gates(self):
        for rejected_by in ("spam", "media", None):
            with self.subTest(rejected_by=rejected_by):
                ns, _ = services()
                ns.update({
                    "bot": SimpleNamespace(user=None, all_commands={}, process_commands=AsyncMock()),
                    "_observe_typing_message": Mock(), "_typing_message_fallback": Mock(),
                    "_plain_admin_message_hint": AsyncMock(return_value=False),
                    "_moderate_message_content": AsyncMock(return_value=True),
                    "parse_command": parse_command, "BLOCK_MEDIA_USER_IDS": {42} if rejected_by == "media" else set(),
                    "EXEMPT_MEDIA_CHANNEL_IDS": set(), "_message_has_blocked_media_or_emoji": Mock(return_value=True),
                    "safe_delete": AsyncMock(return_value=True), "make_surveillance_embed": Mock(return_value=(None, None)),
                    "PRIMARY_EXEMPT_MEDIA_CH_ID": 9, "_dbg": Mock(),
                    "push_recent_message": Mock(), "SPAM_ENABLED": rejected_by == "spam",
                    "_is_exempt": Mock(return_value=False), "is_user_exempt_from_spam": Mock(return_value=False),
                    "check_spam_and_reason": Mock(return_value="rate"), "_user_deletion_rate": {},
                    "_user_violations": {42: 0}, "SPAM_CFG": {"auto_timeout_threshold": 5},
                    "random": SimpleNamespace(random=lambda: 1),
                    "GAME_CARD_CHANNELS": set(), "LAUGH_KEYWORDS": (), "EMOJI_IMAGES": {},
                    "GAME_WARN_RE": re.compile(r"(?!)"), "HOTKEYWORD_EXCLUDE_CHANNELS": {1},
                    "add_xp": Mock(return_value=(25, False, None, [])), "_announce_vip_winner": AsyncMock(),
                })
                message = SimpleNamespace(id=100, content="hello", clean_content="hello",
                    author=SimpleNamespace(id=42, bot=False), guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(id=1, send=AsyncMock()), attachments=[], mentions=[], reference=None)
                await ns["on_message"](message)
                if rejected_by:
                    ns["add_xp"].assert_not_called()
                    ns["_typing_message_fallback"].assert_not_called()
                else:
                    ns["add_xp"].assert_called_once_with(42, event_id=100)


if __name__ == "__main__":
    unittest.main()
