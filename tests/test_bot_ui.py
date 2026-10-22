from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_ui

OLD_ADMIN = 855749166764654653
NEW_ADMIN = 491239880005910538


def interaction(actor=10, guild_id=1, *, action=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=actor),
        guild=SimpleNamespace(id=guild_id, owner_id=10) if guild_id is not None else None,
        data={"custom_id": action},
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(),
                                 edit_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class BotUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        for name in ("SPECIAL_ADMIN_USER_ID", "SPECIAL_ADMIN_USER_IDS", "SPECIAL_ADMIN_GUILD_IDS"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.environment.stop()

    async def test_legacy_intro_preserves_four_button_ids_rows_and_persistence(self):
        view = bot_ui.IntroView()
        self.assertEqual([child.label for child in view.children], list(bot_ui.IntroView.BUTTON_LABELS))
        self.assertEqual([child.custom_id for child in view.children], list(bot_ui.IntroView.BUTTON_IDS))
        self.assertEqual([child.row for child in view.children], [0, 0, 1, 1])
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())

    async def test_typing_notice_has_one_box_with_text_and_two_button_rows(self):
        user = SimpleNamespace(id=42, display_name="Apple Watch", mention="<@42>")
        payload = bot_ui.typing_intro_payload(user)
        self.assertEqual(set(payload), {"view", "allowed_mentions"})
        view = payload["view"]
        self.assertIsInstance(view, bot_ui.TypingIntroView)
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.children), 1)
        container = view.children[0]
        self.assertEqual(len(container.children), 6)
        text, divider, heading, first, spacer, second = container.children
        self.assertEqual(text.content, "**👀 Apple Watch님, 채팅을 작성 중이셨네요!**\n\n필요하시면 도리봇이 도와드릴게요.")
        self.assertEqual(heading.content, "**🐻 도리봇 · 빠른 실행**")
        self.assertTrue(divider.visible)
        self.assertEqual(divider.spacing, bot_ui.discord.SeparatorSpacing.small)
        self.assertFalse(spacer.visible)
        self.assertEqual(spacer.spacing, bot_ui.discord.SeparatorSpacing.large)
        self.assertEqual([len(first.children), len(second.children)], [2, 2])
        buttons = [*first.children, *second.children]
        self.assertEqual([button.custom_id for button in buttons], list(view.BUTTON_IDS))
        self.assertEqual([button.label for button in buttons], list(view.BUTTON_LABELS))
        self.assertEqual(buttons[0].style, bot_ui.discord.ButtonStyle.primary)
        self.assertTrue(all(button.style == bot_ui.discord.ButtonStyle.secondary for button in buttons[1:]))
        self.assertFalse(payload["allowed_mentions"].users)
        self.assertFalse(payload["allowed_mentions"].everyone)
        self.assertFalse(payload["allowed_mentions"].roles)
        self.assertFalse(payload["allowed_mentions"].replied_user)
        if bot_ui.DISCORD_AVAILABLE:
            components = view.to_components()
            self.assertEqual(len(components), 1)
            self.assertEqual(components[0]["type"], 17)
            self.assertEqual([item["type"] for item in components[0]["components"]], [10, 14, 10, 1, 14, 1])
            self.assertEqual([len(components[0]["components"][index]["components"]) for index in (3, 5)], [2, 2])
            self.assertEqual(payload["allowed_mentions"].to_dict()["parse"], [])

    async def test_typing_buttons_execute_each_original_callback(self):
        question, image, growth, menu = (AsyncMock() for _ in range(4))
        view = bot_ui.TypingIntroView(question_service=question, image_service=image,
                                     growth_service=growth, menu_service=menu)
        children = view.children[0].children
        buttons = [*children[3].children, *children[5].children]
        target = interaction()
        await buttons[0].callback(target)
        self.assertIs(target.response.send_modal.call_args.args[0].question_service, question)
        await buttons[1].callback(target)
        self.assertIs(target.response.send_modal.call_args.args[0].image_service, image)
        await buttons[2].callback(target)
        growth.assert_awaited_once_with(target)
        await buttons[3].callback(target)
        menu.assert_awaited_once_with(target)

    async def test_both_intro_layouts_keep_callbacks_to_the_same_services(self):
        question, image, growth, menu = (AsyncMock() for _ in range(4))
        for view_type in (bot_ui.IntroView, bot_ui.TypingIntroView):
            view = view_type(question_service=question, image_service=image,
                             growth_service=growth, menu_service=menu)
            target = interaction()
            await view.question(target)
            modal = target.response.send_modal.call_args.args[0]
            self.assertIsInstance(modal, bot_ui.QuestionModal)
            self.assertIs(modal.question_service, question)
            await view.image(target)
            self.assertIs(target.response.send_modal.call_args.args[0].image_service, image)
            await view.growth(target)
            growth.assert_awaited_with(target)
            await view.menu(target)
            menu.assert_awaited_with(target)

    async def test_question_modal_discloses_public_answer_before_submission(self):
        service = AsyncMock()
        modal = bot_ui.QuestionModal(service)
        self.assertIn("이 채널에 공개됩니다", modal.public_answer_notice)
        if bot_ui.DISCORD_AVAILABLE:
            self.assertIn("이 채널에 공개됩니다", modal.prompt.placeholder)
        modal.prompt = SimpleNamespace(value="  한국어 질문?  ")
        target = interaction()
        await modal.on_submit(target)
        service.assert_awaited_once_with(target, "한국어 질문?")

    async def test_feature_menu_question_routes_through_same_modal_service(self):
        service = AsyncMock()
        view = bot_ui.FeatureMenuView(question_service=service)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].custom_id, "ui:feature-menu")
        target = interaction()
        target.data = {"values": ["question"]}
        await view._select_callback(target)
        modal = target.response.send_modal.call_args.args[0]
        self.assertIs(modal.question_service, service)
        modal.prompt = SimpleNamespace(value="English question?")
        await modal.on_submit(target)
        service.assert_awaited_once_with(target, "English question?")

    async def test_slash_question_routes_through_same_disclosed_modal(self):
        service = AsyncMock()
        tree = SimpleNamespace(add_command=lambda command: None)
        commands = bot_ui.register_slash_commands(tree, question_service=service)
        command = next(command for command in commands if command.name == "질문")
        target = interaction()
        await getattr(command, "callback", command)(target)
        modal = target.response.send_modal.call_args.args[0]
        self.assertIs(modal.question_service, service)
        self.assertIn("공개", modal.public_answer_notice)
        modal.prompt = SimpleNamespace(value="같은 질문 경로")
        await modal.on_submit(target)
        service.assert_awaited_once_with(target, "같은 질문 경로")

    async def test_owner_only_when_both_special_id_settings_are_unset(self):
        guild = SimpleNamespace(id=1, owner_id=10)
        self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=10), guild))
        for admin in (OLD_ADMIN, NEW_ADMIN):
            self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=admin), guild))
            self.assertEqual(bot_ui.admin_authorization_reason(SimpleNamespace(id=admin), guild), "special_id_unconfigured")

    async def test_legacy_single_id_remains_supported(self):
        os.environ["SPECIAL_ADMIN_USER_ID"] = str(OLD_ADMIN)
        guild = SimpleNamespace(id=1, owner_id=10)
        self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=OLD_ADMIN), guild))
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=NEW_ADMIN), guild))

    async def test_legacy_and_new_ids_are_merged_and_deduplicated(self):
        os.environ["SPECIAL_ADMIN_USER_ID"] = str(OLD_ADMIN)
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f" {NEW_ADMIN}, {OLD_ADMIN}, {NEW_ADMIN} "
        self.assertEqual(bot_ui.configured_special_admin_ids(), {OLD_ADMIN, NEW_ADMIN})
        guild = SimpleNamespace(id=1, owner_id=10)
        for admin in (OLD_ADMIN, NEW_ADMIN):
            self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=admin), guild))

    async def test_plural_setting_alone_supports_both_admins(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f"{OLD_ADMIN},{NEW_ADMIN}"
        for admin in (OLD_ADMIN, NEW_ADMIN):
            self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=admin), SimpleNamespace(id=1, owner_id=10)))

    async def test_any_invalid_setting_fails_closed_but_owner_is_preserved(self):
        guild = SimpleNamespace(id=1, owner_id=10)
        for invalid in ("not-an-id", "0", "-1", "+1", "١٢", "1,,2", "1,", str(2**64)):
            with self.subTest(invalid=invalid):
                os.environ["SPECIAL_ADMIN_USER_ID"] = str(OLD_ADMIN)
                os.environ["SPECIAL_ADMIN_USER_IDS"] = invalid
                self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=OLD_ADMIN), guild))
                self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=NEW_ADMIN), guild))
                self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=10), guild))
                self.assertEqual(bot_ui.admin_authorization_reason(SimpleNamespace(id=OLD_ADMIN), guild), "special_id_invalid")
        os.environ["SPECIAL_ADMIN_USER_ID"] = "bad"
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=NEW_ADMIN), guild))

    async def test_guild_scope_applies_to_both_special_ids_and_not_owner(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f"{OLD_ADMIN},{NEW_ADMIN}"
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "2,3"
        for admin in (OLD_ADMIN, NEW_ADMIN):
            self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=admin), SimpleNamespace(id=1, owner_id=10)))
            self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=admin), SimpleNamespace(id=2, owner_id=10)))
        self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=10), SimpleNamespace(id=1, owner_id=10)))
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "2,bad"
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=OLD_ADMIN), SimpleNamespace(id=2, owner_id=10)))

    async def test_dm_roles_display_names_and_noninteger_actor_ids_do_not_authorize(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f"{OLD_ADMIN},{NEW_ADMIN}"
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=OLD_ADMIN), None))
        guild = SimpleNamespace(id=1, owner_id=10)
        actor = SimpleNamespace(id=20, display_name="파딱", guild_permissions=SimpleNamespace(administrator=True), roles=["administrator"])
        self.assertFalse(bot_ui.is_admin_authorized(actor, guild))
        for actor_id in (str(OLD_ADMIN), True, None, OLD_ADMIN + 1):
            self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=actor_id), guild))

    async def test_admin_panel_is_ephemeral_and_keeps_all_existing_action_ids(self):
        target = interaction()
        await bot_ui.admin_panel_response(target)
        args = target.response.send_message.call_args
        self.assertTrue(args.kwargs["ephemeral"])
        self.assertIn("파딱", args.args[0])
        self.assertEqual([button.custom_id for button in args.kwargs["view"].children],
                         ["admin:members", "admin:channel", "admin:rules", "admin:test", "admin:logs", "admin:settings"])

    async def test_typing_greeting_uses_safe_display_names_without_mentions(self):
        names = ("Apple Watch", "늘어버린 도리", "😡🐻👩\u200d💻", "name" * 100,
                 "**name** _x_ `code` ||spoiler|| [label](https://example.com)",
                 "@everyone @here <@123> <@&456>", "line\n\r\tbreak\x00\u202e")
        for name in names:
            with self.subTest(name=name):
                user = SimpleNamespace(id=42, display_name=name, mention="@everyone <@123>")
                payload = bot_ui.typing_intro_payload(user)
                text = payload["view"].text
                self.assertTrue(text.startswith("**👀 "))
                self.assertIn("님, 채팅을 작성 중이셨네요!**", text)
                self.assertEqual(len(text.splitlines()), 3)
                self.assertNotRegex(text, r"<@[!&]?[0-9]+>")
                self.assertNotIn("@everyone", text)
                self.assertNotIn("@here", text)
                self.assertNotIn("\x00", text)
                self.assertNotIn("\u202e", text)
                self.assertLess(len(text), 150)
                self.assertFalse(payload["allowed_mentions"].users)
        self.assertIn("늘어버린 도리", bot_ui.TypingIntroView(SimpleNamespace(display_name="늘어버린 도리")).text)
        self.assertIn("👩\u200d💻", bot_ui.TypingIntroView(SimpleNamespace(display_name="👩\u200d💻")).text)
        self.assertIn("\\*\\*name\\*\\*", bot_ui.TypingIntroView(SimpleNamespace(display_name="**name**")).text)
        self.assertIn("x" * 40 + "…님", bot_ui.TypingIntroView(SimpleNamespace(display_name="x" * 100)).text)

    async def test_typing_name_fallback_never_uses_the_mention_property(self):
        for user in (None, SimpleNamespace(id=42, mention="@everyone"),
                     SimpleNamespace(display_name="\x00\u202e"), SimpleNamespace(display_name=123)):
            self.assertIn("👀 사용자님", bot_ui.TypingIntroView(user).text)
        self.assertIn("👀 account님", bot_ui.TypingIntroView(SimpleNamespace(name="account")).text)

    async def test_configuration_diagnostics_never_contain_raw_values(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f"{OLD_ADMIN},{NEW_ADMIN}"
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "1,2"
        details = bot_ui.admin_configuration_diagnostics()
        self.assertEqual(details["special_id_count"], 2)
        self.assertEqual(details["guild_scope_count"], 2)
        self.assertTrue(details["special_ids_valid"])
        self.assertNotIn(str(NEW_ADMIN), repr(details))
        for invalid in ('"1,2"', "1;2", "1,,2", "1，2", "1,"):
            os.environ["SPECIAL_ADMIN_GUILD_IDS"] = invalid
            self.assertEqual(bot_ui.admin_authorization_reason(SimpleNamespace(id=NEW_ADMIN), SimpleNamespace(id=1, owner_id=10)), "guild_scope_invalid")
            self.assertFalse(bot_ui.admin_configuration_diagnostics()["guild_scope_valid"])

    async def test_admin_open_logs_safe_source_reason_and_guild(self):
        with self.assertLogs(level="INFO") as logs:
            await bot_ui.admin_panel_response(interaction())
        output = " ".join(logs.output)
        self.assertIn("event=admin_open result=allowed reason=authorized guild_id=1 source=slash", output)
        self.assertNotIn(str(NEW_ADMIN), output)

    async def test_panel_is_bound_to_original_actor_and_server_even_for_other_admin(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        service = AsyncMock()
        view = bot_ui.AdminPanelView(action_service=service, actor_id=10, guild_id=1)
        for target in (interaction(NEW_ADMIN, action="admin:rules"), interaction(10, 2, action="admin:rules")):
            await view._button_callback(target)
        service.assert_not_awaited()

    async def test_bound_moderation_modal_rejects_other_admin_and_other_guild(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        service = AsyncMock()
        modal = bot_ui.ModerationTestModal(service, actor_id=10, guild_id=1)
        modal.content = SimpleNamespace(value="test")
        await modal.on_submit(interaction(NEW_ADMIN))
        await modal.on_submit(interaction(10, 2))
        service.assert_not_awaited()

    async def test_admin_service_error_is_private_and_does_not_expose_exception(self):
        service = AsyncMock(side_effect=RuntimeError("SECRET_API_KEY"))
        view = bot_ui.AdminPanelView(action_service=service)
        target = interaction(action="admin:rules")
        with self.assertLogs(level="WARNING") as logs:
            await view._button_callback(target)
        self.assertNotIn("SECRET_API_KEY", " ".join(logs.output))
        args = target.response.send_message.call_args
        self.assertTrue(args.kwargs["ephemeral"])
        self.assertNotIn("SECRET_API_KEY", args.args[0])

    async def test_admin_panel_rechecks_permission_on_every_button(self):
        service = AsyncMock()
        view = bot_ui.AdminPanelView(action_service=service)
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        target = interaction(NEW_ADMIN, action="admin:rules")
        await view._button_callback(target)
        service.assert_awaited_once_with(target, "admin:rules")
        os.environ.pop("SPECIAL_ADMIN_USER_IDS")
        await view._button_callback(target)
        self.assertEqual(service.await_count, 1)
        self.assertTrue(target.response.send_message.call_args.kwargs["ephemeral"])

    async def test_active_rules_pager_displays_all_exact_original_entries_and_correct_count(self):
        roots = [f"word-{index:03}" for index in range(65)] + ["fuck", "Fuck", "FUCK", "병신", "병신"]
        view = bot_ui.ActiveRulesView(reversed(roots), page_size=20)
        self.assertEqual(len(view.roots), 69)
        self.assertEqual(len(view.pages), 4)
        self.assertIn("✅ 활성화된 금칙어: 69개", view.content)
        self.assertIn("📄 1 / 4 페이지", view.content)
        displayed = [line.removeprefix("• ") for page in view.pages for line in page.splitlines()]
        self.assertEqual(set(displayed), set(roots))
        self.assertEqual(len(displayed), len(set(roots)))
        self.assertEqual(view.roots, bot_ui.ActiveRulesView(roots).roots)

    async def test_active_rules_pages_respect_message_limits_without_losing_entries(self):
        roots = [f"{index:03}" + "가" * 200 for index in range(50)]
        view = bot_ui.ActiveRulesView(roots)
        for index in range(len(view.pages)):
            view.page_index = index
            self.assertLessEqual(len(view.content), 2000)
        self.assertEqual(sum(page.count("• ") for page in view.pages), len(roots))
        self.assertTrue(all(root in "\n".join(view.pages) for root in roots))

    async def test_first_middle_last_paging_and_buttons(self):
        view = bot_ui.ActiveRulesView([f"word{index}" for index in range(45)])
        self.assertTrue(view.previous_button.disabled)
        self.assertFalse(view.next_button.disabled)
        target = interaction(action="admin:rules:next")
        await view._button_callback(target)
        self.assertEqual(view.page_index, 1)
        self.assertIn("📄 2 / 3 페이지", target.response.edit_message.call_args.kwargs["content"])
        await view._button_callback(target)
        self.assertEqual(view.page_index, 2)
        self.assertTrue(view.next_button.disabled)
        target.data["custom_id"] = "admin:rules:previous"
        await view._button_callback(target)
        self.assertEqual(view.page_index, 1)

    async def test_each_pager_has_independent_state_and_concurrent_actions_are_bounded(self):
        roots = [str(index) for index in range(60)]
        first, second = bot_ui.ActiveRulesView(roots), bot_ui.ActiveRulesView(roots)
        await asyncio.gather(*(first._button_callback(interaction(action="admin:rules:next")) for _ in range(4)))
        self.assertEqual(first.page_index, 2)
        self.assertEqual(second.page_index, 0)

    async def test_page_and_test_buttons_recheck_auth_after_configuration_changes(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        service = AsyncMock()
        view = bot_ui.ActiveRulesView([str(index) for index in range(50)], test_service=service)
        target = interaction(NEW_ADMIN, action="admin:rules:next")
        await view._button_callback(target)
        self.assertEqual(view.page_index, 1)
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "99"
        await view._button_callback(target)
        self.assertEqual(view.page_index, 1)
        target.data["custom_id"] = "admin:test"
        await view._button_callback(target)
        target.response.send_modal.assert_not_awaited()
        service.assert_not_awaited()
        self.assertTrue(target.response.send_message.call_args.kwargs["ephemeral"])

    async def test_modal_submission_rechecks_admin_permission(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        service = AsyncMock()
        view = bot_ui.ActiveRulesView(["병신"], test_service=service)
        target = interaction(NEW_ADMIN, action="admin:test")
        await view._button_callback(target)
        modal = target.response.send_modal.call_args.args[0]
        modal.content = SimpleNamespace(value="병12121신")
        os.environ.pop("SPECIAL_ADMIN_USER_IDS")
        await modal.on_submit(target)
        service.assert_not_awaited()
        self.assertTrue(target.response.send_message.call_args.kwargs["ephemeral"])

    async def test_both_special_ids_and_owner_can_view_rules_and_test(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = f"{OLD_ADMIN},{NEW_ADMIN}"
        for actor in (10, OLD_ADMIN, NEW_ADMIN):
            with self.subTest(actor=actor):
                service = AsyncMock()
                target = interaction(actor, action="admin:test")
                await bot_ui.send_active_rules_response(target, ["병신", "word"], test_service=service)
                args = target.response.send_message.call_args
                self.assertTrue(args.kwargs["ephemeral"])
                self.assertIn("2개", args.args[0])
                view = args.kwargs["view"]
                await view._button_callback(target)
                modal = target.response.send_modal.call_args.args[0]
                modal.content = SimpleNamespace(value="병12121신")
                await modal.on_submit(target)
                service.assert_awaited_once_with(target, "병12121신")

    async def test_active_rules_response_denies_unauthorized_dm_and_guild_scope(self):
        os.environ["SPECIAL_ADMIN_USER_IDS"] = str(NEW_ADMIN)
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "2"
        for actor, guild in ((20, 2), (NEW_ADMIN, None), (NEW_ADMIN, 1)):
            target = interaction(actor, guild)
            await bot_ui.send_active_rules_response(target, ["secret policy"])
            args = target.response.send_message.call_args
            self.assertNotIn("view", args.kwargs)
            self.assertTrue(args.kwargs["ephemeral"])
            self.assertIn("파딱", args.args[0])
            self.assertNotIn("secret policy", args.args[0])

    async def test_empty_active_rules_has_one_safe_page(self):
        view = bot_ui.ActiveRulesView([])
        self.assertIn("0개", view.content)
        self.assertIn("1 / 1", view.content)
        self.assertTrue(view.previous_button.disabled)
        self.assertTrue(view.next_button.disabled)

    async def test_no_discord_dependency_import_still_supports_structural_payload(self):
        spec = importlib.util.spec_from_file_location("bot_ui_without_discord_test", Path(bot_ui.__file__))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"discord": None}):
            spec.loader.exec_module(module)
        self.assertFalse(module.DISCORD_AVAILABLE)
        payload = module.typing_intro_payload(SimpleNamespace(mention="<@42>", id=42))
        self.assertEqual(set(payload), {"view", "allowed_mentions"})
        children = payload["view"].children[0].children
        self.assertEqual(len(children), 6)
        self.assertTrue(children[1].visible)
        self.assertFalse(children[4].visible)
        self.assertTrue(payload["view"].is_persistent())
        self.assertFalse(payload["allowed_mentions"].users)


if __name__ == "__main__":
    unittest.main()
