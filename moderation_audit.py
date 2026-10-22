"""Bounded, memory-only copies for the configured deletion record channel.

Capture a message before deleting or editing it.  This module never downloads,
logs, serializes, or sends message content.  Its frozen records deliberately
contain no references to Discord objects; a sender must suppress mentions and
verify that destination readers can also read the source channel.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import islice
import math
from pathlib import PurePosixPath
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


DELETE_ORIGINS = frozenset(("bot", "external", "unknown"))
_CUSTOM_EMOJI = re.compile(r"<(a?):([A-Za-z0-9_]{1,32}):([0-9]{1,20})>")


@dataclass(frozen=True)
class SnapshotLimits:
    max_content_chars: int = 12_000
    max_name_chars: int = 256
    max_url_chars: int = 2_048
    max_attachments: int = 10
    max_stickers: int = 3
    max_emojis: int = 64
    max_embeds: int = 10
    max_reason_code_chars: int = 64
    max_detected_root_chars: int = 128
    max_reason_detail_chars: int = 512
    max_total_chars: int = 65_536

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_total_chars < self.reason_reservation:
            raise ValueError("max_total_chars must cover reserved reason metadata")

    @property
    def reason_reservation(self) -> int:
        return self.max_reason_code_chars + self.max_detected_root_chars + self.max_reason_detail_chars


@dataclass(frozen=True, repr=False)
class AuditReason:
    code: str = ""
    detected_root: str = ""
    detail: str = ""

    @property
    def matched_rule(self) -> str:
        return self.detected_root


@dataclass(frozen=True, repr=False)
class AttachmentSnapshot:
    id: int | None
    filename: str
    content_type: str
    size: int | None
    width: int | None
    height: int | None
    spoiler: bool
    url: str
    proxy_url: str
    kind: str


@dataclass(frozen=True, repr=False)
class StickerSnapshot:
    id: int | None
    name: str
    format: str
    url: str


@dataclass(frozen=True, repr=False)
class EmojiSnapshot:
    kind: str
    text: str
    name: str = ""
    id: int | None = None
    animated: bool = False
    url: str = ""


@dataclass(frozen=True, repr=False)
class EmbedSnapshot:
    kind: str
    title: str


@dataclass(frozen=True, repr=False)
class MessageAuditSnapshot:
    message_id: int
    guild_id: int
    guild_name: str
    channel_id: int
    channel_name: str
    author_id: int
    author_name: str
    content: str
    content_available: bool
    original_length: int | None
    created_at: datetime | None
    captured_at: datetime
    reason: AuditReason
    delete_origin: str
    emojis: tuple[EmojiSnapshot, ...]
    attachments: tuple[AttachmentSnapshot, ...]
    stickers: tuple[StickerSnapshot, ...]
    embeds: tuple[EmbedSnapshot, ...]
    attachment_count: int | None
    sticker_count: int | None
    embed_count: int | None
    attachments_available: bool
    stickers_available: bool
    embeds_available: bool
    truncated_fields: tuple[str, ...] = ()
    # Retain the capture limits for bounded immutable reason updates.
    _limits: SnapshotLimits = field(default_factory=SnapshotLimits, compare=False)
    trigger: str = "message_new"
    edited_at: datetime | None = None

    @property
    def reason_code(self) -> str:
        return self.reason.code

    @property
    def detected_root(self) -> str:
        return self.reason.detected_root

    @property
    def matched_rule(self) -> str:
        return self.reason.detected_root

    def __repr__(self) -> str:
        # Even accidentally logging the record must not disclose content or URLs.
        return (f"MessageAuditSnapshot(message_id={self.message_id}, "
                f"guild_id={self.guild_id}, channel_id={self.channel_id}, "
                f"author_id={self.author_id})")


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if number >= 0 else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _Budget:
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        self.truncated: set[str] = set()

    def text(self, value: Any, limit: int, path: str) -> str:
        original = "" if value is None else str(value)
        permitted = min(self.remaining, limit)
        result = original[:permitted]
        if len(result) != len(original):
            self.truncated.add(path)
        self.remaining -= len(result)
        return result

    def url(self, value: Any, limit: int, path: str) -> str:
        candidate = self.text(value, limit, path)
        try:
            parts = urlsplit(candidate)
            if parts.scheme in {"https", "http"} and parts.hostname and not parts.username and not parts.password:
                return candidate
        except ValueError:
            pass
        return ""


def _collection(value: Any, limit: int) -> tuple[tuple[Any, ...], int | None, bool]:
    """Read at most limit+1 objects, even for an unbounded iterable."""
    if value is None:
        return (), None, False
    try:
        iterator = iter(value)
    except TypeError:
        return (), None, False
    if isinstance(value, (str, bytes, Mapping)):
        return (), None, False
    try:
        count = len(value)
    except (TypeError, OverflowError):
        count = None
    captured = tuple(islice(iterator, limit + 1))
    truncated = len(captured) > limit or (count is not None and count > limit)
    if count is None and not truncated:
        count = len(captured)
    return captured[:limit], count, truncated


def attachment_kind(content_type: str | None, filename: str) -> str:
    """Classify MIME first; use a filename suffix only for missing/generic MIME."""
    mime = (content_type or "").split(";", 1)[0].strip().casefold()
    if mime == "image/gif":
        return "gif"
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("audio/", "audio"), ("text/", "document")):
        if mime.startswith(prefix):
            return kind
    if mime in {"application/zip", "application/x-zip-compressed", "application/x-rar-compressed", "application/vnd.rar", "application/x-7z-compressed", "application/gzip", "application/x-tar", "application/x-bzip2"}:
        return "archive"
    if mime in {"application/pdf", "application/json", "application/xml", "application/rtf", "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"} or mime.startswith("application/vnd.openxmlformats-officedocument.") or mime.startswith("application/vnd.oasis.opendocument."):
        return "document"
    if mime and mime != "application/octet-stream":
        return "other"
    suffix = PurePosixPath(filename.casefold()).suffix
    groups = {
        "gif": {".gif"},
        "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".svg", ".avif", ".heic", ".tif", ".tiff"},
        "video": {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"},
        "audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"},
        "document": {".pdf", ".txt", ".md", ".csv", ".tsv", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".json", ".xml", ".rtf", ".odt"},
        "archive": {".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz", ".tgz"},
    }
    return next((kind for kind, suffixes in groups.items() if suffix in suffixes), "other")


def safe_audit_text(value: str) -> str:
    """Neutralize all @ mentions in a display copy, preserving stored originals.

    Senders should additionally pass Discord's AllowedMentions.none().  This
    helper is dependency-free and protects filenames and titles as well as text.
    """
    return str(value).replace("@", "@\u200b")


def _emoji_base(character: str) -> bool:
    number = ord(character)
    return (0x1F000 <= number <= 0x1FAFF or 0x2600 <= number <= 0x27BF
            or number in {0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139, 0x231A, 0x231B, 0x2328, 0x23CF, 0x24C2, 0x2B50, 0x2B55, 0x3030, 0x303D, 0x3297, 0x3299}
            or 0x2194 <= number <= 0x2199 or 0x21A9 <= number <= 0x21AA
            or 0x23E9 <= number <= 0x23F3 or 0x23F8 <= number <= 0x23FA
            or 0x25AA <= number <= 0x25AB or number in {0x25B6, 0x25C0}
            or 0x25FB <= number <= 0x25FE or 0x2934 <= number <= 0x2935
            or 0x2B05 <= number <= 0x2B07 or 0x2B1B <= number <= 0x2B1C)


def _emoji_tokens(content: str):
    """Recognize common Unicode emoji sequences and Discord custom emoji tokens."""
    position = 0
    while position < len(content):
        custom = _CUSTOM_EMOJI.match(content, position)
        if custom:
            yield ("custom", custom.group(0), custom.group(2), int(custom.group(3)), bool(custom.group(1)))
            position = custom.end()
            continue
        start = position
        character = content[position]
        if character in "0123456789#*":
            end = position + 1
            if end < len(content) and content[end] == "\ufe0f":
                end += 1
            if end < len(content) and content[end] == "\u20e3":
                yield ("unicode", content[start:end + 1], "", None, False)
                position = end + 1
                continue
        if not _emoji_base(character):
            position += 1
            continue
        position += 1
        if 0x1F1E6 <= ord(character) <= 0x1F1FF and position < len(content) and 0x1F1E6 <= ord(content[position]) <= 0x1F1FF:
            position += 1
        while position < len(content):
            number = ord(content[position])
            if number in {0xFE0E, 0xFE0F} or 0x1F3FB <= number <= 0x1F3FF or 0xE0020 <= number <= 0xE007F:
                position += 1
            elif content[position] == "\u200d" and position + 1 < len(content) and _emoji_base(content[position + 1]):
                position += 2
            else:
                break
        yield ("unicode", content[start:position], "", None, False)


def _reason(code: Any, root: Any, detail: Any, limits: SnapshotLimits) -> tuple[AuditReason, set[str]]:
    budget = _Budget(limits.reason_reservation)
    result = AuditReason(
        budget.text(code, limits.max_reason_code_chars, "reason.code"),
        budget.text(root, limits.max_detected_root_chars, "reason.detected_root"),
        budget.text(detail, limits.max_reason_detail_chars, "reason.detail"),
    )
    return result, budget.truncated


def capture_message_snapshot(message: Any, *, reason_code: str = "", detected_root: str = "", reason_detail: str = "", delete_origin: str = "unknown", captured_at: datetime | None = None, limits: SnapshotLimits | None = None, trigger: str = "message_new", edited_at: datetime | None = None) -> MessageAuditSnapshot:
    """Detach bounded original content and metadata from a Discord-like message."""
    if delete_origin not in DELETE_ORIGINS:
        raise ValueError("delete_origin must be bot, external, or unknown")
    limits = limits or SnapshotLimits()
    budget = _Budget(limits.max_total_chars - limits.reason_reservation)
    reason, truncated = _reason(reason_code, detected_root, reason_detail, limits)
    raw_content = _get(message, "content")
    content_available = isinstance(raw_content, str)
    content = budget.text(raw_content if content_available else "", limits.max_content_chars, "content")
    guild, channel, author = (_get(message, name) for name in ("guild", "channel", "author"))
    names = [budget.text(_get(value, "display_name", _get(value, "name", "")), limits.max_name_chars, f"{name}_name")
             for name, value in (("guild", guild), ("channel", channel), ("author", author))]

    raw_attachments = _get(message, "attachments")
    items, attachment_count, clipped = _collection(raw_attachments, limits.max_attachments)
    if clipped:
        budget.truncated.add("attachments")
    attachments = []
    for index, item in enumerate(items):
        path = f"attachments[{index}]"
        filename = budget.text(_get(item, "filename", ""), limits.max_name_chars, f"{path}.filename")
        mime = budget.text(_get(item, "content_type", ""), 128, f"{path}.content_type")
        try:
            spoiler = _get(item, "is_spoiler")
            if callable(spoiler):
                spoiler = spoiler()
        except Exception:
            # An optional/version-specific helper must not discard the original
            # text and otherwise usable attachment evidence.
            spoiler = None
        if spoiler is None:
            try:
                spoiler = _get(item, "spoiler")
            except Exception:
                spoiler = None
            if spoiler is None:
                spoiler = filename.startswith("SPOILER_")
        attachments.append(AttachmentSnapshot(
            _number(_get(item, "id")), filename, mime, _number(_get(item, "size")),
            _number(_get(item, "width")), _number(_get(item, "height")), bool(spoiler),
            budget.url(_get(item, "url", ""), limits.max_url_chars, f"{path}.url"),
            budget.url(_get(item, "proxy_url", ""), limits.max_url_chars, f"{path}.proxy_url"),
            attachment_kind(mime, filename),
        ))

    raw_stickers = _get(message, "stickers")
    if not raw_stickers:
        raw_stickers = _get(message, "sticker_items", raw_stickers)
    items, sticker_count, clipped = _collection(raw_stickers, limits.max_stickers)
    if clipped:
        budget.truncated.add("stickers")
    stickers = []
    for index, item in enumerate(items):
        path = f"stickers[{index}]"
        sticker_format = _get(item, "format", _get(item, "format_type", ""))
        stickers.append(StickerSnapshot(
            _number(_get(item, "id")),
            budget.text(_get(item, "name", ""), limits.max_name_chars, f"{path}.name"),
            budget.text(_get(sticker_format, "name", sticker_format), 32, f"{path}.format"),
            budget.url(_get(item, "url", ""), limits.max_url_chars, f"{path}.url"),
        ))

    raw_embeds = _get(message, "embeds")
    items, embed_count, clipped = _collection(raw_embeds, limits.max_embeds)
    if clipped:
        budget.truncated.add("embeds")
    embeds = tuple(EmbedSnapshot(
        budget.text(_get(item, "type", ""), 32, f"embeds[{index}].kind"),
        budget.text(_get(item, "title", ""), limits.max_name_chars, f"embeds[{index}].title"),
    ) for index, item in enumerate(items))

    emojis = []
    seen: set[tuple[str, str]] = set()
    for kind, text, name, emoji_id, animated in _emoji_tokens(content):
        key = (kind, text)
        if key in seen:
            continue
        seen.add(key)
        if len(emojis) >= limits.max_emojis:
            budget.truncated.add("emojis")
            break
        index = len(emojis)
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}" if emoji_id is not None else ""
        emojis.append(EmojiSnapshot(
            kind, budget.text(text, 128, f"emojis[{index}].text"),
            budget.text(name, 32, f"emojis[{index}].name"), emoji_id, animated,
            budget.url(url, limits.max_url_chars, f"emojis[{index}].url"),
        ))
    truncated.update(budget.truncated)
    return MessageAuditSnapshot(
        _number(_get(message, "id")) or 0,
        _number(_get(guild, "id", _get(message, "guild_id"))) or 0, names[0],
        _number(_get(channel, "id", _get(message, "channel_id"))) or 0, names[1],
        _number(_get(author, "id", _get(message, "author_id"))) or 0, names[2],
        content, content_available, len(raw_content) if content_available else None,
        _timestamp(_get(message, "created_at")), _timestamp(captured_at) or datetime.now(timezone.utc),
        reason, delete_origin, tuple(emojis), tuple(attachments), tuple(stickers), embeds,
        attachment_count, sticker_count, embed_count,
        raw_attachments is not None, raw_stickers is not None, raw_embeds is not None,
        tuple(sorted(truncated)), limits,
        trigger if trigger in {"message_new", "message_edit", "history_scan"} else "message_new",
        _timestamp(edited_at) or _timestamp(_get(message, "edited_at")),
    )


class MessageAuditCache:
    """Bounded LRU cache with absolute monotonic TTL, held only in process RAM.

    Reading or annotating a record never extends its retention period.  The
    default cache retains at most 256 snapshots for at most ten minutes.
    """

    def __init__(self, *, max_entries: int = 256, ttl_seconds: float = 600, clock: Callable[[], float] = time.monotonic, limits: SnapshotLimits | None = None) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.limits = limits or SnapshotLimits()
        self._entries: OrderedDict[tuple[int, int], tuple[float, MessageAuditSnapshot]] = OrderedDict()

    def _expire(self) -> None:
        now = self.clock()
        for key, (expires, _) in tuple(self._entries.items()):
            if expires <= now:
                del self._entries[key]

    def remember(self, snapshot: MessageAuditSnapshot) -> MessageAuditSnapshot:
        if not isinstance(snapshot, MessageAuditSnapshot):
            raise TypeError("remember requires a MessageAuditSnapshot")
        self._expire()
        key = (snapshot.guild_id, snapshot.message_id)
        self._entries[key] = (self.clock() + self.ttl_seconds, snapshot)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return snapshot

    def capture(self, message: Any, **kwargs: Any) -> MessageAuditSnapshot:
        kwargs.setdefault("limits", self.limits)
        return self.remember(capture_message_snapshot(message, **kwargs))

    def get(self, guild_id: int, message_id: int) -> MessageAuditSnapshot | None:
        self._expire()
        key = (guild_id, message_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def pop(self, guild_id: int, message_id: int) -> MessageAuditSnapshot | None:
        self._expire()
        entry = self._entries.pop((guild_id, message_id), None)
        return entry[1] if entry is not None else None

    def annotate(self, guild_id: int, message_id: int, *, reason_code: str | None = None, detected_root: str | None = None, reason_detail: str | None = None, delete_origin: str | None = None, trigger: str | None = None, edited_at: datetime | None = None) -> MessageAuditSnapshot | None:
        if delete_origin is not None and delete_origin not in DELETE_ORIGINS:
            raise ValueError("delete_origin must be bot, external, or unknown")
        snapshot = self.get(guild_id, message_id)
        if snapshot is None:
            return None
        reason, truncated = _reason(
            snapshot.reason.code if reason_code is None else reason_code,
            snapshot.reason.detected_root if detected_root is None else detected_root,
            snapshot.reason.detail if reason_detail is None else reason_detail,
            snapshot._limits,
        )
        updated = replace(snapshot, reason=reason,
                          delete_origin=snapshot.delete_origin if delete_origin is None else delete_origin,
                          trigger=trigger if trigger in {"message_new", "message_edit", "history_scan"} else snapshot.trigger,
                          edited_at=_timestamp(edited_at) or snapshot.edited_at,
                          truncated_fields=tuple(sorted(set(snapshot.truncated_fields) | truncated)))
        key = (guild_id, message_id)
        expires, _ = self._entries[key]
        self._entries[key] = (expires, updated)
        return updated

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        self._expire()
        return len(self._entries)


__all__ = [
    "AttachmentSnapshot", "AuditReason", "DELETE_ORIGINS", "EmbedSnapshot",
    "EmojiSnapshot", "MessageAuditCache", "MessageAuditSnapshot", "SnapshotLimits",
    "StickerSnapshot", "attachment_kind", "capture_message_snapshot", "safe_audit_text",
]
