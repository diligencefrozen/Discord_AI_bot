"""Exercise the real prefix entry when the Discord slash picker is unavailable."""
import ast
import logging
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord
from slash_access import slash_access_report_with_refresh
from test_main_services import services, message


def adapter():
    source = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    function = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "slash_access_diagnostic")
    function.decorator_list = []
    namespace = {"discord": discord, "logging": logging, "time": time,
                 "slash_access_report_with_refresh": slash_access_report_with_refresh, "_slash_diagnostic_cooldowns": {}}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


def context(user_id=491239880005910538):
    guild = SimpleNamespace(id=802904099816472616, owner_id=10, me=object())
    actor = SimpleNamespace(id=user_id, bot=False, guild=guild)
    channel = SimpleNamespace(id=802904099816472619, guild=guild,
                              permissions_for=Mock(return_value=discord.Permissions(view_channel=True)))
    return SimpleNamespace(guild=guild, author=actor, channel=channel, reply=AsyncMock())


class SlashAccessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_self_check_needs_no_slash_or_admin_permission(self):
        ns, ctx = adapter(), context(42)
        await ns["slash_access_diagnostic"](ctx)
        args = ctx.reply.call_args
        self.assertIn("애플리케이션 명령어 사용: 거부", args.args[0])
        self.assertIn("/관리 인증: 비허용", args.args[0])
        self.assertFalse(args.kwargs["mention_author"])
        self.assertEqual(args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertEqual(args.kwargs["delete_after"], 90)

    async def test_duplicate_requests_are_bounded_but_other_users_are_independent(self):
        ns, first, second = adapter(), context(), context(42)
        await ns["slash_access_diagnostic"](first)
        await ns["slash_access_diagnostic"](first)
        await ns["slash_access_diagnostic"](second)
        first.reply.assert_awaited_once()
        second.reply.assert_awaited_once()
        key = (first.guild.id, first.author.id)
        ns["_slash_diagnostic_cooldowns"][key] = time.monotonic() - 61
        await ns["slash_access_diagnostic"](first)
        self.assertEqual(first.reply.await_count, 2)

    async def test_send_failure_rolls_back_and_logs_no_content_or_exception_text(self):
        ns, ctx = adapter(), context()
        ctx.reply.side_effect = [RuntimeError("SECRET_TRANSPORT"), None]
        with self.assertLogs(level="WARNING") as logs:
            await ns["slash_access_diagnostic"](ctx)
        self.assertNotIn("SECRET_TRANSPORT", "\n".join(logs.output))
        self.assertNotIn(str(ctx.author.id), "\n".join(logs.output))
        await ns["slash_access_diagnostic"](ctx)
        self.assertEqual(ctx.reply.await_count, 2)

    async def test_state_is_bounded_and_bots_are_ignored(self):
        ns, ctx = adapter(), context()
        ns["_slash_diagnostic_cooldowns"].update({(1, number): 0 for number in range(2048)})
        await ns["slash_access_diagnostic"](ctx)
        self.assertEqual(len(ns["_slash_diagnostic_cooldowns"]), 2048)
        bot_ctx = context(50)
        bot_ctx.author.bot = True
        await ns["slash_access_diagnostic"](bot_ctx)
        bot_ctx.reply.assert_not_awaited()

    async def test_actual_message_dispatches_diagnostic_without_xp_or_ai(self):
        ns = services()
        ns["bot"].all_commands["명령진단"] = object()
        ns["bot"].all_commands["slashcheck"] = object()
        for content in ("!명령진단", "!slashcheck"):
            target = message(content)
            await ns["on_message"](target)
        self.assertEqual(ns["bot"].process_commands.await_count, 2)
        self.assertEqual(ns["user_xp_data"], {})
        ns["_typing_message_fallback"].assert_not_called()
        ns["_plain_admin_message_hint"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
