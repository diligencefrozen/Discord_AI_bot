"""Single-channel Discord deletion cards; bodies never enter application logs.

Snapshots and delivery reservations are bounded, process-local data. Deletion
enforcement does not depend on audit-channel availability or delivery success.
"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import inspect
import logging
import os
import re
import unicodedata
from time import monotonic
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

import discord

from moderation_audit import MessageAuditCache, MessageAuditSnapshot, capture_message_snapshot
from moderation_delete import delete_message


LOGGER = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
REASON_LABELS = {
    "profanity": "자동 검열 · 금칙어", "link": "자동 검열 · 허용되지 않은 링크",
    "link+profanity": "자동 검열 · 링크 및 금칙어", "blocked_media": "자동 제한 · 차단된 미디어",
    "spam": "스팸 방지 · 도배", "admin_cleanup": "관리자 패널 · 메시지 정리",
    "moderation": "봇 검열", "external": "외부 삭제 · 실행자 확인 불가",
    "unknown": "확인 불가 · 봇 검열 여부 미확인",
}


def _code(value: Any) -> str:
    # Free-form rules and API exceptions must never become telemetry values.
    return value if isinstance(value, str) and value in REASON_LABELS else "unknown"


def _text(value: Any, limit: int = 1000) -> str:
    """Render untrusted text literally, without pings, links or code fences."""
    original = str(value)
    # Bound work BEFORE escaping. The bounded scheme token and word boundary
    # avoid quadratic matching on an adversarial, unbroken string of letters.
    text = original[:limit]
    text = "".join("�" if ((unicodedata.category(char) == "Cc" and char not in "\n\t") or
                          char in "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069") else char for char in text)
    text = re.sub(r"\b([a-z][a-z0-9+.-]{0,31}):(?=//)", lambda match: match.group(1) + ":\u200b", text, flags=re.I)
    text = text.replace("@", "@\u200b").replace("<", "<\u200b").replace(">", ">\u200b")
    text = discord.utils.escape_markdown(text)
    return text if len(text) <= limit and len(original) <= limit else text[: max(0, limit - 12)] + "… (표시 생략)"


def _collection(blocks: list[str], total: int | None, available: bool, *, note: str = "") -> str:
    if not available:
        return "메타데이터 확보 불가"
    shown: list[str] = []
    for block in blocks:
        # Reserve space for counts, explicit omission and any policy note.
        if len("\n".join([*shown, block])) > 850 - len(note):
            break
        shown.append(block)
    result = "\n".join(shown) or "없음"
    result += f"\n{len(shown)}개 표시 / 전체 {total if total is not None else '미상'}개"
    if (total is not None and total > len(shown)) or len(blocks) > len(shown):
        result += " · 나머지 표시 생략"
    if note:
        result += "\n" + note
    return result[:1024]


def _size(value: int | None) -> str:
    if value is None:
        return "크기 미상"
    if value < 1024:
        return f"{value:,} B"
    if value < 1024 * 1024:
        return f"{value / 1024:,.1f} KB"
    return f"{value / (1024 * 1024):,.1f} MB"


def build_deletion_embed(snapshot: MessageAuditSnapshot, *, deleted_at: datetime | None = None,
                         include_content: bool = True) -> discord.Embed:
    """Build one bounded red card matching the operator deletion-audit layout."""
    deleted_at = deleted_at or datetime.now(timezone.utc)
    if deleted_at.tzinfo is None:
        deleted_at = deleted_at.replace(tzinfo=timezone.utc)
    author = _text(snapshot.author_name or "작성자 확인 불가", 120)
    author_id = str(snapshot.author_id) if snapshot.author_id else "확인 불가"
    channel = _text(snapshot.channel_name or "채널 이름 확인 불가", 120)
    reason_code = _code(snapshot.reason.code)
    description = (
        f"**작성자** {author} · ID {author_id}\n"
        f"**위치** #{channel} · 채널 ID {snapshot.channel_id}\n"
        f"**삭제 확인 시각** {deleted_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST · 봇 수신 기준\n"
        f"**삭제 유형** {'봇 직접 삭제 · ' if snapshot.delete_origin == 'bot' else ''}{REASON_LABELS[reason_code]}"
    )
    if snapshot.created_at is not None:
        description += f"\n**작성 시각** {snapshot.created_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST"
    if snapshot.trigger == "message_edit":
        description += "\n**검열 경로** 수정 후 검열 · 삭제 직전 확인된 버전"
    elif snapshot.trigger == "history_scan":
        description += "\n**검열 경로** 정기 재점검 · 현재 확인된 버전의 위반 탐지"
    if snapshot.edited_at is not None:
        description += f"\n**수정 시각** {snapshot.edited_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST"
    embed = discord.Embed(title="🗑️ 메시지 삭제 기록", description=description[:4096], color=0xED4245)
    if not include_content:
        original = "원문 기록이 비활성화되어 있습니다."
    elif not snapshot.content_available:
        original = "원문 확인 불가(캐시/스냅샷 없음 또는 최신 수정 원문 미수신)"
    elif snapshot.content:
        original = _text(snapshot.content, 960)
        if len(snapshot.content) > 960 or snapshot.original_length > len(snapshot.content):
            original += f"\n원문 {snapshot.original_length:,}자 · 일부 표시"
    elif snapshot.attachment_count or snapshot.sticker_count or snapshot.embed_count:
        original = "본문 없음 · 첨부파일/스티커/임베드만 있는 메시지"
    else:
        original = "본문 없음 · 빈 메시지"
    embed.add_field(name="📝 삭제된 원문", value=original[:1024], inline=False)
    detection = REASON_LABELS[reason_code]
    if include_content and snapshot.reason.detected_root:
        detection += "\n탐지 금칙어: " + _text(snapshot.reason.detected_root, 160)
    if include_content and snapshot.reason.detail:
        detection += "\n세부 사유: " + _text(snapshot.reason.detail, 600)
    if reason_code in {"unknown", "external"}:
        detection += "\n삭제한 사용자/운영진은 추정하지 않습니다."
    embed.add_field(name="🔎 탐지 정보", value=detection[:1024], inline=False)

    if include_content:
        emoji_lines = []
        for item in snapshot.emojis[:12]:
            if item.kind == "custom":
                emoji_lines.append(f"커스텀 {'움직이는 ' if item.animated else ''}이모지 {_text(item.name, 60)} · ID {item.id}")
            else:
                emoji_lines.append(_text(item.text, 80))
        if len(snapshot.emojis) > 12:
            emoji_lines.append(f"외 {len(snapshot.emojis) - 12}개 표시 생략")
        emoji_text = "\n".join(emoji_lines)[:850] or "없음/확보된 정보 없음"
        emoji_text += "\n원문 유지 · 이모지 분석은 일부 형식만 지원"
        embed.add_field(name="🙂 이모지", value=emoji_text, inline=False)
        stickers = [f"{_text(item.name, 100)} · ID {item.id} · {_text(item.format, 40)}" for item in snapshot.stickers[:4]]
        embed.add_field(name="🎟️ 스티커", value=_collection(stickers, snapshot.sticker_count, snapshot.stickers_available), inline=False)
        attachment_lines = []
        for item in snapshot.attachments[:4]:
            dimensions = f" · {item.width}×{item.height}" if item.width and item.height else ""
            kind = {"image": "이미지", "gif": "움직이는 이미지", "video": "동영상", "audio": "오디오", "document": "문서", "archive": "압축파일"}.get(item.kind, "기타")
            attachment_lines.append(
                f"• {_text(item.filename or '이름 미상', 90)} · ID {item.id or '미상'}\n"
                f"  {_text(item.content_type or 'MIME 미상', 60)} · {_size(item.size)} · {kind} ({_text(item.kind, 25)})"
                f"{dimensions} · 스포일러 {'예' if item.spoiler else '아니오'}"
            )
        count = snapshot.attachment_count
        image_count = sum(item.kind in {"image", "gif"} for item in snapshot.attachments)
        counts = f"이미지 {image_count} · 기타 {len(snapshot.attachments) - image_count}"
        if count is not None and count > len(snapshot.attachments):
            counts = "확보한 메타데이터 기준: " + counts
        note = counts + "\n파일 바이트/미리보기는 보관하지 않습니다."
        embed.add_field(name=f"📎 첨부파일 · {count if count is not None else '미상'}개", value=_collection(
            attachment_lines, count, snapshot.attachments_available, note=note), inline=False)
        embeds = [f"{_text(item.kind, 40)} · {_text(item.title or '제목 없음', 130)}" for item in snapshot.embeds[:4]]
        embed.add_field(name="🧩 임베드", value=_collection(embeds, snapshot.embed_count, snapshot.embeds_available), inline=False)
        if snapshot.truncated_fields:
            embed.add_field(name="확보 범위", value="일부 데이터는 안전한 메모리/표시 상한으로 생략되었습니다.", inline=False)
    embed.set_footer(text=f"메시지 ID {snapshot.message_id} · 서버 {_text(snapshot.guild_name or '이름 확인 불가', 120)} · ID {snapshot.guild_id}")
    # Field caps above normally stay below 6000; trim metadata from the end if
    # every untrusted metadata item happens to hit its maximum simultaneously.
    removed = False
    while len(embed) > 5850 and len(embed.fields) > 2:
        embed.remove_field(len(embed.fields) - 1)
        removed = True
    if removed:
        embed.add_field(name="표시 생략", value="Discord의 6,000자 한도로 일부 메타데이터 표시를 생략했습니다.", inline=False)
    return embed


def _operator_permissions(value: Any) -> bool:
    return any(getattr(value, name, False) is True for name in ("administrator", "manage_guild", "manage_messages"))


def private_destination_reason(channel: Any, guild_id: int, admin_ids: Iterable[int] = ()) -> str | None:
    """Legacy helper retained for callers; V6 delivery uses reader containment."""
    if not isinstance(channel, discord.TextChannel):
        return "destination_not_text_channel"
    guild = getattr(channel, "guild", None)
    if guild is None or getattr(guild, "id", None) != guild_id:
        return "destination_guild_mismatch"
    try:
        everyone, bot_member = guild.default_role, guild.me
        if bot_member is None:
            return "bot_member_unavailable"
        if channel.permissions_for(everyone).view_channel:
            return "destination_public"
        bot_permissions = channel.permissions_for(bot_member)
        if not all(getattr(bot_permissions, key, False) for key in ("view_channel", "send_messages", "embed_links")):
            return "destination_bot_permissions"
        bot_id = bot_member.id
        allowed_ids = {guild.owner_id, bot_id, *admin_ids}
        for role in guild.roles:
            if role.id == everyone.id or not channel.permissions_for(role).view_channel:
                continue
            tags = getattr(role, "tags", None)
            own_bot_role = getattr(tags, "bot_id", None) == bot_id
            if not own_bot_role and not _operator_permissions(role.permissions):
                return "destination_nonoperator_role"
        for target, overwrite in channel.overwrites.items():
            if getattr(overwrite, "view_channel", None) is not True:
                continue
            if isinstance(target, discord.Role):
                tags = getattr(target, "tags", None)
                if getattr(tags, "bot_id", None) != bot_id and not _operator_permissions(target.permissions):
                    return "destination_nonoperator_role"
            elif isinstance(target, discord.Member):
                if target.id not in allowed_ids and not _operator_permissions(target.guild_permissions):
                    return "destination_nonoperator_member"
            elif isinstance(target, discord.Object) and target.type is discord.User and target.id in allowed_ids:
                # With the members intent disabled, Discord still exposes the
                # overwrite's user ID/type. Only explicit trusted IDs qualify.
                continue
            else:
                return "destination_unknown_overwrite"
    except Exception:
        return "destination_permissions_unavailable"
    return None


def _view_override(channel: Any, target_id: int, *, role: bool) -> bool | None:
    result = None
    for target, overwrite in channel.overwrites.items():
        if getattr(target, "id", None) != target_id:
            continue
        is_role = isinstance(target, discord.Role) or (isinstance(target, discord.Object) and target.type is discord.Role) or target_id == channel.guild.default_role.id
        if is_role != role:
            continue
        result = getattr(overwrite, "view_channel", None)
        if result not in (True, False, None):
            raise ValueError("invalid_permission_overwrite")
    return result


def _role_profiles(source: Any, destination: Any, guild: Any) -> tuple[tuple[bool, bool, bool, bool, bool, bool], ...]:
    """Compress roles by the six bits relevant to View Channel containment."""
    profiles = set()
    for role in guild.roles:
        if role.id == guild.default_role.id:
            continue
        source_value = _view_override(source, role.id, role=True)
        destination_value = _view_override(destination, role.id, role=True)
        view = getattr(role.permissions, "view_channel", None)
        administrator = getattr(role.permissions, "administrator", None)
        if not isinstance(view, bool) or not isinstance(administrator, bool):
            raise ValueError("role_permissions_unavailable")
        profiles.add((administrator, view, source_value is True, source_value is False,
                      destination_value is True, destination_value is False))
    return tuple(profiles)


def _member_overwrites(channel: Any) -> dict[int, Any]:
    result = {}
    known_roles = {role.id for role in channel.guild.roles}
    for target, overwrite in channel.overwrites.items():
        target_id = getattr(target, "id", None)
        if target_id in known_roles:
            continue
        if isinstance(target, discord.Member) or (isinstance(target, discord.Object) and target.type is discord.User):
            result[target_id] = target
        else:
            # A missing role/member type could conceal a permitted reader.
            raise ValueError("unknown_permission_overwrite")
    return result


def public_destination_reason(source: Any, destination: Any, guild_id: int) -> str | None:
    """Prove every destination reader may read the source, or fail closed.

    Role grants combine by union and role overwrite Allow dominates Deny. Any
    counterexample needs at most two role profiles: one destination grant and
    one source denial. Member overwrites are checked separately, using actual
    membership when available and all possible role profiles otherwise. This
    also accepts member-role channels where @everyone is denied at both ends.
    """
    if not isinstance(destination, discord.TextChannel):
        return "destination_not_text_channel"
    if not isinstance(source, discord.TextChannel):
        return "source_not_text_channel"
    if source.id == destination.id:
        return "destination_recursion"
    guild = getattr(source, "guild", None)
    if (guild is None or getattr(guild, "id", None) != guild_id or
            getattr(getattr(destination, "guild", None), "id", None) != guild_id):
        return "destination_guild_mismatch"
    try:
        bot_member = guild.me
        if bot_member is None:
            return "bot_member_unavailable"
        bot_permissions = destination.permissions_for(bot_member)
        for name in ("view_channel", "send_messages", "embed_links"):
            if getattr(bot_permissions, name, None) is not True:
                return "destination_bot_" + name
        if source.permissions_for(bot_member).view_channel is not True:
            return "source_bot_view_channel"
        everyone = guild.default_role
        everyone_view = getattr(everyone.permissions, "view_channel", None)
        everyone_admin = getattr(everyone.permissions, "administrator", None)
        if not isinstance(everyone_view, bool) or not isinstance(everyone_admin, bool):
            return "source_permissions_unavailable"
        profiles = _role_profiles(source, destination, guild)
        source_members, destination_members = _member_overwrites(source), _member_overwrites(destination)
        # Administrator ignores all channel overwrites in both channels.
        combinations = [()]
        combinations.extend((profile,) for profile in profiles)
        combinations.extend((first, second) for index, first in enumerate(profiles) for second in profiles[index + 1:])
        source_everyone = _view_override(source, everyone.id, role=True)
        destination_everyone = _view_override(destination, everyone.id, role=True)

        def visible(combination, *, destination_side=False, member_override=None):
            if everyone_admin or any(item[0] for item in combination):
                return True
            base = everyone_view or any(item[1] for item in combination)
            everyone_override = destination_everyone if destination_side else source_everyone
            if everyone_override is not None:
                base = everyone_override
            allow_index, deny_index = (4, 5) if destination_side else (2, 3)
            if any(item[deny_index] for item in combination):
                base = False
            if any(item[allow_index] for item in combination):
                base = True
            return base if member_override is None else member_override

        cases = {(everyone_admin or any(item[0] for item in combination), visible(combination),
                  visible(combination, destination_side=True)) for combination in combinations}
        for _, source_view, destination_view in cases:
            if destination_view and not source_view:
                return "source_reader_scope_exceeded"
        for member_id in source_members.keys() | destination_members.keys():
            member = source_members.get(member_id) or destination_members.get(member_id)
            resolver = getattr(guild, "get_member", None)
            actual = resolver(member_id) if callable(resolver) else None
            if not isinstance(actual, discord.Member) and isinstance(member, discord.Member):
                actual = member
            if isinstance(actual, discord.Member):
                destination_view = destination.permissions_for(actual).view_channel
                source_view = source.permissions_for(actual).view_channel
                if not isinstance(destination_view, bool) or not isinstance(source_view, bool):
                    return "source_permissions_unavailable"
                if destination_view and not source_view:
                    return "source_member_scope_exceeded"
            else:
                source_override = _view_override(source, member_id, role=False)
                destination_override = _view_override(destination, member_id, role=False)
                for administrator, source_view, destination_view in cases:
                    source_view = source_view if administrator or source_override is None else source_override
                    destination_view = destination_view if administrator or destination_override is None else destination_override
                    if destination_view and not source_view:
                        return "source_member_scope_exceeded"
    except Exception:
        return "source_permissions_unavailable"
    return None


def _configured_ids(raw: str) -> tuple[frozenset[int], bool]:
    if not raw.strip():
        return frozenset(), True
    values = raw.split(",")
    if any(not 1 <= len(value.strip()) <= 20 or not value.strip().isascii() or not value.strip().isdigit() or
           not 0 < int(value.strip()) < 2 ** 64 for value in values):
        return frozenset(), False
    return frozenset(int(value.strip()) for value in values), True


@dataclass
class _DeleteAttempt:
    original: MessageAuditSnapshot
    snapshot: MessageAuditSnapshot
    gateway_observed: bool = False
    gateway_observed_at: datetime | None = None


class DeletionAuditService:
    def __init__(self, routes: Mapping[int, int], channel_resolver: Callable[[int], Any], *,
                 include_content: bool | None = None, configured_admin_ids: Iterable[int] | None = None,
                 cache: MessageAuditCache | None = None, clock: Callable[[], float] = monotonic,
                 delivery_limit: int = 4096, delivery_ttl: float = 600, concurrency: int = 4, pending_limit: int = 64,
                 wall_clock: Callable[[], datetime] | None = None, guild_resolver: Callable[[int], Any] | None = None,
                 enabled: bool | None = None, all_guild_channels: bool | None = None):
        self.routes = dict(routes)
        self.channel_resolver = channel_resolver
        self.cache = cache if cache is not None else MessageAuditCache()
        raw = os.getenv("DELETION_PUBLIC_LOG_INCLUDE_CONTENT", "1").strip()
        self.include_content = raw == "1" if include_content is None else include_content is True
        self.enabled = os.getenv("DELETION_PUBLIC_LOG_ENABLED", "1").strip() == "1" if enabled is None else enabled is True
        self.all_guild_channels = os.getenv("DELETION_LOG_ALL_GUILD_CHANNELS", "1").strip() == "1" if all_guild_channels is None else all_guild_channels is True
        self.extra_sources, self.extra_sources_valid = _configured_ids(os.getenv("DELETION_LOG_EXTRA_SOURCE_CHANNEL_IDS", ""))
        self.guild_resolver = guild_resolver
        self.admin_ids = None if configured_admin_ids is None else frozenset(configured_admin_ids)
        self.clock, self.delivery_limit, self.delivery_ttl = clock, max(1, delivery_limit), max(1, delivery_ttl)
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._delivered: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._deleted: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._inflight: set[tuple[int, int]] = set()
        self.pending_limit = max(1, pending_limit)
        self._attempts: dict[tuple[int, int], _DeleteAttempt] = {}
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._stats: dict[int, dict[str, Any]] = {}

    def _stat(self, guild_id: int) -> dict[str, Any]:
        return self._stats.setdefault(guild_id, {"sent": 0, "failures": 0, "last_success": None,
                                                  "last_reason": None, "reasons": Counter()})

    def _record(self, guild_id: int, reason: str, *, failure: bool = False) -> None:
        stats = self._stat(guild_id)
        stats["last_reason"] = reason
        stats["reasons"][reason] += 1
        if failure:
            stats["failures"] += 1

    def _resolve_cached(self, channel_id: int) -> Any:
        try:
            channel = self.channel_resolver(channel_id)
            if inspect.isawaitable(channel):
                if inspect.iscoroutine(channel):
                    channel.close()
                return None
            return channel
        except Exception:
            return None

    def _destination_ids(self, guild_id: int) -> set[int]:
        return {destination_id for destination_id in set(self.routes.values())
                if (getattr(self._resolve_cached(destination_id), "id", None) == destination_id and
                    getattr(getattr(self._resolve_cached(destination_id), "guild", None), "id", None) == guild_id)}

    def destination_for(self, guild_id: int, channel_id: int) -> int | None:
        """Reuse the same guild's existing destination; never invent a channel."""
        if not self.enabled or not guild_id or channel_id in self.routes.values():
            return None
        if channel_id in self.routes:
            return self.routes[channel_id]
        if not self.all_guild_channels and channel_id not in self.extra_sources:
            return None
        source = self._resolve_cached(channel_id)
        if getattr(source, "id", None) != channel_id or getattr(getattr(source, "guild", None), "id", None) != guild_id:
            return None
        destinations = self._destination_ids(guild_id)
        return next(iter(destinations)) if len(destinations) == 1 else None

    def can_track_source(self, channel: Any) -> bool:
        guild_id, channel_id = getattr(getattr(channel, "guild", None), "id", 0), getattr(channel, "id", 0)
        destination_id = self.destination_for(guild_id, channel_id)
        if destination_id is None:
            return False
        destination = self._resolve_cached(destination_id)
        if getattr(destination, "id", None) != destination_id:
            return False
        return public_destination_reason(channel, destination, guild_id) is None

    def diagnostics(self, guild_or_id: Any) -> dict[str, Any]:
        """Return aggregate metadata only, suitable for delegated administrators."""
        guild_id = getattr(guild_or_id, "id", guild_or_id)
        destinations = self._destination_ids(guild_id)
        reason = "disabled" if not self.enabled else ("destination_missing" if not destinations else
                 "destination_ambiguous" if len(destinations) != 1 else "ready")
        if self.enabled and not destinations:
            for destination_id in set(self.routes.values()):
                candidate = self._resolve_cached(destination_id)
                if (getattr(getattr(candidate, "guild", None), "id", None) == guild_id and
                        getattr(candidate, "id", None) != destination_id):
                    reason = "destination_id_mismatch"
                    break
        destination = self._resolve_cached(next(iter(destinations))) if len(destinations) == 1 else None
        permissions = None
        if destination is not None:
            try:
                permissions = destination.permissions_for(destination.guild.me)
                for name in ("view_channel", "send_messages", "embed_links"):
                    if getattr(permissions, name, None) is not True:
                        reason = "destination_bot_" + name
                        break
            except Exception:
                reason = "destination_permissions_unavailable"
        guild = guild_or_id if hasattr(guild_or_id, "channels") else None
        if guild is None and self.guild_resolver is not None:
            try:
                guild = self.guild_resolver(guild_id)
            except Exception:
                guild = None
        if guild is None and destination is not None:
            guild = destination.guild
        channels = getattr(guild, "text_channels", ()) or ()
        if not isinstance(channels, (tuple, list)):
            channels = ()
        tracked = sum(self.can_track_source(channel) for channel in channels)
        stats = self._stat(guild_id)
        return {"result": "ready" if reason == "ready" else "failed", "reason": reason,
                "destination_identified": len(destinations) == 1, "destination_count": len(destinations),
                "bot_view_channel": getattr(permissions, "view_channel", False) is True,
                "bot_send_messages": getattr(permissions, "send_messages", False) is True,
                "bot_embed_links": getattr(permissions, "embed_links", False) is True,
                "enabled": self.enabled, "include_content": self.include_content,
                "all_guild_channels": self.all_guild_channels, "extra_sources_valid": self.extra_sources_valid,
                "configured_source_count": sum(getattr(getattr(self._resolve_cached(source_id), "guild", None), "id", None) == guild_id
                                               for source_id in self.routes),
                "extra_source_count": sum(getattr(getattr(self._resolve_cached(source_id), "guild", None), "id", None) == guild_id
                                           for source_id in self.extra_sources),
                "eligible_source_count": tracked, "excluded_source_count": max(0, len(channels) - tracked),
                "sent": stats["sent"], "failures": stats["failures"],
                "last_success": stats["last_success"], "last_reason": stats["last_reason"],
                "reason_counts": dict(stats["reasons"])}

    async def send_test(self, guild_or_id: Any) -> dict[str, Any]:
        """Send one synthetic card to this guild's existing destination only."""
        if hasattr(guild_or_id, "guild"):
            guild_or_id = guild_or_id.guild
        status = self.diagnostics(guild_or_id)
        if status["result"] != "ready":
            return status
        guild_id = getattr(guild_or_id, "id", guild_or_id)
        destination_ids = self._destination_ids(guild_id)
        if len(destination_ids) != 1:
            return {**status, "result": "failed", "reason": "destination_missing"}
        destination_id = next(iter(destination_ids))
        destination = self._resolve_cached(destination_id)
        if getattr(destination, "id", None) != destination_id:
            return {**status, "result": "failed", "reason": "destination_id_mismatch"}
        embed = discord.Embed(title="🧪 삭제 로그 전송 테스트", description=(
            "기존 공용 기록 채널 전송 확인입니다.\n실제 사용자 원문·파일은 포함하지 않습니다.\n"
            f"확인 시각: {self._observation_time().astimezone(KST):%Y-%m-%d %H:%M:%S} KST"), colour=0xED4245)
        try:
            await destination.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self._stat(guild_id)["last_success"] = self._observation_time()
            self._record(guild_id, "test_sent")
            return {**status, "result": "ready", "reason": "test_sent"}
        except Exception as error:
            reason = self._delivery_error(error)
            self._record(guild_id, reason, failure=True)
            LOGGER.warning("event=deletion_audit_test result=failed reason=%s guild_id=%s", reason, guild_id)
            return {**status, "result": "failed", "reason": reason}

    @staticmethod
    def _delivery_error(error: Exception) -> str:
        status = getattr(error, "status", None)
        if status == 403:
            return "delivery_forbidden"
        if status == 429:
            return "delivery_rate_limited"
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            return "delivery_timeout"
        if isinstance(error, discord.HTTPException) or isinstance(status, int):
            return "delivery_http_failed"
        return "delivery_failed"

    def _observation_time(self) -> datetime:
        observed_at = self.wall_clock()
        return observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=timezone.utc)

    def _routed(self, guild_id: int, channel_id: int) -> bool:
        return self.destination_for(guild_id, channel_id) is not None

    def remember(self, message: Any) -> MessageAuditSnapshot | None:
        guild_id = getattr(getattr(message, "guild", None), "id", 0)
        channel_id = getattr(getattr(message, "channel", None), "id", 0)
        if not self._routed(guild_id, channel_id) or getattr(getattr(message, "author", None), "bot", False):
            return None
        try:
            previous = self.cache.get(guild_id, getattr(message, "id", 0))
            attempt = self._attempts.get((guild_id, getattr(message, "id", 0)))
            candidates = [value for value in (previous, attempt.snapshot if attempt is not None else None)
                          if value is not None and value.edited_at is not None]
            latest = max(candidates, key=lambda value: value.edited_at) if candidates else None
            snapshot = self.cache.capture(message)
            if latest is not None and (snapshot.edited_at is None or snapshot.edited_at < latest.edited_at):
                # Cached edit callbacks can arrive after a newer raw/cached
                # revision. Never overwrite confirmed evidence with an older
                # body, including while its delete request is still pending.
                self.cache.remember(latest)
                return latest
            if attempt is not None:
                attempt.original = snapshot
                snapshot = replace(snapshot, reason=attempt.snapshot.reason, delete_origin="bot",
                                   trigger=attempt.snapshot.trigger,
                                   edited_at=snapshot.edited_at or attempt.snapshot.edited_at)
                attempt.snapshot = snapshot
                self.cache.remember(snapshot)
            return snapshot
        except Exception:
            LOGGER.warning("event=deletion_audit_snapshot result=failed guild_id=%s message_id=%s", guild_id, getattr(message, "id", 0))
            return None

    def on_raw_message_edit(self, payload: Any) -> None:
        # A raw edit for an evicted Discord message cannot safely preserve the
        # old original. The subsequent cached edit event supplies fresh data.
        guild_id, message_id = getattr(payload, "guild_id", 0), getattr(payload, "message_id", 0)
        data = getattr(payload, "data", {}) or {}
        if any(key in data for key in ("content", "attachments", "embeds", "sticker_items")):
            snapshot = self.cache.get(guild_id, message_id)
            attempt = self._attempts.get((guild_id, message_id))
            if attempt is not None and (snapshot is None or (attempt.snapshot.edited_at is not None and
                    (snapshot.edited_at is None or attempt.snapshot.edited_at > snapshot.edited_at))):
                snapshot = attempt.snapshot
            timestamp = data.get("edited_timestamp")
            edited_at = None
            if isinstance(timestamp, str) and len(timestamp) <= 64:
                try:
                    edited_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if edited_at.tzinfo is None:
                        edited_at = None
                except ValueError:
                    pass
            if snapshot is not None and snapshot.edited_at is not None and edited_at is not None:
                if edited_at < snapshot.edited_at:
                    return
                if (snapshot.content_available and isinstance(data.get("content"), str)
                        and data["content"] == snapshot.content and edited_at == snapshot.edited_at):
                    # Cached/raw handlers can report the same verified edit
                    # while the delete HTTP request is still pending.
                    return
            self.cache.pop(guild_id, message_id)
            if snapshot is not None:
                # A snapshot captured before the HTTP request is also stale.
                # A later reliable on_message_edit(after) restores current data.
                updated = replace(snapshot, content="", content_available=False, original_length=None,
                                  emojis=(), attachments=(), stickers=(), embeds=(), attachment_count=None,
                                  sticker_count=None, embed_count=None, attachments_available=False,
                                  stickers_available=False, embeds_available=False,
                                  edited_at=edited_at or snapshot.edited_at,
                                  truncated_fields=tuple(sorted(set(snapshot.truncated_fields) | {"raw_edit"})))
                # A bounded content-free tombstone keeps the known timestamp
                # even after raw invalidation, so a delayed older callback
                # cannot restore a body already known to be out of date.
                self.cache.remember(updated)
                if attempt is not None:
                    attempt.snapshot = updated
                    attempt.original = replace(updated, reason=replace(updated.reason, code="", detected_root="", detail=""),
                                               delete_origin="unknown")

    async def delete(self, message: Any, *, reason: str, matched_rule: str | None = None,
                     detail: str | None = None, trigger: str = "message_new", edited_at: datetime | None = None) -> bool:
        key = (getattr(getattr(message, "guild", None), "id", 0), getattr(message, "id", 0))
        for existing, deleted_at in list(self._deleted.items()):
            if self.clock() - deleted_at >= self.delivery_ttl:
                self._deleted.pop(existing, None)
        if key in self._deleted:
            self._record(key[0], "duplicate_delete")
            return False
        if key in self._attempts:
            LOGGER.info("event=deletion_audit result=skipped reason=delete_pending guild_id=%s message_id=%s", *key)
            return False
        snapshot = self.remember(message)
        original, attempt = snapshot, None
        if snapshot is not None:
            try:
                snapshot = self.cache.annotate(snapshot.guild_id, snapshot.message_id, reason_code=_code(reason),
                                               detected_root=matched_rule or "", reason_detail=detail or "",
                                               delete_origin="bot", trigger=trigger, edited_at=edited_at)
            except Exception:
                LOGGER.warning("event=deletion_audit_snapshot result=failed guild_id=%s message_id=%s",
                               snapshot.guild_id, snapshot.message_id)
                snapshot = None
        if snapshot is not None and len(self._attempts) < self.pending_limit:
            attempt = _DeleteAttempt(original, snapshot)
            self._attempts[key] = attempt
        elif snapshot is not None:
            # Enforcement still proceeds when audit capacity is full, but no
            # unconfirmed bot attribution may leak into a later gateway event.
            self.cache.remember(original)
            snapshot = None
            LOGGER.warning("event=deletion_audit result=skipped reason=burst_limit guild_id=%s message_id=%s", *key)
        # Snapshot and reason exist before the HTTP await, since a gateway
        # deletion can arrive before Discord's HTTP response is processed.
        try:
            deleted = await delete_message(message, reason=_code(reason), logger=LOGGER)
            observed_at = self._observation_time()
        except BaseException:
            if attempt is not None:
                self._attempts.pop(key, None)
                self.cache.remember(attempt.original)
            raise
        if attempt is not None:
            self._attempts.pop(key, None)
            snapshot = attempt.snapshot
            if not deleted:
                self.cache.remember(attempt.original)
                if attempt.gateway_observed:
                    await self._confirmed(attempt.original, observed_at=attempt.gateway_observed_at or observed_at)
        if deleted:
            self._deleted[key] = self.clock()
            while len(self._deleted) > self.delivery_limit:
                self._deleted.popitem(last=False)
        if deleted and snapshot is not None:
            self.cache.remember(snapshot)
            await self._publish(snapshot, deleted_at=(attempt.gateway_observed_at if attempt is not None else None) or observed_at)
        return deleted

    async def on_message_delete(self, message: Any, *, _observed_at: datetime | None = None) -> None:
        observed_at = _observed_at or self._observation_time()
        guild_id = getattr(getattr(message, "guild", None), "id", 0)
        channel_id = getattr(getattr(message, "channel", None), "id", 0)
        if not self._routed(guild_id, channel_id) or getattr(getattr(message, "author", None), "bot", False):
            return
        snapshot = self.cache.get(guild_id, getattr(message, "id", 0)) or self.remember(message)
        if snapshot is not None:
            await self._confirmed(snapshot, observed_at=observed_at)

    async def on_raw_message_delete(self, payload: Any, *, _observed_at: datetime | None = None) -> None:
        observed_at = _observed_at or self._observation_time()
        cached = getattr(payload, "cached_message", None)
        if cached is not None:
            await self.on_message_delete(cached, _observed_at=observed_at)
            return
        guild_id, channel_id = getattr(payload, "guild_id", 0), getattr(payload, "channel_id", 0)
        message_id = getattr(payload, "message_id", 0)
        if not self._routed(guild_id, channel_id) or not message_id:
            return
        snapshot = self.cache.get(guild_id, message_id)
        if snapshot is None:
            snapshot = capture_message_snapshot(SimpleNamespace(
                id=message_id, guild=SimpleNamespace(id=guild_id, name=""),
                channel=SimpleNamespace(id=channel_id, name=""),
            ))
        await self._confirmed(snapshot, observed_at=observed_at)

    async def _confirmed(self, snapshot: MessageAuditSnapshot, *, observed_at: datetime) -> None:
        attempt = self._attempts.get((snapshot.guild_id, snapshot.message_id))
        if attempt is not None:
            # Do not wait: callbacks can arrive inside the pending HTTP await.
            attempt.gateway_observed = True
            if attempt.gateway_observed_at is None:
                attempt.gateway_observed_at = observed_at
            return
        if not snapshot.reason.code and snapshot.content_available:
            snapshot = replace(snapshot, reason=replace(snapshot.reason, code="external"), delete_origin="external")
        await self._publish(snapshot, deleted_at=observed_at)

    async def on_raw_bulk_message_delete(self, payload: Any) -> None:
        observed_at = self._observation_time()
        guild_id, channel_id = getattr(payload, "guild_id", 0), getattr(payload, "channel_id", 0)
        if not self._routed(guild_id, channel_id):
            return
        cached = {message.id: message for message in (getattr(payload, "cached_messages", None) or ())}
        # Discord bulk payloads contain at most 100 IDs. Process bounded batches
        # even for a malformed/test payload, without allocating unbounded tasks.
        batch = []
        for message_id in getattr(payload, "message_ids", ()):
            batch.append(self.on_raw_message_delete(SimpleNamespace(
                guild_id=guild_id, channel_id=channel_id, message_id=message_id,
                cached_message=cached.get(message_id)), _observed_at=observed_at))
            if len(batch) == 20:
                await asyncio.gather(*batch)
                batch.clear()
        if batch:
            await asyncio.gather(*batch)

    async def _publish(self, snapshot: MessageAuditSnapshot, *, deleted_at: datetime) -> bool:
        key, now = (snapshot.guild_id, snapshot.message_id), self.clock()
        for existing, delivered_at in list(self._delivered.items()):
            if now - delivered_at >= self.delivery_ttl:
                self._delivered.pop(existing, None)
        if key in self._delivered or key in self._inflight:
            self._record(snapshot.guild_id, "duplicate")
            return False
        if len(self._inflight) >= self.pending_limit:
            LOGGER.warning("event=deletion_audit result=skipped reason=burst_limit guild_id=%s message_id=%s", *key)
            self._record(snapshot.guild_id, "burst_limit", failure=True)
            return False
        self._inflight.add(key)
        try:
            async with self._semaphore:
                destination_id = self.destination_for(snapshot.guild_id, snapshot.channel_id)
                if destination_id is None:
                    self._record(snapshot.guild_id, "destination_missing", failure=True)
                    return False
                channel = self.channel_resolver(destination_id)
                if inspect.isawaitable(channel):
                    channel = await channel
                source = self.channel_resolver(snapshot.channel_id)
                if inspect.isawaitable(source):
                    source = await source
                if channel is not None and getattr(channel, "id", None) != destination_id:
                    reason = "destination_id_mismatch"
                elif source is not None and getattr(source, "id", None) != snapshot.channel_id:
                    reason = "source_id_mismatch"
                else:
                    reason = public_destination_reason(source, channel, snapshot.guild_id)
                if reason:
                    LOGGER.warning("event=deletion_audit result=skipped reason=%s guild_id=%s message_id=%s", reason, *key)
                    self._record(snapshot.guild_id, reason, failure=True)
                    return False
                if not snapshot.content_available:
                    self._record(snapshot.guild_id, "snapshot_unavailable")
                embed = build_deletion_embed(snapshot, deleted_at=deleted_at, include_content=self.include_content)
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                self._delivered[key] = self.clock()
                self._delivered.move_to_end(key)
                while len(self._delivered) > self.delivery_limit:
                    self._delivered.popitem(last=False)
                stats = self._stat(snapshot.guild_id)
                stats["sent"] += 1
                stats["last_success"] = self._observation_time()
                self._record(snapshot.guild_id, "sent")
                LOGGER.info("event=deletion_audit result=sent reason=%s guild_id=%s message_id=%s", _code(snapshot.reason.code), *key)
                return True
        except Exception as error:
            # No repr/str/traceback: Discord exceptions may contain message data.
            reason = self._delivery_error(error)
            self._record(snapshot.guild_id, reason, failure=True)
            LOGGER.warning("event=deletion_audit result=failed reason=%s guild_id=%s message_id=%s", reason, *key)
            return False
        finally:
            self._inflight.discard(key)


__all__ = ["DeletionAuditService", "build_deletion_embed", "private_destination_reason", "public_destination_reason"]
