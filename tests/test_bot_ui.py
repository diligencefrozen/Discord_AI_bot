from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

import bot_ui


class BotUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_user = os.environ.get("SPECIAL_ADMIN_USER_ID")
        self.original_guilds = os.environ.get("SPECIAL_ADMIN_GUILD_IDS")
        os.environ.pop("SPECIAL_ADMIN_USER_ID", None)
        os.environ.pop("SPECIAL_ADMIN_GUILD_IDS", None)

    def tearDown(self) -> None:
        if self.original_user is None:
            os.environ.pop("SPECIAL_ADMIN_USER_ID", None)
        else:
            os.environ["SPECIAL_ADMIN_USER_ID"] = self.original_user
        if self.original_guilds is None:
            os.environ.pop("SPECIAL_ADMIN_GUILD_IDS", None)
        else:
            os.environ["SPECIAL_ADMIN_GUILD_IDS"] = self.original_guilds

    def test_intro_view_has_exactly_four_buttons_in_two_rows(self):
        view = bot_ui.IntroView()
        self.assertEqual([child.label for child in view.children], list(bot_ui.IntroView.BUTTON_LABELS))
        if bot_ui.DISCORD_AVAILABLE:
            self.assertIsNone(view.timeout)
            self.assertEqual([child.row for child in view.children], [0, 0, 1, 1])
            self.assertTrue(all(child.style == bot_ui.discord.ButtonStyle.secondary for child in view.children))

    def test_feature_menu_is_a_private_select_view(self):
        view = bot_ui.FeatureMenuView()
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].custom_id, "ui:feature-menu")

    def test_typing_notice_is_short_and_mentions_only_the_user(self):
        payload = bot_ui.typing_intro_payload(SimpleNamespace(mention="<@42>"))
        self.assertIn("<@42>", payload["content"])
        self.assertIn("무엇을 해볼까요?", payload["content"])
        self.assertNotIn("embed_description", payload)

    def test_owner_only_when_special_id_is_unset(self):
        guild = SimpleNamespace(id=1, owner_id=10)
        self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=10), guild))
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=855749166764654653), guild))
        self.assertEqual(
            bot_ui.admin_authorization_reason(SimpleNamespace(id=855749166764654653), guild),
            "special_id_unconfigured",
        )
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=20), guild))

    def test_special_id_requires_exact_id_and_optional_guild_scope(self):
        os.environ["SPECIAL_ADMIN_USER_ID"] = "855749166764654653"
        guild = SimpleNamespace(id=1, owner_id=10)
        self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=855749166764654653), guild))
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=855749166764654654), guild))
        os.environ["SPECIAL_ADMIN_GUILD_IDS"] = "2,3"
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=855749166764654653), guild))
        self.assertTrue(bot_ui.is_admin_authorized(SimpleNamespace(id=855749166764654653), SimpleNamespace(id=2, owner_id=10)))

    def test_invalid_special_id_is_fail_closed_and_diagnosable(self):
        os.environ["SPECIAL_ADMIN_USER_ID"] = "not-an-id"
        guild = SimpleNamespace(id=1, owner_id=10)
        actor = SimpleNamespace(id=855749166764654653)
        self.assertFalse(bot_ui.is_admin_authorized(actor, guild))
        self.assertEqual(bot_ui.admin_authorization_reason(actor, guild), "special_id_invalid")

    def test_dm_and_administrator_role_do_not_authorize(self):
        os.environ["SPECIAL_ADMIN_USER_ID"] = "855749166764654653"
        self.assertFalse(bot_ui.is_admin_authorized(SimpleNamespace(id=855749166764654653), None))
        guild = SimpleNamespace(id=1, owner_id=10)
        actor = SimpleNamespace(id=20, guild_permissions=SimpleNamespace(administrator=True), roles=["administrator"])
        self.assertFalse(bot_ui.is_admin_authorized(actor, guild))


if __name__ == "__main__":
    unittest.main()
