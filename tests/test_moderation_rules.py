import unittest

from moderation_rules import (
    BAD_ROOTS,
    consume_exemption,
    extract_command_argument,
    find_badroot,
    is_ai_prompt_command,
    lookup_exemption,
    normalize_text,
    should_moderate_message,
)


class ModerationRulesTests(unittest.TestCase):
    def test_nfkc_and_zero_width_obfuscation(self):
        self.assertEqual(normalize_text("Ｃｉ\u200bｖａａl"), "Civaal")
        self.assertEqual(find_badroot("씨\u200b발"), "씨발")

    def test_limited_separator_matching_and_no_unbounded_filler(self):
        self.assertEqual(find_badroot("씨--발"), "씨발")
        self.assertIsNone(find_badroot("씨-_-_-_-_-발"))

    def test_policy_roots_are_preserved(self):
        self.assertIn("씨발", BAD_ROOTS)
        self.assertIn("fuck", BAD_ROOTS)
        self.assertIn("ㅗ", BAD_ROOTS)

    def test_all_prefixed_text_is_moderated_before_dispatch(self):
        self.assertTrue(should_moderate_message("!ask hello", {"ask"}))
        self.assertTrue(should_moderate_message("!xp 씨발", {"xp"}))
        self.assertTrue(should_moderate_message("!unknown 씨발", {"ask"}))
        self.assertTrue(should_moderate_message("/ 씨발", {"ask"}))

    def test_ai_prompt_commands_are_parsed_for_explicit_moderation(self):
        self.assertTrue(is_ai_prompt_command("!ask hello"))
        self.assertTrue(is_ai_prompt_command("/img a sunset"))
        self.assertEqual(extract_command_argument("!ask hello world"), "hello world")
        self.assertIsNone(extract_command_argument("!ask"))
        self.assertIsNone(extract_command_argument("!xp 씨발"))

    def test_exemption_lookup_does_not_consume(self):
        state = {"rewards_active": {"achievement_profanity": {"type": "profanity", "count": 2}}}
        found = lookup_exemption(state, "profanity")
        self.assertIsNotNone(found)
        self.assertEqual(state["rewards_active"]["achievement_profanity"]["count"], 2)

    def test_consume_is_explicit_and_expires_are_not_decremented(self):
        state = {"rewards_active": {"achievement_profanity": {"type": "profanity", "count": 1}}}
        self.assertIsNotNone(consume_exemption(state, "profanity"))
        self.assertNotIn("achievement_profanity", state["rewards_active"])
        all_state = {"rewards_active": {"trial": {"expires_at": 1000}}}
        self.assertIsNotNone(consume_exemption(all_state, "profanity", now=500))
        self.assertIn("trial", all_state["rewards_active"])


if __name__ == "__main__":
    unittest.main()
