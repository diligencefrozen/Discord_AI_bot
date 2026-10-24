"""Pure moderation rules shared by the bot and offline regression tests.

This module deliberately has no Discord or persistence dependency.  The bot can
adapt its existing pickle-backed state to the small mapping accepted by the
exemption helpers, while tests can exercise the security-sensitive decisions
without importing ``main.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import string
import time
import unicodedata
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence


# One link policy is shared by new messages, edits, scans and AI prompts.
# Preserve the historical HTTP(S) and explicit bare-domain rules verbatim;
# additionally recognise common scheme-less web addresses and Discord invites.
# Do not treat arbitrary dotted words, filenames or email addresses as links.
# Host labels are bounded and separated by literal dots, avoiding ambiguous
# nested repetitions. This checks syntax only and performs no DNS/network I/O.
LINK_REGEX = re.compile(
    r"https?://\S+"
    r"|youtu\.be|youtube\.com|gall\.dcinside\.com|m\.dcinside\.com|news\.(naver|v\.daum)\.com"
    r"|(?<![\w@.-])www\."
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?![\w@-])"
    r"|(?<![\w@.-])(?:discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+(?![\w@-])",
    re.I,
)


# Keep this list in sync with the original bot policy.  Adding normalization or
# matching behavior must not silently change the configured policy vocabulary.
BAD_ROOTS = frozenset(
    {
        "씨발", "시발", "지랄", "존나", "섹스", "병신", "새끼", "애미", "에미", "븅신", "보지",
        "한녀", "느금", "페미", "패미", "짱깨", "닥쳐", "노무", "정공", "씹놈", "씹년", "십놈",
        "십년", "계집", "장애", "시팔", "씨팔", "ㅈㄴ", "ㄷㅊ", "ㅈㄹ", "미친", "미띤", "애비",
        "ㅅㅂ", "ㅆㅂ", "ㅇㅁ", "ㄲㅈ", "ㅄ", "닥치", "씨벌", "시벌", "븅띤", "치매", "또라이",
        "도라이", "피싸개", "정신병", "조선족", "쪽발이", "쪽빨이", "쪽바리", "쪽팔이", "노예",
        "아가리", "ㅇㄱㄹ", "fuck", "좆", "설거지", "난교", "재명", "재앙", "개놈", "개년",
        "sex", "ㅗ", "아줌마", "노괴", "무현", "엿", "돌아이", "ㄴㄱㅁ", "Fuck", "FUCK",
        "자지", "씹치", "씹덕", "걸레", "갈보", "창녀", "창남", "꽃뱀", "틀딱", "맘충", "한남",
        "된장녀", "김치녀", "보슬아치", "급식", "짱개", "왜구", "쪽국", "섬숭이", "쪽숭이", "찌질",
        "관종", "호구", "흑우", "베충", "일베",
    }
)

# Prefix commands whose free-form argument is sent to an AI provider.  These
# are checked explicitly because registered prefix commands are dispatched
# before the normal message moderation path in ``main.py``.
AI_PROMPT_COMMANDS = frozenset(("ask", "img"))

# Exact source strings are the policy entries: case variants remain distinct
# for display/counting even though they share a case-insensitive matcher.
# This is the only active list; a read-only admin view needs no separate state.
def active_bad_roots() -> tuple[str, ...]:
    """Return every distinct original policy entry in stable display order."""

    return tuple(sorted(BAD_ROOTS, key=lambda root: (root.casefold(), root)))


# Zero-width formatting characters are removed before matching. Korean roots
# also tolerate inserted ASCII letters, numbers, jamo, whitespace, punctuation,
# symbols, marks and format characters. Complete syllables and other letters
# remain boundaries so unrelated words in ordinary prose are not joined.
# Only the conservative Latin matcher retains the historical 16-character gap
# limit. Korean scanning is linear with bounded state even for long insertions.
ZERO_WIDTH_CHARS = frozenset("\u00ad\u034f\u061c\u180e\u200b\u200c\u200d\u2060\ufeff")
MAX_FILLER_GAP = 16
_ASCII_PUNCTUATION = frozenset(string.punctuation)


def normalize_text(text: str) -> str:
    """Use the same compatibility/canonical normalization for roots and text."""

    normalized = unicodedata.normalize("NFKC", str(text))
    visible = "".join(ch for ch in normalized if ch not in ZERO_WIDTH_CHARS)
    return unicodedata.normalize("NFD", visible)


def _matching_atoms(text: str) -> str:
    # Canonical Hangul syllables form one matching atom.  Without this grouping,
    # NFD's vowel in an ordinary syllable such as '고' would match the single
    # configured jamo root 'ㅗ', and syllable parts could become gap filler.
    return unicodedata.normalize("NFC", normalize_text(text)).casefold()


# NFKC maps compatibility ㅄ to one PIEUP-SIOS atom (ᄡ), rather than the
# separate keystrokes ᄇᄉ. Treat these isolated jamo as equivalent for matching
# only, after NFC has protected complete syllables such as 값 and 밥상.
# The scanner retains the unexpanded atom's position for prose boundaries;
# Latin gap accounting also continues to use the original atoms.
_JAMO_MATCHING_ALIASES = {
    "\u1121": "\u1107\u1109",  # isolated initial/compatibility PIEUP-SIOS
    "\u11b9": "\u1107\u1109",  # isolated final PIEUP-SIOS
    "\u11b8": "\u1107",        # isolated final PIEUP
    "\u11ba": "\u1109",        # isolated final SIOS
}


def _is_filler(ch: str) -> bool:
    """Historical Latin filler policy; keep its prose and URL safeguards."""

    number = ord(ch)
    return (
        "a" <= ch <= "z"
        or "0" <= ch <= "9"
        or ch.isspace()
        or ch in _ASCII_PUNCTUATION
        or unicodedata.category(ch).startswith("P")
        or 0x1100 <= number <= 0x11FF
        or 0x3130 <= number <= 0x318F
        or 0xA960 <= number <= 0xA97F
        or 0xD7B0 <= number <= 0xD7FF
    )


def _is_korean_filler(ch: str) -> bool:
    # Do not use a blanket non-Hangul rule: accented/CJK letters and complete
    # syllables are prose boundaries. Emoji can include variation/combining
    # marks and invisible formatting, all of which are valid inserted filler.
    category = unicodedata.category(ch)
    return _is_filler(ch) or category[0] in "NSM" or category == "Cf"


@dataclass(frozen=True)
class _RuleNode:
    parent: int
    output: Optional[int] = None


@dataclass(frozen=True)
class _CompiledRules:
    roots: tuple[str, ...]
    nodes: tuple[_RuleNode, ...]
    transitions: Mapping[str, tuple[int, ...]]
    latin_roots: tuple[tuple[int, str], ...]


@lru_cache(maxsize=32)
def _compile_rules(roots: tuple[str, ...]) -> _CompiledRules:
    """Build a prefix trie, sharing normalized equivalents internally only."""

    prefixes: dict[str, int] = {}
    nodes: list[_RuleNode] = []
    transitions: dict[str, list[int]] = {}
    original_roots: list[str] = []
    normalized_roots: set[str] = set()
    latin_roots: list[tuple[int, str]] = []
    for root in roots:
        normalized = "".join(_JAMO_MATCHING_ALIASES.get(ch, ch) for ch in _matching_atoms(root))
        if not normalized or normalized in normalized_roots:
            continue
        normalized_roots.add(normalized)
        output = len(original_roots)
        original_roots.append(root)
        if normalized.isascii() and normalized.isalpha():
            latin_roots.append((output, normalized))
            continue
        parent = -1
        for length in range(1, len(normalized) + 1):
            prefix = normalized[:length]
            node_id = prefixes.get(prefix)
            if node_id is None:
                node_id = len(nodes)
                prefixes[prefix] = node_id
                nodes.append(_RuleNode(parent))
                transitions.setdefault(prefix[-1], []).append(node_id)
            if length == len(normalized):
                nodes[node_id] = _RuleNode(nodes[node_id].parent, output)
            parent = node_id
    # Children run first so one character cannot advance several positions of
    # a repeated-letter root.  Parent nodes are always created before children.
    return _CompiledRules(
        tuple(original_roots), tuple(nodes),
        {ch: tuple(sorted(ids, reverse=True)) for ch, ids in transitions.items()},
        tuple(latin_roots),
    )


_DEFAULT_RULES = _compile_rules(tuple(sorted(BAD_ROOTS, key=lambda root: (-len(root), root))))


def _find_obfuscated_latin(text: str, root: str) -> bool:
    """Match separated Latin roots without joining ordinary prose or URLs.

    Literal substrings retain the existing policy separately.  Obfuscated
    English forms need word boundaries and may use explicit filler other than
    Latin letters.  Otherwise e.g. the 's://ex' in 'https://example.com' would
    become 'sex', or ordinary English words could form unrelated subsequences.
    """

    latest = [-1] * len(root)
    last_non_filler = -1
    for position, ch in enumerate(text):
        for offset in range(len(root) - 1, -1, -1):
            if ch != root[offset]:
                continue
            if offset == 0:
                previous = text[position - 1] if position else ""
                if previous and (previous.isalnum() or previous == "_"):
                    continue
            else:
                previous_position = latest[offset - 1]
                if (
                    previous_position < 0
                    or previous_position < last_non_filler
                    or position - previous_position > MAX_FILLER_GAP + 1
                ):
                    continue
            latest[offset] = position
            if offset == len(root) - 1:
                following = text[position + 1] if position + 1 < len(text) else ""
                if not following or not (following.isalnum() or following == "_"):
                    return True
        if "a" <= ch <= "z" or not _is_filler(ch):
            last_non_filler = position
    return False


def find_badroot(text: str, roots: Iterable[str] = BAD_ROOTS) -> Optional[str]:
    """Return the configured root found in *text*, or ``None``.

    A deterministic prefix-state scan skips inserted filler between Korean
    root characters without a gap-length bypass. Latin roots retain their
    conservative boundary and ``MAX_FILLER_GAP`` policy. No regex backtracking
    is involved: input atoms visit only their trie transitions, with bounded
    state independent of message length. Original policy priority is retained
    (longest root first, then original string), including custom sets.
    """

    rules = _DEFAULT_RULES if roots is BAD_ROOTS else _compile_rules(
        tuple(sorted({root for root in roots if root}, key=lambda root: (-len(root), root)))
    )
    latest = [-1] * len(rules.nodes)
    last_non_filler = -1
    best: Optional[int] = None
    normalized = _matching_atoms(text)
    for output, latin_root in rules.latin_roots:
        if best is not None and output >= best:
            continue
        if latin_root in normalized or _find_obfuscated_latin(normalized, latin_root):
            best = output
            if best == 0:
                return rules.roots[0]
    for position, ch in enumerate(normalized):
        for atom in _JAMO_MATCHING_ALIASES.get(ch, ch):
            for node_id in rules.transitions.get(atom, ()):
                node = rules.nodes[node_id]
                if node.parent >= 0:
                    previous = latest[node.parent]
                    if previous < 0 or previous < last_non_filler:
                        continue
                latest[node_id] = position
                if node.output is not None and (best is None or node.output < best):
                    best = node.output
                    if best == 0:
                        return rules.roots[0]
        if not _is_korean_filler(ch):
            last_non_filler = position
    return rules.roots[best] if best is not None else None


def parse_command(content: str, prefixes: Sequence[str] = ("!", "/")) -> Optional[str]:
    """Parse a command name only when content starts with a configured prefix."""

    value = str(content).lstrip()
    for prefix in prefixes:
        if value.startswith(prefix):
            match = re.match(r"[!/][\w가-힣-]+", value)
            return match.group(0)[1:].casefold() if match else None
    return None


def is_ai_prompt_command(
    content: str,
    commands: Iterable[str] = AI_PROMPT_COMMANDS,
    prefixes: Sequence[str] = ("!", "/"),
) -> bool:
    """Return whether *content* invokes a free-form AI prompt command.

    The command name is parsed independently from its prompt so callers can
    apply the same moderation policy to both ``!`` and slash-style prefixes.
    The default set intentionally contains only the two existing AI commands;
    other historical commands keep their prior dispatch behavior.
    """

    command = parse_command(content, prefixes)
    if command is None:
        return False
    known = {str(item).casefold().lstrip("!/") for item in commands}
    return command in known


def extract_command_argument(
    content: str,
    commands: Iterable[str] = AI_PROMPT_COMMANDS,
    prefixes: Sequence[str] = ("!", "/"),
) -> Optional[str]:
    """Return the free-form argument of an AI prompt command.

    The parser deliberately returns ``None`` for non-AI commands and for an
    empty argument.  Keeping this helper pure lets the gateway handler and
    offline tests apply identical moderation rules before dispatching a
    prompt to an external provider.
    """

    value = str(content).lstrip()
    command = parse_command(value, prefixes)
    if command is None or command not in {
        str(item).casefold().lstrip("!/") for item in commands
    }:
        return None
    match = re.match(r"[!/][\w가-힣-]+(?:\s+(.*))?$", value, re.DOTALL)
    if not match:
        return None
    argument = (match.group(1) or "").strip()
    return argument or None


def should_moderate_message(
    content: str,
    known_commands: Optional[Iterable[str]] = None,
    prefixes: Sequence[str] = ("!", "/"),
) -> bool:
    """Return whether a message must enter moderation before dispatch.

    Command metadata is retained for API compatibility, but recognized
    commands no longer bypass moderation.  Their complete content, including
    arguments, must be checked before ``process_commands`` is called.
    """

    return True


@dataclass(frozen=True)
class Exemption:
    """An available exemption, without consuming it."""

    source: str
    key: str
    kind: str


def _active(entry: Mapping[str, Any], now: float) -> bool:
    expires_at = entry.get("expires_at")
    return expires_at is None or float(expires_at) > now


def lookup_exemption(
    user_state: Mapping[str, Any], kind: str, *, now: Optional[float] = None
) -> Optional[Exemption]:
    """Look up an exemption without mutating state or writing persistence."""

    now = time.time() if now is None else now
    rewards = user_state.get("rewards_active", {}) or {}
    # Time-limited all-purpose rewards take precedence over counted passes.
    for key in ("trial", "achievement_all"):
        entry = rewards.get(key)
        if isinstance(entry, Mapping) and _active(entry, now):
            return Exemption("reward", key, "all")
    for key, entry in rewards.items():
        if (
            isinstance(entry, Mapping)
            and _active(entry, now)
            and entry.get("type") in ("all", "trial")
        ):
            return Exemption("reward", str(key), "all")
    for key, entry in rewards.items():
        if not isinstance(entry, Mapping) or not _active(entry, now):
            continue
        # Achievement counters in the legacy pickle are keyed by name and do
        # not carry a ``type`` field (e.g. ``achievement_profanity``).
        effect_kind = entry.get("type") or (
            kind if str(key) == f"achievement_{kind}" else None
        )
        if effect_kind == kind and int(entry.get("count", 0)) > 0:
            return Exemption("reward", str(key), str(effect_kind))
    return None


def consume_exemption(
    user_state: MutableMapping[str, Any], kind: str, *, now: Optional[float] = None
) -> Optional[Exemption]:
    """Consume one counted exemption after an explicit moderation decision.

    Lookup is intentionally separate so callers can display an explanation,
    delete a message, and persist exactly once after deciding to consume.
    Time-limited exemptions are returned but never decremented.
    """

    found = lookup_exemption(user_state, kind, now=now)
    if found is None:
        return None
    entry = user_state.setdefault("rewards_active", {}).get(found.key)
    if found.kind != "all" and isinstance(entry, MutableMapping) and "count" in entry:
        entry["count"] = int(entry.get("count", 0)) - 1
        if entry["count"] <= 0:
            user_state["rewards_active"].pop(found.key, None)
    return found
