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
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import moderation_rules
from edit_moderation import evaluate_policy

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
    "_evaluate_message_policy", "_moderate_message_content", "_moderate_ai_prompt_interaction", "on_message",
    "_interaction_send", "_interaction_defer", "_question_interaction_service",
    "_publish_question_answer", "_generate_text_response", "_generate_image_bytes",
    "_reserve_image_request", "ask", "strip_think", "_send_typing_reminder",
    "_deliver_typing_reminder", "_typing_scope_key", "_typing_notice_on_cooldown",
    "_image_interaction_service", "on_message_delete", "on_raw_message_delete",
    "on_raw_bulk_message_delete", "on_message_edit", "on_raw_message_edit",
}


def services():
    namespace = {
        "asyncio": asyncio, "discord": discord, "datetime": datetime,
        "logging": logging, "time": time, "re": re, "io": io,
        "find_badroot": moderation_rules.find_badroot,
        "evaluate_policy": evaluate_policy,
        "EDIT_MODERATION": SimpleNamespace(known_outcome=Mock(return_value=None), record_new_outcome=Mock(),
            on_cached_edit=AsyncMock(), on_raw_edit=AsyncMock()),
        "lookup_rule_exemption": moderation_rules.lookup_exemption,
        "parse_command": moderation_rules.parse_command,
        "ALLOWED_CHANNELS": {99}, "EXEMPT_PROFANITY_CHANNEL_IDS": {98},
        "LINK_REGEX": moderation_rules.LINK_REGEX,
        "user_xp_data": {}, "get_today_date": lambda: "2026-10-21",
        "save_xp_data": Mock(), "save_achievements_data": Mock(), "check_achievements": Mock(return_value=[]),
        "safe_delete": AsyncMock(return_value=True), "_metric": Mock(),
        "AIProviderError": ai_providers.AIProviderError if ai_providers else RuntimeError,
        "GEMINI_CONFIG": object(), "GEMINI_LIMITER": object(), "HORDE_CONFIG": object(),
        "SYS_PROMPT": "same-language, four sentences", "MAX_TOKENS": 512,
        "TEXT_AI_CONFIG_ERROR": None, "_image_request_lock": asyncio.Lock(),
        "_image_generation_semaphore": asyncio.Semaphore(2),
        "_last_image_request": {}, "IMAGE_REQUEST_COOLDOWN": 20,
        "THINK_RE": re.compile(r"<think>.*?</think>", re.S),
        "bot": SimpleNamespace(user=None, all_commands={"ask": object(), "img": object(), "xp": object()}, process_commands=AsyncMock()),
        "TYPE_REMINDER_COOLDOWN": 86400, "TYPING_INTRO_DELAY_SECONDS": 1.3,
        "_last_typing_notice": {}, "_typing_notice_inflight": set(), "_typing_tasks": {},
        "_persist_typing_cooldowns": AsyncMock(),
        "_observe_typing_message": Mock(), "_typing_message_fallback": Mock(),
        "_plain_admin_message_hint": AsyncMock(return_value=False),
        "DELETION_AUDIT": SimpleNamespace(remember=Mock(), delete=AsyncMock(return_value=True),
            on_message_delete=AsyncMock(), on_raw_message_delete=AsyncMock(),
            on_raw_bulk_message_delete=AsyncMock(), on_raw_message_edit=Mock()),
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
            for content in ("병12121신", "ㅂ122121ㅅ", "https://example.com", "병ㄱㄱㄱ신 https://example.com"):
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
            ns["_observe_typing_message"].assert_called_once()
            ns["_typing_message_fallback"].assert_not_called()
        ns = services()
        await ns["on_message"](message("!ask hello"))
        ns["bot"].process_commands.assert_awaited_once()
        ns["_typing_message_fallback"].assert_not_called()

    async def test_message_fallback_runs_only_after_media_and_spam_acceptance(self):
        class ReachedFallback(Exception):
            pass

        for gate in ("blocked_media", "spam", "accepted"):
            with self.subTest(gate=gate):
                ns = services()
                target = message("hello")
                target.attachments = []
                target.clean_content = target.content
                target.mentions = []
                target.reference = None
                ns.update({
                    "BLOCK_MEDIA_USER_IDS": {42} if gate == "blocked_media" else set(),
                    "EXEMPT_MEDIA_CHANNEL_IDS": set(), "PRIMARY_EXEMPT_MEDIA_CH_ID": 99,
                    "_dbg": Mock(), "_message_has_blocked_media_or_emoji": Mock(return_value=True),
                    "make_surveillance_embed": Mock(return_value=(object(), object())),
                    "push_recent_message": Mock(), "SPAM_ENABLED": gate == "spam",
                    "_is_exempt": Mock(return_value=False), "is_user_exempt_from_spam": Mock(return_value=False),
                    "check_spam_and_reason": Mock(return_value="frequency"),
                    "_user_deletion_rate": {}, "_user_violations": {42: 1},
                    "SPAM_CFG": {"auto_timeout_threshold": 5},
                    "random": SimpleNamespace(random=lambda: 1.0),
                    "_typing_message_fallback": Mock(side_effect=ReachedFallback),
                    "add_xp": Mock(side_effect=AssertionError("test stops before XP")),
                })
                if gate == "accepted":
                    with self.assertRaises(ReachedFallback):
                        await ns["on_message"](target)
                    ns["_typing_message_fallback"].assert_called_once_with(target)
                else:
                    await ns["on_message"](target)
                    ns["_typing_message_fallback"].assert_not_called()
                ns["add_xp"].assert_not_called()


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

    async def test_slow_and_http_failures_are_deferred_private_and_never_retried(self):
        for code in ("timeout", "network", "quota", "authentication", "model_unavailable", "server"):
            with self.subTest(code=code):
                ns = services()
                target = interaction()
                async def fail_after_defer(*_args, **_kwargs):
                    self.assertTrue(target.response.is_done())
                    raise ai_providers.GeminiProviderError(code, "안전한 오류 안내")
                ns["_generate_text_response"] = AsyncMock(side_effect=fail_after_defer)
                await ns["_question_interaction_service"](target, "private prompt")
                target.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                target.channel.send.assert_not_awaited()
                target.followup.send.assert_awaited_once_with("안전한 오류 안내", ephemeral=True)
                ns["_generate_text_response"].assert_awaited_once()

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
        channel = SimpleNamespace(id=1, guild=SimpleNamespace(id=1), send=AsyncMock())
        user = SimpleNamespace(id=42, mention="<@42>")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await ns["_send_typing_reminder"](channel, user, (1, 42), time.time())
            sleep.assert_awaited_once_with(1.3)
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

    async def test_all_deletion_and_edit_events_use_one_audit_coordinator(self):
        ns = services()
        target = message("private message body")
        await ns["on_message_delete"](target)
        await ns["on_raw_message_delete"](target)
        await ns["on_raw_bulk_message_delete"](target)
        await ns["on_message_edit"](None, target)
        await ns["on_raw_message_edit"](target)
        audit = ns["DELETION_AUDIT"]
        audit.on_message_delete.assert_awaited_once_with(target)
        audit.on_raw_message_delete.assert_awaited_once_with(target)
        audit.on_raw_bulk_message_delete.assert_awaited_once_with(target)
        audit.remember.assert_called_once_with(target)
        audit.on_raw_message_edit.assert_called_once_with(target)
        ns["EDIT_MODERATION"].on_cached_edit.assert_awaited_once_with(None, target)
        ns["EDIT_MODERATION"].on_raw_edit.assert_awaited_once_with(target)

    async def test_moderation_keeps_original_separate_from_measured_rule(self):
        ns = services()
        original = "시321133발"
        target = message(original)
        measured_rule = moderation_rules.find_badroot(original)
        self.assertIsNotNone(measured_rule)
        self.assertFalse(await ns["_moderate_message_content"](target))
        ns["safe_delete"].assert_awaited_once_with(target, reason="profanity", matched_rule=measured_rule)
        self.assertEqual(target.content, original)


@unittest.skipIf(discord is None, "requires actual discord.py")
class MainDeletionAuditIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_digit_obfuscated_consonants_are_deleted_before_xp_and_keep_raw_audit_body(self):
        ns, target, log_channel, audit = self.audit_context("ㅂ122121ㅅ")
        violation = ns["_evaluate_message_policy"](target)
        self.assertIsNotNone(violation)
        self.assertEqual(violation.matched_rule, "ㅄ")
        ns["add_xp"] = Mock(side_effect=AssertionError("blocked message awarded XP"))
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            await ns["on_message"](target)
            await ns["on_message_delete"](target)
            await ns["on_raw_message_delete"](SimpleNamespace(
                guild_id=1, channel_id=1, message_id=123, cached_message=target))
        target.delete.assert_awaited_once()
        log_channel.send.assert_awaited_once()
        card = log_channel.send.call_args.kwargs["embed"]
        self.assertEqual(next(field.value for field in card.fields if field.name == "Deleted Content"),
                         "ㅂ122121ㅅ")
        self.assertIn("ㅄ", next(field.value for field in card.fields if "삭제 사유" in field.name))
        self.assertEqual(target.content, "ㅂ122121ㅅ")
        self.assertIn((target.guild.id, target.id), audit._delivered)
        self.assertNotIn(target.content, "\n".join(captured.output))
        ns["add_xp"].assert_not_called()
        ns["bot"].process_commands.assert_not_awaited()
        ns["_typing_message_fallback"].assert_not_called()

    async def test_digit_obfuscated_consonant_edit_reuses_shared_policy_and_single_audit(self):
        from edit_moderation import EditModerationService

        ns, target, log_channel, audit = self.audit_context("ㅇ")
        now = datetime.datetime.now(datetime.timezone.utc)
        target.created_at, target.edited_at = now, None
        worker = EditModerationService(
            SimpleNamespace(get_channel=lambda _id: target.channel, user=SimpleNamespace(id=99)),
            evaluate=ns["_evaluate_message_policy"], delete=ns["safe_delete"],
            consume_pass=ns["use_profanity_pass"], has_pass=ns["is_user_exempt_from_profanity"],
            remember=audit.remember)
        ns["EDIT_MODERATION"] = worker
        self.assertTrue(await ns["_moderate_message_content"](target))
        before = SimpleNamespace(**vars(target))
        target.content, target.edited_at = "ㅂ122121ㅅ", now + datetime.timedelta(seconds=1)
        raw = SimpleNamespace(guild_id=1, channel_id=1, message_id=123,
                              cached_message=before, message=target,
                              data={"content": target.content, "edited_timestamp": target.edited_at.isoformat()})
        ns["add_xp"] = Mock(side_effect=AssertionError("edit replayed XP"))
        ns["_generate_text_response"] = AsyncMock(side_effect=AssertionError("edit replayed AI"))
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            await asyncio.gather(ns["on_raw_message_edit"](raw), ns["on_message_edit"](before, target))
            await ns["on_message_delete"](target)
        target.delete.assert_awaited_once()
        log_channel.send.assert_awaited_once()
        card = log_channel.send.call_args.kwargs["embed"]
        self.assertEqual(next(field.value for field in card.fields if field.name == "Deleted Content"),
                         "ㅂ122121ㅅ")
        reason = next(field.value for field in card.fields if "삭제 사유" in field.name)
        self.assertIn("ㅄ", reason)
        self.assertIn("수정 후 검열", str(card.to_dict()))
        self.assertNotIn(target.content, "\n".join(captured.output))
        ns["add_xp"].assert_not_called()
        ns["_generate_text_response"].assert_not_awaited()
        ns["_observe_typing_message"].assert_not_called()
        ns["_typing_message_fallback"].assert_not_called()

    async def test_naver_link_edit_uses_real_policy_delete_and_public_audit_once(self):
        from edit_moderation import EditModerationService

        for mode in ("cached_first", "raw_first", "uncached", "partial", "preview"):
            with self.subTest(mode=mode):
                ns, target, log_channel, audit = self.audit_context("ㅇ")
                now = datetime.datetime.now(datetime.timezone.utc)
                target.created_at, target.edited_at = now, None
                target.channel.fetch_message = AsyncMock(return_value=target)
                target.channel.get_partial_message = Mock(return_value=SimpleNamespace(delete=target.delete))
                worker = EditModerationService(
                    SimpleNamespace(get_channel=lambda _id: target.channel, user=SimpleNamespace(id=99)),
                    evaluate=ns["_evaluate_message_policy"], delete=ns["safe_delete"],
                    consume_pass=ns["use_profanity_pass"], has_pass=ns["is_user_exempt_from_profanity"],
                    remember=audit.remember)
                ns["EDIT_MODERATION"] = worker
                self.assertTrue(await ns["_moderate_message_content"](target))
                before = SimpleNamespace(**vars(target))
                target.content, target.edited_at = "https://naver.com", now + datetime.timedelta(seconds=1)
                data = {"id": "123", "channel_id": "1", "content": target.content,
                        "edited_timestamp": target.edited_at.isoformat(), "author": {"id": "42", "username": "author"}}
                raw = SimpleNamespace(guild_id=1, channel_id=1, message_id=123,
                                      cached_message=None if mode == "uncached" else before,
                                      message=target if mode != "uncached" else None, data=data)
                if mode == "partial":
                    data.pop("content")
                elif mode == "preview":
                    raw.data = {"id": "123", "channel_id": "1", "embeds": [{"type": "link", "url": target.content}]}
                ns["add_xp"] = Mock(side_effect=AssertionError("edit replayed XP"))
                ns["_generate_text_response"] = AsyncMock(side_effect=AssertionError("edit replayed AI"))
                with self.assertLogs("deletion_audit", level="INFO") as captured:
                    if mode == "cached_first":
                        await ns["on_message_edit"](before, target)
                        await ns["on_raw_message_edit"](raw)
                    else:
                        await ns["on_raw_message_edit"](raw)
                        await ns["on_message_edit"](before, target)
                    await ns["on_message_delete"](target)
                target.delete.assert_awaited_once()
                log_channel.send.assert_awaited_once()
                card = log_channel.send.call_args.kwargs["embed"]
                original = next(field.value for field in card.fields if field.name == "Deleted Content")
                self.assertEqual(original.replace("\u200b", ""), "https://naver.com")
                self.assertIn("링크", next(field.value for field in card.fields if "사유" in field.name))
                self.assertIn("수정", str(card.to_dict()))
                self.assertNotIn(target.content, "\n".join(captured.output))
                ns["add_xp"].assert_not_called()
                ns["_generate_text_response"].assert_not_awaited()
                ns["_observe_typing_message"].assert_not_called()
                ns["_typing_message_fallback"].assert_not_called()
                self.assertEqual(target.channel.fetch_message.await_count, int(mode in {"partial", "preview"}))

    def audit_context(self, content="시321133발"):
        from deletion_audit import DeletionAuditService

        ns = services()
        target = message(content)
        target.delete = AsyncMock()
        target.attachments, target.stickers, target.embeds = [], [], []
        target.author.display_name = "테스트 사용자"
        target.guild.name = "테스트 서버"
        everyone = SimpleNamespace(id=1, permissions=discord.Permissions(view_channel=True))
        target.guild.default_role, target.guild.me = everyone, SimpleNamespace(id=99)
        target.guild.owner_id, target.guild.roles = 77, [everyone]
        source_channel = MagicMock(spec=discord.TextChannel)
        source_channel.id, source_channel.guild, source_channel.overwrites = 1, target.guild, {}
        source_channel.name, source_channel.send = "general", AsyncMock()
        source_channel.type = discord.ChannelType.text
        source_channel.permissions_for = lambda _actor: SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True,
            read_message_history=True, manage_messages=True)
        target.channel = source_channel
        log_channel = MagicMock(spec=discord.TextChannel)
        log_channel.id, log_channel.guild, log_channel.overwrites = 10, target.guild, {}
        log_channel.send = AsyncMock()
        log_channel.permissions_for = source_channel.permissions_for
        audit = DeletionAuditService({1: 10}, {1: source_channel, 10: log_channel}.get,
                                     include_content=True, configured_admin_ids=())
        ns["DELETION_AUDIT"] = audit
        safe_delete_node = next(node for node in SOURCE.body
                                if isinstance(node, ast.AsyncFunctionDef) and node.name == "safe_delete")
        module = ast.Module(body=[safe_delete_node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "main.py", "exec"), ns)
        return ns, target, log_channel, audit

    async def test_real_moderation_to_public_card_preserves_original_and_deduplicates_events(self):
        ns, target, log_channel, audit = self.audit_context()
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            self.assertFalse(await ns["_moderate_message_content"](target))
            await ns["on_message_delete"](target)
            await ns["on_raw_message_delete"](SimpleNamespace(
                guild_id=1, channel_id=1, message_id=123, cached_message=target))
        target.delete.assert_awaited_once()
        log_channel.send.assert_awaited_once()
        payload = log_channel.send.call_args.kwargs
        rendered = str(payload["embed"].to_dict())
        self.assertIn("시321133발", rendered)
        self.assertIn(moderation_rules.find_badroot(target.content), rendered)
        self.assertEqual(payload["embed"].description, "**User:** <@42>\n**Channel:** <#1>")
        self.assertNotIn("메시지 ID", rendered)
        self.assertIn((target.guild.id, target.id), audit._delivered)
        self.assertNotIn("시321133발", "\n".join(captured.output))
        self.assertEqual(payload["allowed_mentions"].to_dict()["parse"], [])

    async def test_v8_explicit_private_moderation_restores_legacy_metadata_without_body(self):
        with patch.dict("os.environ", {"DELETION_PUBLIC_LOG_REGISTERED_SOURCE_CONTENT": "0"}):
            ns, target, log_channel, audit = self.audit_context()
        target.channel.name, target.author.display_name = "moderator-only", "Apple Watch"
        target.channel.overwrites = {
            discord.Object(id=1, type=discord.Role): discord.PermissionOverwrite(view_channel=False)}
        target.attachments = [SimpleNamespace(id=444, filename="PRIVATE-FILENAME.png", content_type="image/png",
                                             size=128, width=32, height=32, is_spoiler=lambda: False)]
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            self.assertFalse(await ns["_moderate_message_content"](target))
            await asyncio.gather(ns["on_message_delete"](target), ns["on_raw_message_delete"](SimpleNamespace(
                guild_id=1, channel_id=1, message_id=123, cached_message=target)))
        target.delete.assert_awaited_once()
        log_channel.send.assert_awaited_once()
        card = log_channel.send.call_args.kwargs["embed"]
        rendered = str(card.to_dict())
        for legacy in ("<@42>", "<#1>", "KST"):
            self.assertIn(legacy, rendered)
        for redundant in ("Apple Watch", "moderator-only", "메시지 ID", "채널 ID"):
            self.assertNotIn(redundant, rendered)
        self.assertIn((target.guild.id, target.id), audit._delivered)
        self.assertIn("금칙어", rendered)
        for secret in (target.content, "PRIVATE-FILENAME", moderation_rules.find_badroot(target.content)):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, "\n".join(captured.output))
        status = audit.diagnostics(target.guild)
        self.assertEqual(status["sent_full"], 0)
        self.assertEqual(status["sent_metadata_only"], 1)
        self.assertEqual(log_channel.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_v8_log_bot_message_returns_before_moderation_xp_or_audit(self):
        ns = services()
        ns["bot"].user = SimpleNamespace(id=99)
        target = message("시321133발", channel_id=10)
        target.author = SimpleNamespace(id=99, bot=True)
        ns["add_xp"] = Mock(side_effect=AssertionError("audit replayed XP"))
        await ns["on_message"](target)
        ns["safe_delete"].assert_not_awaited()
        ns["DELETION_AUDIT"].remember.assert_not_called()
        ns["add_xp"].assert_not_called()
        ns["bot"].process_commands.assert_not_awaited()

    async def test_v8_explicit_private_full_opt_in_keeps_real_rule_and_original_separate(self):
        with patch.dict("os.environ", {"DELETION_PUBLIC_LOG_PRIVATE_FULL_SOURCE_IDS": "1"}):
            ns, target, log_channel, audit = self.audit_context()
        target.channel.name, target.author.display_name = "moderator-only", "Apple Watch"
        target.channel.overwrites = {
            discord.Object(id=1, type=discord.Role): discord.PermissionOverwrite(view_channel=False)}
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            self.assertFalse(await ns["_moderate_message_content"](target))
            await ns["on_raw_message_delete"](SimpleNamespace(
                guild_id=1, channel_id=1, message_id=123, cached_message=target))
        target.delete.assert_awaited_once()
        log_channel.send.assert_awaited_once()
        card = log_channel.send.call_args.kwargs["embed"]
        self.assertEqual(card.description, "**User:** <@42>\n**Channel:** <#1>")
        original = next(field.value for field in card.fields if field.name == "Deleted Content")
        detected = next(field.value for field in card.fields if "삭제 사유" in field.name)
        self.assertIn("시321133발", original)
        self.assertIn(moderation_rules.find_badroot(target.content), detected)
        self.assertEqual(audit.diagnostics(target.guild)["sent_full"], 1)
        self.assertNotIn(target.content, "\n".join(captured.output))

    async def test_real_raw_and_cached_edit_to_public_card_never_replays_xp_ai_or_typing(self):
        from edit_moderation import EditModerationService

        ns, target, log_channel, audit = self.audit_context("1")
        now = datetime.datetime.now(datetime.timezone.utc)
        target.created_at, target.edited_at = now, None
        worker = EditModerationService(
            SimpleNamespace(get_channel=lambda _id: target.channel, user=SimpleNamespace(id=99)),
            evaluate=ns["_evaluate_message_policy"], delete=ns["safe_delete"],
            consume_pass=ns["use_profanity_pass"], has_pass=ns["is_user_exempt_from_profanity"],
            remember=audit.remember)
        ns["EDIT_MODERATION"] = worker
        self.assertTrue(await ns["_moderate_message_content"](target))
        before = SimpleNamespace(**vars(target))
        target.content, target.edited_at = "시321133발", now + datetime.timedelta(seconds=1)
        raw = SimpleNamespace(guild_id=1, channel_id=1, message_id=123,
                              cached_message=before, message=target,
                              data={"content": target.content, "edited_timestamp": target.edited_at.isoformat()})
        ns["add_xp"] = Mock(side_effect=AssertionError("edit replayed XP"))
        ns["_generate_text_response"] = AsyncMock(side_effect=AssertionError("edit replayed AI"))
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            await asyncio.gather(ns["on_raw_message_edit"](raw), ns["on_message_edit"](before, target))
            await ns["on_message_delete"](target)
        target.delete.assert_awaited_once()
        log_channel.send.assert_awaited_once()
        rendered = str(log_channel.send.call_args.kwargs["embed"].to_dict())
        self.assertIn("시321133발", rendered)
        self.assertIn("수정", rendered)
        self.assertEqual(log_channel.send.call_args.kwargs["embed"].description,
                         "**User:** <@42>\n**Channel:** <#1>")
        self.assertNotIn("메시지 ID", rendered)
        self.assertIn((target.guild.id, target.id), audit._delivered)
        self.assertNotIn("시321133발", "\n".join(captured.output))
        ns["add_xp"].assert_not_called()
        ns["_generate_text_response"].assert_not_awaited()
        ns["_observe_typing_message"].assert_not_called()
        ns["_typing_message_fallback"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
