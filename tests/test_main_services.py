"""Execute the real main.py services with isolated state and fake transports.

AST loading avoids gateway startup, dotenv secrets and production state files.
The tested function bodies are compiled unchanged (only command decorators
are removed), so route/ordering and reward regressions exercise real code.
"""
from __future__ import annotations

import ast
import asyncio
import datetime
import io
import logging
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import moderation_rules

try:
    import discord
    import ai_providers
    import bot_ui
except ModuleNotFoundError:
    discord = ai_providers = None


SOURCE = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
SERVICE_NAMES = {
    "get_user_xp", "claim_reward", "_legacy_exemption_state", "_lookup_legacy_exemption",
    "_consume_legacy_exemption", "is_user_exempt_from_profanity", "use_profanity_pass",
    "_moderate_message_content", "_moderate_ai_prompt_interaction", "on_message",
    "_interaction_send", "_interaction_defer", "_question_interaction_service",
    "_publish_question_answer", "_generate_text_response", "_generate_image_bytes",
    "_reserve_image_request", "ask", "strip_think", "_send_typing_reminder",
    "_image_interaction_service", "on_message_delete",
}


def services():
    namespace = {
        "asyncio": asyncio, "discord": discord, "datetime": datetime,
        "logging": logging, "time": time, "re": re, "io": io,
        "find_badroot": moderation_rules.find_badroot,
        "lookup_rule_exemption": moderation_rules.lookup_exemption,
        "parse_command": moderation_rules.parse_command,
        "ALLOWED_CHANNELS": {99}, "EXEMPT_PROFANITY_CHANNEL_IDS": {98},
        "LINK_REGEX": re.compile(r"https?://\S+", re.I),
        "user_xp_data": {}, "get_today_date": lambda: "2026-10-21",
        "save_xp_data": Mock(), "check_achievements": Mock(return_value=[]),
        "safe_delete": AsyncMock(return_value=True), "_metric": Mock(),
        "AIProviderError": ai_providers.AIProviderError if ai_providers else RuntimeError,
        "GEMINI_CONFIG": object(), "GEMINI_LIMITER": object(), "HORDE_CONFIG": object(),
        "SYS_PROMPT": "same-language, four sentences", "MAX_TOKENS": 512,
        "TEXT_AI_CONFIG_ERROR": None, "_image_request_lock": asyncio.Lock(),
        "_image_generation_semaphore": asyncio.Semaphore(2),
        "_last_image_request": {}, "IMAGE_REQUEST_COOLDOWN": 20,
        "THINK_RE": re.compile(r"<think>.*?</think>", re.S),
        "bot": SimpleNamespace(user=None, all_commands={"ask": object(), "img": object(), "xp": object()}, process_commands=AsyncMock()),
        "TYPE_REMINDER_COOLDOWN": 43200, "TYPE_REMINDER_CHANNEL_COOLDOWN": 60,
        "_last_typing_notice": {}, "_last_typing_channel_notice": {},
        "_typing_notice_send_lock": asyncio.Lock(), "_typing_tasks": {},
        "_typing_scope_key": lambda channel, user: f"1:{channel.id}:{user.id}",
        "_save_typing_cooldowns": Mock(),
        "AI_IMAGE_FAILURE_MESSAGE": "무료 이미지 서버가 혼잡해요.",
    }
    bodies = []
    for node in SOURCE.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "XP_CONFIG" for t in node.targets):
            namespace["XP_CONFIG"] = ast.literal_eval(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in SERVICE_NAMES:
            node.decorator_list = []
            bodies.append(node)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *bodies], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), namespace)
    return namespace


def interaction(*, channel_id=1):
    response = SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(), send_message=AsyncMock())
    async def defer(**_kwargs):
        response.is_done.return_value = True
    response.defer.side_effect = defer
    return SimpleNamespace(
        user=SimpleNamespace(id=42, mention="<@42>"), response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()),
        guild=SimpleNamespace(id=1),
    )


def message(content, channel_id=1):
    return SimpleNamespace(
        id=123, content=content, author=SimpleNamespace(id=42, mention="<@42>", bot=False),
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()), guild=SimpleNamespace(id=1),
        add_reaction=AsyncMock(),
    )


