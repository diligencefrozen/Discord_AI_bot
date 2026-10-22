"""Exercise real main adapters without a token, gateway or production state."""
import ast
import logging
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import bot_ui


def adapters():
    source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    names = {"_admin_action_service", "_admin_test_service", "_plain_admin_message_hint"}
    nodes = [node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    namespace = {
        "discord": discord, "logging": logging, "os": os, "time": time,
        "log_admin_access": bot_ui.log_admin_access,
        "_interaction_send": AsyncMock(), "send_active_rules_response": AsyncMock(),
        "active_bad_roots": lambda: ("rule",), "find_badroot": lambda value: None,
        "handle_admin_action": AsyncMock(return_value=True),
        "XP_DATA_FILE": Path(__file__),
        "bot": SimpleNamespace(tree=SimpleNamespace(get_commands=lambda: [1] * 5)),
        "_slash_registration_state": SimpleNamespace(synced=False),
        "_admin_text_hint_cooldowns": {},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


def target(user_id=491239880005910538):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), guild=SimpleNamespace(id=8, owner_id=77))


class AdminAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {
            "SPECIAL_ADMIN_USER_ID": "855749166764654653",
            "SPECIAL_ADMIN_USER_IDS": "855749166764654653,491239880005910538",
            "SPECIAL_ADMIN_GUILD_IDS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_actual_service_adapters_reject_unknown_dm_and_scoped_admin(self):
        for interaction in (target(12), SimpleNamespace(user=target().user, guild=None)):
            ns = adapters()
            await ns["_admin_action_service"](interaction, "admin:members")
            await ns["_admin_test_service"](interaction, "sensitive input")
            ns["handle_admin_action"].assert_not_awaited()
            self.assertEqual(ns["_interaction_send"].await_count, 2)
        ns = adapters()
        with patch.dict(os.environ, {"SPECIAL_ADMIN_GUILD_IDS": "9"}):
            await ns["_admin_action_service"](target(), "admin:members")
        ns["handle_admin_action"].assert_not_awaited()

    async def test_both_ids_and_owner_route_to_real_tools(self):
        for user_id in (855749166764654653, 491239880005910538, 77):
            ns = adapters()
            await ns["_admin_action_service"](target(user_id), "admin:members")
            ns["handle_admin_action"].assert_awaited_once()
            text = ns["handle_admin_action"].call_args.kwargs["status_text"]
            self.assertIn("원격 게시 성공: 미확인", text)
            self.assertIn("재시작 시 초기화", text)

    async def test_rules_and_test_routes_remain_available(self):
        ns = adapters()
        await ns["_admin_action_service"](target(), "admin:rules")
        ns["send_active_rules_response"].assert_awaited_once()
        ns["handle_admin_action"].assert_not_awaited()
        await ns["_admin_test_service"](target(), "hello")
        self.assertIn("탐지되지", ns["_interaction_send"].call_args.args[1])


class PlainAdminHintTests(unittest.IsolatedAsyncioTestCase):
    def message(self, content="/관리", user_id=42, guild_id=8):
        return SimpleNamespace(content=content, author=SimpleNamespace(id=user_id, bot=False),
                               guild=SimpleNamespace(id=guild_id), channel=SimpleNamespace(send=AsyncMock()))

    async def test_literal_text_only_hints_once_and_never_opens_tools(self):
        ns, message = adapters(), self.message()
        self.assertTrue(await ns["_plain_admin_message_hint"](message))
        self.assertTrue(await ns["_plain_admin_message_hint"](message))
        message.channel.send.assert_awaited_once()
        ns["handle_admin_action"].assert_not_awaited()
        self.assertEqual(message.channel.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertFalse(await ns["_plain_admin_message_hint"](self.message("/관리 이야기")))

    async def test_failure_can_retry_and_members_are_independent(self):
        ns, message = adapters(), self.message()
        message.channel.send.side_effect = [PermissionError("private data"), None]
        with self.assertLogs(level="WARNING") as captured:
            await ns["_plain_admin_message_hint"](message)
        self.assertNotIn("private data", "\n".join(captured.output))
        await ns["_plain_admin_message_hint"](message)
        self.assertEqual(message.channel.send.await_count, 2)
        other = self.message(user_id=43)
        await ns["_plain_admin_message_hint"](other)
        other.channel.send.assert_awaited_once()

    async def test_bot_and_dm_have_no_hint(self):
        for mode in ("bot", "dm"):
            ns, message = adapters(), self.message()
            if mode == "bot":
                message.author.bot = True
            else:
                message.guild = None
            self.assertFalse(await ns["_plain_admin_message_hint"](message))
            message.channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
