from copy import deepcopy
import pickle
import time
import unicodedata
import unittest

from moderation_rules import (
    BAD_ROOTS,
    MAX_FILLER_GAP,
    active_bad_roots,
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

    def test_required_obfuscations(self):
        for value in (
            "병신", "병12121신", "병aadasda신", "병  신", "병    신",
            "병ㄱㄱㄱ신", "병-_-신", "병\u200b신",
            "병\u00a0\u2009\u3000신", "병Ａａ１２신", "병…—·신",
        ):
            with self.subTest(value=value):
                self.assertEqual(find_badroot(value), "병신")

    def test_bounded_filler_and_disallowed_categories(self):
        self.assertEqual(find_badroot("씨--발"), "씨발")
        for filler in ("-", "a", "1", "ㄱ", " "):
            with self.subTest(filler=filler):
                self.assertEqual(find_badroot("병" + filler * MAX_FILLER_GAP + "신"), "병신")
                self.assertIsNone(find_badroot("병" + filler * (MAX_FILLER_GAP + 1) + "신"))
        for value in ("병원신", "병é신", "병🙂신"):
            with self.subTest(value=value):
                self.assertIsNone(find_badroot(value))

    def test_normal_prose_and_clean_links_are_not_subsequences(self):
        for value in (
            "병원에 가서 신문을 읽었다", "변신", "일반적인 한국어 문장",
            "일반적인 영어 문장", "https://example.com",
            "Please see https://example.com for details.",
            "This is an example of ordinary English prose.",
            "Some examples explain how to write short and natural replies.",
            "고 공 구 궁 곰 봄 송 볼 곡",  # NFD vowels are not standalone ㅗ.
        ):
            with self.subTest(value=value):
                self.assertIsNone(find_badroot(value))

    def test_latin_literal_policy_and_conservative_obfuscation(self):
        for value in ("fuck", "Fuck", "FuCk", "ＦＵＣＫ", "prefixfuckpostfix", "f-u-c-k", "f1u2c3k", "f ㄱ u ㄱ c ㄱ k"):
            with self.subTest(value=value):
                self.assertEqual(find_badroot(value).casefold(), "fuck")
        for value in ("sex", "s-e-x", "s  e  x"):
            with self.subTest(value=value):
                self.assertEqual(find_badroot(value), "sex")
        # Preserve the old literal substring policy, but do not assemble Latin
        # roots across word interiors or unrelated Latin-letter filler.
        self.assertEqual(find_badroot("Sussex"), "sex")
        self.assertIsNone(find_badroot("prefixf-u-c-kpostfix"))
        self.assertIsNone(find_badroot("fAauBbcCck"))

    def test_hangul_roots_and_text_share_nfkc_nfd_normalization(self):
        for root in ("병신", "ㅅㅂ", "ㅈㄴ", "ㅆㅂ", "ㅄ", "ㅗ"):
            for form in ("NFC", "NFD", "NFKC", "NFKD"):
                with self.subTest(root=root, form=form):
                    value = unicodedata.normalize(form, root)
                    self.assertEqual(find_badroot(value, (root,)), root)
                    self.assertEqual(find_badroot(root, (value,)), value)
        self.assertEqual(normalize_text("병신"), unicodedata.normalize("NFD", "병신"))
        self.assertEqual(find_badroot("병ᄀᄀᄀ신"), "병신")
        self.assertEqual(find_badroot(unicodedata.normalize("NFD", "병") + "\u200b" + unicodedata.normalize("NFD", "신")), "병신")
        self.assertEqual(find_badroot("ㅅ--ㅂ"), "ㅅㅂ")

    def test_policy_roots_are_preserved(self):
        original_policy = set("""
            씨발 시발 지랄 존나 섹스 병신 새끼 애미 에미 븅신 보지 한녀 느금 페미 패미 짱깨
            닥쳐 노무 정공 씹놈 씹년 십놈 십년 계집 장애 시팔 씨팔 ㅈㄴ ㄷㅊ ㅈㄹ 미친 미띤
            애비 ㅅㅂ ㅆㅂ ㅇㅁ ㄲㅈ ㅄ 닥치 씨벌 시벌 븅띤 치매 또라이 도라이 피싸개 정신병
            조선족 쪽발이 쪽빨이 쪽바리 쪽팔이 노예 아가리 ㅇㄱㄹ fuck 좆 설거지 난교 재명 재앙
            개놈 개년 sex ㅗ 아줌마 노괴 무현 엿 돌아이 ㄴㄱㅁ Fuck FUCK 자지 씹치 씹덕 걸레
            갈보 창녀 창남 꽃뱀 틀딱 맘충 한남 된장녀 김치녀 보슬아치 급식 짱개 왜구 쪽국 섬숭이
            쪽숭이 찌질 관종 호구 흑우 베충 일베
        """.split())
        self.assertEqual(BAD_ROOTS, original_policy)
        active = active_bad_roots()
        self.assertIsInstance(active, tuple)
        self.assertEqual(set(active), original_policy)
        self.assertEqual(len(active), len(original_policy))
        self.assertEqual(active, tuple(sorted(original_policy, key=lambda value: (value.casefold(), value))))
        self.assertTrue({"fuck", "Fuck", "FUCK"}.issubset(active))
        for root in active:
            with self.subTest(root=root):
                self.assertIsNotNone(find_badroot(root))

    def test_custom_rules_priority_and_repeated_character_states(self):
        self.assertEqual(find_badroot("a-a-b", ("aab",)), "aab")
        self.assertIsNone(find_badroot("a-b", ("aab",)))
        self.assertEqual(find_badroot("병-신", ("병신", "신")), "병신")
        self.assertEqual(find_badroot("aaa", ("a", "aaa")), "aaa")
        self.assertIsNone(find_badroot("anything", ()))

    def test_long_adversarial_inputs_remain_bounded(self):
        started = time.perf_counter()
        self.assertIsNone(find_badroot("f" * 100_000 + "u" * 100_000 + "c" * 100_000))
        self.assertIsNone(find_badroot("병" + "a" * 100_000 + "신"))
        # A deliberately broad ceiling catches explosive backtracking without
        # imposing a microbenchmark target on slower CI machines.
        self.assertLess(time.perf_counter() - started, 5.0)

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
        original = deepcopy(state)
        found = lookup_exemption(state, "profanity")
        self.assertIsNotNone(found)
        self.assertEqual(state, original)

    def test_consume_is_explicit_and_expires_are_not_decremented(self):
        state = {"rewards_active": {"achievement_profanity": {"type": "profanity", "count": 1}}}
        self.assertIsNotNone(consume_exemption(state, "profanity"))
        self.assertNotIn("achievement_profanity", state["rewards_active"])
        all_state = {"rewards_active": {"trial": {"expires_at": 1000}}}
        self.assertIsNotNone(consume_exemption(all_state, "profanity", now=500))
        self.assertIn("trial", all_state["rewards_active"])

    def test_timed_all_and_trial_with_legacy_count_are_never_decremented(self):
        for key, entry in (
            ("trial", {"count": 1, "expires_at": 1000}),
            ("achievement_all", {"count": 0, "expires_at": 1000}),
            ("3", {"type": "all", "count": 2, "expires_at": 1000}),
            ("legacy_trial", {"type": "trial", "count": 1, "expires_at": 1000}),
        ):
            with self.subTest(key=key):
                state = {"rewards_active": {key: deepcopy(entry)}}
                for _ in range(3):
                    self.assertEqual(consume_exemption(state, "profanity", now=500).kind, "all")
                    self.assertEqual(state["rewards_active"][key], entry)
                self.assertIsNone(consume_exemption(state, "profanity", now=1000))

    def test_timed_reward_precedes_counted_pass_and_expired_reward_falls_back(self):
        for key, entry in (
            ("trial", {"count": 1, "expires_at": 1000}),
            ("3", {"type": "all", "count": 1, "expires_at": 1000}),
        ):
            with self.subTest(key=key):
                state = {"rewards_active": {
                    "achievement_profanity": {"count": 2},
                    key: entry,
                }}
                self.assertEqual(consume_exemption(state, "profanity", now=999).key, key)
                self.assertEqual(state["rewards_active"]["achievement_profanity"]["count"], 2)
                self.assertEqual(consume_exemption(state, "profanity", now=1000).key, "achievement_profanity")
                self.assertEqual(state["rewards_active"]["achievement_profanity"]["count"], 1)

    def test_xp_and_legacy_achievement_counts_survive_in_memory_pickle_roundtrip(self):
        for key, entry in (
            ("1", {"type": "profanity", "count": 10}),
            ("achievement_profanity", {"count": 2}),
        ):
            with self.subTest(key=key):
                state = {"xp": 180, "claimed": [1], "rewards_active": {key: entry}}
                restored = pickle.loads(pickle.dumps(state))
                self.assertEqual(consume_exemption(restored, "profanity").key, key)
                self.assertEqual(restored["rewards_active"][key]["count"], entry["count"] - 1)
                self.assertEqual(restored["xp"], 180)
                self.assertEqual(restored["claimed"], [1])

    def test_empty_or_exhausted_rewards_do_not_grant_exemption(self):
        for state in ({}, {"rewards_active": {}}, {"rewards_active": {"1": {"type": "profanity", "count": 0}}}):
            with self.subTest(state=state):
                original = deepcopy(state)
                self.assertIsNone(consume_exemption(state, "profanity"))
                self.assertEqual(state, original)


if __name__ == "__main__":
    unittest.main()
