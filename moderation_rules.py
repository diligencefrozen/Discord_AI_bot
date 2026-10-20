"""Pure moderation rules shared by the bot and offline regression tests.

This module deliberately has no Discord or persistence dependency.  The bot can
adapt its existing pickle-backed state to the small mapping accepted by the
exemption helpers, while tests can exercise the security-sensitive decisions
without importing ``main.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
import unicodedata
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence


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

# Zero-width formatting characters are removed before matching.  The filler
# set is intentionally small and a maximum gap is enforced; arbitrary junk
# between every character caused the old pattern to over-match normal prose.
ZERO_WIDTH_CHARS = frozenset("\u00ad\u034f\u061c\u180e\u200b\u200c\u200d\u2060\ufeff")
_GAP_CHARS = r"\s\u2000-\u200a\u2028\u2029.,!?~:;/'\"`_+=|\\-"
_MAX_GAP = 2


def normalize_text(text: str) -> str:
    """Normalize Unicode and remove invisible formatting characters."""

    normalized = unicodedata.normalize("NFKC", str(text))
    return "".join(ch for ch in normalized if ch not in ZERO_WIDTH_CHARS)


def _pattern_for(root: str) -> re.Pattern[str]:
    # Permit at most two ordinary separators between root characters.  This
    # catches common spacing/punctuation obfuscation while bounding false hits.
    gap = f"[{_GAP_CHARS}]{{0,{_MAX_GAP}}}"
    return re.compile(gap.join(re.escape(ch) for ch in root), re.IGNORECASE)


_PATTERNS = tuple((root, _pattern_for(root)) for root in sorted(BAD_ROOTS, key=lambda x: (-len(x), x)))


def find_badroot(text: str, roots: Iterable[str] = BAD_ROOTS) -> Optional[str]:
    """Return the configured root found in *text*, or ``None``.

    Matching runs against NFKC text with zero-width characters removed.  A
    caller may pass a configured subset (for example, a guild-managed list).
    """

    normalized = normalize_text(text)
    patterns = _PATTERNS if roots is BAD_ROOTS else tuple(
        (root, _pattern_for(root))
        for root in sorted(set(roots), key=lambda x: (-len(x), x))
        if root
    )
    for root, pattern in patterns:
        if pattern.search(normalized):
            return root
    return None


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
        if not isinstance(entry, Mapping) or not _active(entry, now):
            continue
        # Achievement counters in the legacy pickle are keyed by name and do
        # not carry a ``type`` field (e.g. ``achievement_profanity``).
        effect_kind = entry.get("type") or (
            kind if str(key) == f"achievement_{kind}" else None
        )
        if effect_kind == "all" or effect_kind == kind:
            if effect_kind == "all" or int(entry.get("count", 0)) > 0:
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
    if isinstance(entry, MutableMapping) and "count" in entry:
        entry["count"] = int(entry.get("count", 0)) - 1
        if entry["count"] <= 0:
            user_state["rewards_active"].pop(found.key, None)
    return found