@unittest.skipIf(discord is None or ai_providers is None, "requires project dependencies")
class MainModerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_and_prompt_spend_once_for_link_profanity_or_both(self):
        for route in ("message", "interaction"):
            for content in ("병12121신", "https://example.com", "병ㄱㄱㄱ신 https://example.com"):
                for count in (0, 1, 2):
                    with self.subTest(route=route, content=content, count=count):
                        ns = services()
                        state = {"xp": 180, "date": "2026-10-21", "claimed": [], "rewards_active": {"2": {"count": count}}}
                        ns["user_xp_data"][42] = state
                        target = message(content) if route == "message" else interaction()
                        allowed = await (ns["_moderate_message_content"](target) if route == "message" else ns["_moderate_ai_prompt_interaction"](target, content))
                        self.assertEqual(allowed, count > 0)
                        self.assertEqual(state["rewards_active"].get("2", {}).get("count", 0), max(0, count - 1))
                        self.assertEqual(ns["safe_delete"].await_count, int(route == "message" and count == 0))

    async def test_channel_exceptions_and_clean_text_do_not_spend(self):
        for text, channel_id in (("hello", 1), ("https://example.com", 99), ("병신", 98)):
            ns = services()
            ns["user_xp_data"][42] = {"date": "2026-10-21", "rewards_active": {"2": {"count": 2}}}
            self.assertTrue(await ns["_moderate_message_content"](message(text, channel_id)))
            self.assertTrue(await ns["_moderate_ai_prompt_interaction"](interaction(channel_id=channel_id), text))
            self.assertEqual(ns["user_xp_data"][42]["rewards_active"]["2"]["count"], 2)

    async def test_claimed_xp_and_legacy_achievement_passes_work_for_links(self):
        ns = services()
        state = {"xp": 180, "date": "2026-10-21", "claimed": [], "rewards_active": {}}
        ns["user_xp_data"][42] = state
        self.assertTrue(ns["claim_reward"](42, 2))
        self.assertFalse(ns["claim_reward"](42, 2))
        self.assertTrue(await ns["_moderate_message_content"](message("https://example.com")))
        self.assertEqual(state["rewards_active"]["2"]["count"], 9)
        state["rewards_active"] = {"achievement_profanity": {"count": 1}}
        self.assertTrue(await ns["_moderate_ai_prompt_interaction"](interaction(), "https://example.com 병신"))
        self.assertEqual(state["rewards_active"], {})

    async def test_timed_rewards_unchanged_and_expired_rewards_deny(self):
        for key in ("trial", "achievement_all", "3"):
            for expired in (False, True):
                with self.subTest(key=key, expired=expired):
                    ns = services()
                    entry = {"expires_at": time.time() + (-60 if expired else 60), "count": 3}
                    ns["user_xp_data"][42] = {"date": "2026-10-21", "rewards_active": {key: entry}}
                    self.assertEqual(await ns["_moderate_ai_prompt_interaction"](interaction(), "병신 https://example.com"), not expired)
                    self.assertEqual(entry["count"], 3)

    async def test_two_concurrent_requests_cannot_spend_one_pass_twice(self):
        ns = services()
        ns["user_xp_data"][42] = {"date": "2026-10-21", "rewards_active": {"2": {"count": 1}}}
        results = await asyncio.gather(*(ns["_moderate_ai_prompt_interaction"](interaction(), "병신 https://example.com") for _ in range(2)))
        self.assertEqual(sorted(results), [False, True])

    async def test_blocked_normal_and_command_messages_return_before_xp_or_dispatch(self):
        for text in ("병aadasda신", "!ask 병  신", "!img 병12121신", "!xp 병-_-신"):
            ns = services()
            ns["add_xp"] = Mock(side_effect=AssertionError("blocked message awarded XP"))
            await ns["on_message"](message(text))
            ns["safe_delete"].assert_awaited_once()
            ns["bot"].process_commands.assert_not_awaited()
            ns["add_xp"].assert_not_called()
        ns = services()
        await ns["on_message"](message("!ask hello"))
        ns["bot"].process_commands.assert_awaited_once()


@unittest.skipIf(discord is None or ai_providers is None, "requires project dependencies")
class PublicQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_posts_answer_publicly_then_private_confirmation(self):
        ns = services()
        ns["_generate_text_response"] = AsyncMock(return_value="첫 문단\n\nSecond paragraph @everyone <@&7> <@9>")
        target = interaction()
        await ns["_question_interaction_service"](target, "private question")
        target.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        ns["_generate_text_response"].assert_awaited_once_with("private question", requester_id=42)
        target.channel.send.assert_awaited_once()
        args, kwargs = target.channel.send.call_args
        self.assertIn("<@42>", args[0])
        self.assertIn("첫 문단", args[0])
        self.assertIn("Second paragraph", args[0])
        self.assertNotIn("private question", args[0])
        self.assertNotIn("ephemeral", kwargs)
        mentions = kwargs["allowed_mentions"].to_dict()
        self.assertEqual(mentions["users"], [42])
        self.assertEqual(mentions["parse"], [])
        self.assertFalse(kwargs["allowed_mentions"].replied_user)
        target.followup.send.assert_awaited_once_with("✅ 답변을 채널에 올렸어요.", ephemeral=True)

    async def test_long_code_answer_is_complete_utf8_attachment(self):
        ns = services()
        answer = "```python\n" + "print('안녕')\n" * 250 + "```"
        ns["_generate_text_response"] = AsyncMock(return_value=answer)
        target = interaction()
        await ns["_question_interaction_service"](target, "hello")
        args, kwargs = target.channel.send.call_args
        self.assertLessEqual(len(args[0]), 2000)
        self.assertEqual(kwargs["file"].fp.getvalue().decode("utf-8"), answer)
        self.assertEqual(kwargs["file"].filename, "answer.txt")

    async def test_no_public_output_on_moderation_or_provider_failure(self):
        for prompt, error in (("병신 https://example.com", None), ("hello", ai_providers.AIProviderError("무료 요청 한도")), ("hello", ai_providers.AIProviderError("키 설정 확인"))):
            with self.subTest(prompt=prompt, error=error):
                ns = services()
                ns["_generate_text_response"] = AsyncMock(side_effect=error)
                target = interaction()
                await ns["_question_interaction_service"](target, prompt)
                target.channel.send.assert_not_awaited()
                self.assertTrue(target.followup.send.call_args.kwargs["ephemeral"])
                if "병신" in prompt:
                    ns["_generate_text_response"].assert_not_awaited()

    async def test_missing_channel_or_send_permission_is_private(self):
        for missing in (True, False):
            ns = services()
            ns["_generate_text_response"] = AsyncMock(return_value="answer")
            target = interaction()
            if missing:
                target.channel = None
            else:
                target.channel.send.side_effect = PermissionError("sensitive exception detail")
            await ns["_question_interaction_service"](target, "hello")
            if missing:
                ns["_generate_text_response"].assert_not_awaited()
            self.assertTrue(target.followup.send.call_args.kwargs["ephemeral"])
            self.assertNotIn("sensitive", target.followup.send.call_args.args[0])

    async def test_failed_private_ack_never_duplicates_public_answer(self):
        ns = services()
        ns["_generate_text_response"] = AsyncMock(return_value="answer")
        target = interaction()
        target.followup.send.side_effect = RuntimeError("ack failed")
        await ns["_question_interaction_service"](target, "hello")
        target.channel.send.assert_awaited_once()
        ns["_generate_text_response"].assert_awaited_once()
        target.followup.send.assert_awaited_once()

    async def test_all_text_uses_gemini_and_images_always_use_horde(self):
        ns = services()
        ns["generate_gemini_text"] = AsyncMock(return_value="answer")
        ns["generate_horde_image"] = AsyncMock(return_value=b"png")
        ns["AI_PROVIDER"] = "huggingface"
        self.assertEqual(await ns["_generate_text_response"]("hello", requester_id=42), "answer")
        self.assertEqual(ns["generate_gemini_text"].call_args.kwargs["user_id"], 42)
        ns["GEMINI_CONFIG"] = None
        ns["TEXT_AI_CONFIG_ERROR"] = "Gemini 설정 필요"
        self.assertEqual(await ns["_generate_image_bytes"]("sunset", requester_id=42), b"png")
        ns["generate_horde_image"].assert_awaited_once_with("sunset", config=ns["HORDE_CONFIG"])
        with self.assertRaisesRegex(ai_providers.AIProviderError, "설정"):
            await ns["_generate_text_response"]("hello", requester_id=42)
        ns["generate_gemini_text"].assert_awaited_once()

    async def test_prefix_question_keeps_public_output_and_complete_answer(self):
        ns = services()
        ns["_generate_text_response"] = AsyncMock(return_value="first\n\nsecond")
        class Typing:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
        ctx = SimpleNamespace(author=SimpleNamespace(id=42), typing=Typing, reply=AsyncMock())
        await ns["ask"](ctx, prompt="hello")
        ctx.reply.assert_awaited_once()
        self.assertIn("first\n\nsecond", ctx.reply.call_args.args[0])
        self.assertNotIn("ephemeral", ctx.reply.call_args.kwargs)
        ns["_generate_text_response"].assert_awaited_once_with("hello", requester_id=42)

    async def test_prefix_empty_answer_and_post_failure_are_handled_without_retry(self):
        class Typing:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
        for answer, send_failure in (("<think>only hidden</think>", False), ("answer", True)):
            with self.subTest(send_failure=send_failure):
                ns = services()
                ns["_generate_text_response"] = AsyncMock(return_value=answer)
                ctx = SimpleNamespace(author=SimpleNamespace(id=42), typing=Typing, reply=AsyncMock())
                if send_failure:
                    ctx.reply.side_effect = PermissionError("private transport detail")
                await ns["ask"](ctx, prompt="hello")
                ns["_generate_text_response"].assert_awaited_once()
                self.assertEqual(ctx.reply.await_count, 2 if send_failure else 1)
                failure_args, failure_kwargs = ctx.reply.call_args
                self.assertNotIn("private transport detail", failure_args[0])
                self.assertNotIn("only hidden", failure_args[0])
                self.assertEqual(failure_kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_typing_sender_uses_container_payload_and_preserves_delay_cooldown(self):
        ns = services()
        ns["typing_intro_payload"] = bot_ui.typing_intro_payload
        for name in ("_question_interaction_service", "_image_interaction_service", "_growth_interaction_service", "_menu_interaction_service"):
            ns[name] = AsyncMock()
        async def history(**_kwargs):
            if False:
                yield None
        channel = SimpleNamespace(id=1, guild=SimpleNamespace(id=1), send=AsyncMock(), history=history)
        user = SimpleNamespace(id=42, mention="<@42>")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await ns["_send_typing_reminder"](channel, user, (1, 42), time.time())
            sleep.assert_awaited_once_with(5)
            channel.send.assert_awaited_once()
            payload = channel.send.call_args.kwargs
            self.assertNotIn("content", payload)
            self.assertNotIn("embeds", payload)
            self.assertIsInstance(payload["view"], discord.ui.LayoutView)
            self.assertIsInstance(payload["view"].children[0], discord.ui.Container)
            await ns["_send_typing_reminder"](channel, user, (1, 42), time.time())
            channel.send.assert_awaited_once()
            sleep.assert_awaited_once()

    async def test_image_success_stays_public_with_private_confirmation(self):
        ns = services()
        ns["GEMINI_CONFIG"] = None
        ns["_generate_image_bytes"] = AsyncMock(return_value=b"validated-image")
        target = interaction()
        await ns["_image_interaction_service"](target, "sunset")
        target.channel.send.assert_awaited_once()
        self.assertNotIn("ephemeral", target.channel.send.call_args.kwargs)
        self.assertEqual(target.channel.send.call_args.kwargs["file"].filename, "generated.png")
        target.followup.send.assert_awaited_once_with("✅ 그림을 채널에 올렸어요.", ephemeral=True)

    async def test_deleted_message_log_contains_metadata_without_original_text(self):
        ns = services()
        ns["seoul_tz"] = datetime.timezone(datetime.timedelta(hours=9))
        ns["CHANNEL_TO_LOG"] = {1: 10}
        log_channel = SimpleNamespace(send=AsyncMock())
        ns["bot"].get_channel = Mock(return_value=log_channel)
        target = message("private message body")
        target.channel.mention = "<#1>"
        target.guild.name = "test guild"
        await ns["on_message_delete"](target)
        kwargs = log_channel.send.call_args.kwargs
        self.assertNotIn("private message body", str(kwargs["embed"].to_dict()))
        self.assertIn("123", str(kwargs["embed"].to_dict()))
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])


if __name__ == "__main__":
    unittest.main()
