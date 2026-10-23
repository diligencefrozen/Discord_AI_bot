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
import math
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
    "moderation": "봇 검열", "external": "일반 삭제 · 삭제자 확인 불가",
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
    # escape_markdown's greedy masked-link branch can swallow later Markdown
    # inside one match, even with ignore_links=False. Escape tokens literally
    # so repeated links cannot leave trailing fences or spoilers unescaped.
    text = re.sub(r"([\\`*_~|\[\]#>])", r"\\\1", text)
    text = re.sub(r"(?m)^(\s*)([-+])", r"\1\\\2", text)
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
        f"**👤 User · 작성자** {author} · ID {author_id}\n"
        f"**📍 Channel · 위치** #{channel} · 채널 ID {snapshot.channel_id}\n"
        f"**🆔 메시지 ID** {snapshot.message_id}\n"
        f"**삭제 확인 시각** {deleted_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST · 봇 수신 기준\n"
        f"**삭제 유형** {'봇 직접 삭제 · ' if snapshot.delete_origin == 'bot' else ''}{REASON_LABELS[reason_code]}"
    )
    if snapshot.created_at is not None:
        description += f"\n**작성 시각** {snapshot.created_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST"
    if snapshot.trigger == "message_edit":
        description += "\n**검열 경로** 수정 후 검열 · 삭제 직전 확인된 버전"
    elif snapshot.trigger == "history_scan":
        description += "\n**검열 경로** 정기 재점검 · 현재 확인된 버전의 위반 탐지"
    elif snapshot.delete_origin == "bot":
        description += "\n**검열 경로** 새 메시지"
    if snapshot.edited_at is not None:
        description += f"\n**수정 시각** {snapshot.edited_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST"
    title = "🗑️ 메시지 삭제 기록 · 자동 검열" if snapshot.delete_origin == "bot" and reason_code in {
        "profanity", "link", "link+profanity", "moderation", "blocked_media"} else "🗑️ 메시지 삭제 기록"
    embed = discord.Embed(title=title, description=description[:4096], color=0xED4245)
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


def build_metadata_deletion_embed(snapshot: MessageAuditSnapshot, *, deleted_at: datetime | None = None,
                                  reason_code: str = "private_source") -> discord.Embed:
    """Restore explicitly routed legacy identity without republishing bodies.

    The operator's LOG_ROUTES selection authorizes these base fields only.
    Detected roots, free-form details, media names and URLs remain excluded.
    """
    allowed_reasons = {"private_source", "unknown_permissions", "source_not_found", "content_disabled",
                       "snapshot_not_found", "snapshot_stale"}
    reason_code = reason_code if reason_code in allowed_reasons else "unknown_permissions"
    embed = build_deletion_embed(snapshot, deleted_at=deleted_at, include_content=False)
    embed.title = "🗑️ 메시지 삭제 기록"
    if reason_code == "content_disabled":
        content_state = "원문 기록이 비활성화되어 있습니다."
    elif reason_code in {"snapshot_not_found", "snapshot_stale"}:
        content_state = "원문 확인 불가(캐시/스냅샷 없음 또는 최신 수정 원문 미수신)"
    else:
        content_state = "원본 채널 공개 정책에 따라 생략 · 원문 비공개"
        if not snapshot.content_available:
            content_state += "\n원문 확인 불가(캐시/스냅샷 없음)"
    embed.set_field_at(0, name="📝 삭제된 원문", value=content_state, inline=False)
    embed.add_field(name="🔒 기록 상태", value=f"기본 기록만 표시 · {reason_code}", inline=False)
    counts = " · ".join(f"{label} {count if count is not None else '확인 불가'}개"
                        for label, count in (("첨부파일", snapshot.attachment_count),
                                             ("스티커", snapshot.sticker_count), ("임베드", snapshot.embed_count)))
    embed.add_field(name="📎 첨부파일 · 상태", value=counts + "\n상세 파일명·미디어 내용은 공개하지 않습니다.", inline=False)
    return embed


def build_restricted_deletion_embed(*, deleted_at: datetime | None = None,
                                    reason_code: str = "private_source") -> discord.Embed:
    """Non-identifying receipt when a source has a narrower readership.

    This card intentionally contains NO source/channel/user/message identifiers,
    reason details, attachment names or original message content. The record
    destination is public and must not widen private-channel visibility.
    """
    deleted_at = deleted_at or datetime.now(timezone.utc)
    if deleted_at.tzinfo is None:
        deleted_at = deleted_at.replace(tzinfo=timezone.utc)
    reason_code = "unknown_permissions" if reason_code == "unknown_permissions" else "private_source"
    explanation = ("원본 채널의 권한 정보를 확인하지 못해" if reason_code == "unknown_permissions" else
                   "원본 채널의 열람 범위가 공용 기록 채널보다 좁아")
    return discord.Embed(
        title="🗑️ 메시지 삭제 감지 · 열람 범위 제한",
        description=(
            f"**확인 시각** {deleted_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST\n"
            "이 서버에서 메시지 삭제가 감지되었습니다.\n"
            f"{explanation} 작성자·채널·원문·첨부 정보는 공개하지 않습니다.\n"
            f"**제한 사유** {reason_code}"
        ),
        colour=0xED4245,
    )


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


@dataclass(frozen=True)
class PublicDestinationDecision:
    """Safe permission evidence; never retains an exception message or content."""

    status: str
    reason_code: str
    stage: str
    exception_type: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status == "PUBLIC_VERIFIED"

    @property
    def public_verified(self) -> bool:
        return self.allowed


class _PermissionEvidenceError(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _overwrite_kind(target: Any) -> str:
    if isinstance(target, discord.Role):
        return "role"
    if isinstance(target, (discord.Member, discord.User)):
        return "member"
    if isinstance(target, discord.Object):
        if target.type is discord.Role:
            return "role"
        # GuildChannel.overwrites is defined in discord.abc, where User is the
        # abc.User protocol, NOT the exported concrete discord.User class.
        # Comparing only with discord.User rejects actual uncached members.
        if target.type in (discord.abc.User, discord.User, discord.Member):
            return "member"
    raise _PermissionEvidenceError("unknown_permission_overwrite")


def _view_override(channel: Any, target_id: int, *, role: bool) -> bool | None:
    result = None
    for target, overwrite in channel.overwrites.items():
        if getattr(target, "id", None) != target_id:
            continue
        is_role = _overwrite_kind(target) == "role"
        if is_role != role:
            continue
        result = getattr(overwrite, "view_channel", None)
        if result is not True and result is not False and result is not None:
            raise _PermissionEvidenceError("invalid_permission_overwrite")
    return result


def _unknown_role_ids(source: Any, destination: Any, guild: Any) -> set[int]:
    known_roles = {role.id for role in guild.roles}
    return {target.id for channel in (source, destination) for target in channel.overwrites
            if _overwrite_kind(target) == "role" and target.id not in known_roles}


def _role_profiles(source: Any, destination: Any, guild: Any, *, include_unknown: bool = True) -> tuple[tuple[bool, bool, bool, bool, bool, bool], ...]:
    """Compress known roles, conservatively expanding uncached role grants.

    Uncached role overwrites are real role targets, not malformed member
    overwrites. Their base View Channel bit may be either value. Administrator
    cannot produce a containment failure because it sees both channels.
    """
    profiles = set()
    for role in guild.roles:
        if role.id == guild.default_role.id:
            continue
        source_value = _view_override(source, role.id, role=True)
        destination_value = _view_override(destination, role.id, role=True)
        view = getattr(role.permissions, "view_channel", None)
        administrator = getattr(role.permissions, "administrator", None)
        if not isinstance(view, bool) or not isinstance(administrator, bool):
            raise _PermissionEvidenceError("role_permissions_unavailable")
        profiles.add((administrator, view, source_value is True, source_value is False,
                      destination_value is True, destination_value is False))
    if include_unknown:
        for role_id in _unknown_role_ids(source, destination, guild):
            source_value = _view_override(source, role_id, role=True)
            destination_value = _view_override(destination, role_id, role=True)
            for view in (False, True):
                profiles.add((False, view, source_value is True, source_value is False,
                              destination_value is True, destination_value is False))
    return tuple(profiles)


def _member_overwrites(channel: Any) -> dict[int, Any]:
    result = {}
    for target, overwrite in channel.overwrites.items():
        target_id = getattr(target, "id", None)
        kind = _overwrite_kind(target)
        if kind == "role":
            continue
        if isinstance(target_id, int) and target_id > 0:
            result[target_id] = target
        else:
            raise _PermissionEvidenceError("invalid_permission_target")
    return result


def evaluate_public_destination(source: Any, destination: Any, guild_id: int, *,
                                bot_member: Any = None) -> PublicDestinationDecision:
    """Prove every destination reader may read the source, or fail closed.

    Role grants combine by union and role overwrite Allow dominates Deny. Any
    counterexample needs at most two role profiles: one destination grant and
    one source denial. Member overwrites are checked separately, using actual
    membership when available and all possible role profiles otherwise. This
    also accepts member-role channels where @everyone is denied at both ends.
    """
    def decision(status, reason_code, stage, exception_type=None):
        return PublicDestinationDecision(status, reason_code, stage, exception_type)

    if destination is None:
        return decision("DESTINATION_MISSING", "destination_missing", "destination_type")
    if source is None:
        return decision("SOURCE_MISSING", "source_not_found", "source_type")
    if not isinstance(destination, discord.TextChannel):
        return decision("UNSUPPORTED_CHANNEL_TYPE", "destination_not_text_channel", "destination_type")
    if not isinstance(source, discord.TextChannel):
        return decision("UNSUPPORTED_CHANNEL_TYPE", "source_not_text_channel", "source_type")
    if source.id == destination.id:
        return decision("DESTINATION_FORBIDDEN", "destination_recursion", "routing")
    guild = getattr(source, "guild", None)
    if guild is None or getattr(guild, "id", None) != guild_id:
        return decision("SOURCE_GUILD_MISMATCH", "source_guild_mismatch", "guild_validation")
    if getattr(getattr(destination, "guild", None), "id", None) != guild_id:
        return decision("DESTINATION_GUILD_MISMATCH", "destination_guild_mismatch", "guild_validation")
    stage = "bot_member"
    try:
        bot_member = bot_member if bot_member is not None else guild.me
        if bot_member is None:
            return decision("UNKNOWN_PERMISSIONS", "bot_member_unavailable", stage)
        member_guild = getattr(bot_member, "guild", None)
        member_guild_id = getattr(member_guild, "id", None)
        if isinstance(member_guild_id, int) and member_guild_id != guild_id:
            return decision("UNKNOWN_PERMISSIONS", "bot_member_guild_mismatch", stage)
        stage = "destination_bot_permissions"
        bot_permissions = destination.permissions_for(bot_member)
        for name in ("view_channel", "send_messages", "embed_links"):
            if getattr(bot_permissions, name, None) is not True:
                return decision("DESTINATION_FORBIDDEN", "destination_bot_" + name, stage)
        stage = "source_bot_permissions"
        if source.permissions_for(bot_member).view_channel is not True:
            return decision("UNKNOWN_PERMISSIONS", "source_bot_view_channel", stage)
        stage = "everyone_permissions"
        everyone = guild.default_role
        if everyone is None:
            return decision("UNKNOWN_PERMISSIONS", "everyone_role_unavailable", stage)
        everyone_view = getattr(everyone.permissions, "view_channel", None)
        everyone_admin = getattr(everyone.permissions, "administrator", None)
        if not isinstance(everyone_view, bool) or not isinstance(everyone_admin, bool):
            return decision("UNKNOWN_PERMISSIONS", "everyone_permissions_unavailable", stage)
        stage = "role_profiles"
        profiles = _role_profiles(source, destination, guild)
        known_profiles = _role_profiles(source, destination, guild, include_unknown=False)
        missing_roles = _unknown_role_ids(source, destination, guild)
        stage = "member_overwrites"
        source_members, destination_members = _member_overwrites(source), _member_overwrites(destination)
        # Administrator ignores all channel overwrites in both channels.
        def combinations_for(values):
            yield ()
            yield from ((profile,) for profile in values)
            yield from ((first, second) for index, first in enumerate(values) for second in values[index + 1:])

        stage = "everyone_overwrites"
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

        def cases_for(values):
            return {(everyone_admin or any(item[0] for item in combination), visible(combination),
                     visible(combination, destination_side=True)) for combination in combinations_for(values)}

        stage = "role_containment"
        cases = cases_for(profiles)
        known_cases = cases_for(known_profiles)
        for _, source_view, destination_view in cases:
            if destination_view and not source_view:
                if missing_roles and not any(destination_view and not source_view for _, source_view, destination_view in known_cases):
                    return decision("UNKNOWN_PERMISSIONS", "role_cache_incomplete", stage)
                return decision("PRIVATE_VERIFIED", "source_reader_scope_exceeded", stage)
        stage = "member_containment"
        for member_id in source_members.keys() | destination_members.keys():
            if member_id in (getattr(guild, "owner_id", None), getattr(bot_member, "id", None)):
                # Guild owners bypass overwrites. The bot was checked against
                # both channels above, including when supplied by a REST fetch.
                continue
            member = source_members.get(member_id) or destination_members.get(member_id)
            resolver = getattr(guild, "get_member", None)
            actual = resolver(member_id) if callable(resolver) else None
            if not isinstance(actual, discord.Member) and isinstance(member, discord.Member):
                actual = member
            if isinstance(actual, discord.Member):
                destination_view = destination.permissions_for(actual).view_channel
                source_view = source.permissions_for(actual).view_channel
                if not isinstance(destination_view, bool) or not isinstance(source_view, bool):
                    return decision("UNKNOWN_PERMISSIONS", "member_permissions_unavailable", stage)
                if destination_view and not source_view:
                    return decision("PRIVATE_VERIFIED", "source_member_scope_exceeded", stage)
            else:
                source_override = _view_override(source, member_id, role=False)
                destination_override = _view_override(destination, member_id, role=False)
                for administrator, source_view, destination_view in cases:
                    source_view = source_view if administrator or source_override is None else source_override
                    destination_view = destination_view if administrator or destination_override is None else destination_override
                    if destination_view and not source_view:
                        return decision("UNKNOWN_PERMISSIONS", "source_member_roles_unavailable", stage)
    except Exception as error:
        reason_code = error.reason_code if isinstance(error, _PermissionEvidenceError) else "permission_evaluation_failed"
        return decision("UNKNOWN_PERMISSIONS", reason_code, stage, type(error).__name__)
    return decision("PUBLIC_VERIFIED", "reader_containment_verified", "complete")


def public_destination_reason(source: Any, destination: Any, guild_id: int) -> str | None:
    """Compatibility wrapper; new delivery/diagnostics consume the decision."""
    result = evaluate_public_destination(source, destination, guild_id)
    if result.allowed:
        return None
    if result.reason_code == "source_member_roles_unavailable":
        return "source_member_scope_exceeded"
    if result.reason_code == "source_guild_mismatch":
        return "destination_guild_mismatch"
    if result.status == "UNKNOWN_PERMISSIONS" and result.reason_code not in {"bot_member_unavailable", "source_bot_view_channel"}:
        return "source_permissions_unavailable"
    return result.reason_code


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


class _DeliveryPolicyChanged(Exception):
    def __init__(self, reason_code: str, stage: str = "policy", *, skipped: bool = False):
        self.reason_code = reason_code
        self.stage = stage
        self.skipped = skipped
        super().__init__(reason_code)


class DeletionAuditService:
    def __init__(self, routes: Mapping[int, int], channel_resolver: Callable[[int], Any], *,
                 include_content: bool | None = None, configured_admin_ids: Iterable[int] | None = None,
                 cache: MessageAuditCache | None = None, clock: Callable[[], float] = monotonic,
                 delivery_limit: int = 4096, delivery_ttl: float = 600, concurrency: int = 4, pending_limit: int = 64,
                 wall_clock: Callable[[], datetime] | None = None, guild_resolver: Callable[[int], Any] | None = None,
                 enabled: bool | None = None, all_guild_channels: bool | None = None,
                 channel_fetcher: Callable[[int], Any] | None = None,
                 bot_member_fetcher: Callable[[Any], Any] | None = None,
                 retry_attempts: int = 2, max_retry_after: float = 30.0,
                 retry_sleep: Callable[[float], Any] = asyncio.sleep):
        self.routes = dict(routes)
        self.channel_resolver = channel_resolver
        self.cache = cache if cache is not None else MessageAuditCache()
        raw = os.getenv("DELETION_PUBLIC_LOG_INCLUDE_CONTENT", "1").strip()
        self.include_content = raw == "1" if include_content is None else include_content is True
        self.enabled = os.getenv("DELETION_PUBLIC_LOG_ENABLED", "1").strip() == "1" if enabled is None else enabled is True
        self.all_guild_channels = os.getenv("DELETION_LOG_ALL_GUILD_CHANNELS", "1").strip() == "1" if all_guild_channels is None else all_guild_channels is True
        self.extra_sources, self.extra_sources_valid = _configured_ids(os.getenv("DELETION_LOG_EXTRA_SOURCE_CHANNEL_IDS", ""))
        private_ids, self.private_full_sources_valid = _configured_ids(
            os.getenv("DELETION_PUBLIC_LOG_PRIVATE_FULL_SOURCE_IDS", ""))
        # An extra/all-guild source is NOT the operator's legacy LOG_ROUTES
        # consent. Destination self-routes cannot opt themselves into auditing.
        self.private_full_sources = private_ids.intersection(self.routes).difference(self.routes.values())
        self.guild_resolver = guild_resolver
        # The gateway channel cache can be incomplete even for configured
        # destination IDs. A bounded REST fallback avoids silent lost logs.
        self.channel_fetcher = channel_fetcher
        self.bot_member_fetcher = bot_member_fetcher
        self.retry_attempts = max(0, min(3, retry_attempts))
        self.max_retry_after = max(0.0, min(60.0, max_retry_after))
        self.retry_sleep = retry_sleep
        self._fetched_channels: OrderedDict[int, tuple[float, Any]] = OrderedDict()
        self._channel_errors: OrderedDict[int, str] = OrderedDict()
        self._bot_members: OrderedDict[int, tuple[float, Any]] = OrderedDict()
        self._resolve_lock = asyncio.Lock()
        self._member_lock = asyncio.Lock()
        self.admin_ids = None if configured_admin_ids is None else frozenset(configured_admin_ids)
        self.clock, self.delivery_limit, self.delivery_ttl = clock, max(1, delivery_limit), max(1, delivery_ttl)
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._delivered: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._deleted: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._inflight: set[tuple[int, int]] = set()
        self.pending_limit = max(1, pending_limit)
        self._attempts: dict[tuple[int, int], _DeleteAttempt] = {}
        # Overflow correlation stores only IDs/timestamps, not extra bodies.
        self._deleting: dict[tuple[int, int], datetime | None] = {}
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._stats: dict[int, dict[str, Any]] = {}

    def _stat(self, guild_id: int) -> dict[str, Any]:
        return self._stats.setdefault(guild_id, {
            "sent": 0, "failures": 0, "sent_full": 0, "sent_metadata_only": 0,
            "sent_restricted": 0, "failed": 0, "skipped": 0, "dropped": 0,
            "last_success": None, "last_full": None, "last_test": None,
            "test_result": "not_tested", "last_reason": None, "last_stage": None,
            "last_decision": None, "reasons": Counter(),
            "public_deletion_status": "check_required", "latest_public_outcome": None,
            "last_permission_reason": None, "last_permission_stage": None,
        })

    def _record(self, guild_id: int, reason: str, *, failure: bool = False) -> None:
        stats = self._stat(guild_id)
        stats["last_reason"] = reason
        stats["reasons"][reason] += 1
        if failure:
            stats["failures"] += 1
            stats["failed"] += 1

    def _event(self, guild_id: int, message_id: int, channel_id: int, *, result: str,
               reason: str, stage: str, destination_id: int | None = None,
               error_type: str | None = None) -> None:
        """Only fixed codes, numeric identifiers and exception types enter logs."""
        self._record(guild_id, reason, failure=result == "failed")
        stats = self._stat(guild_id)
        stats["last_stage"] = stage
        if result in {"sent_full", "sent_metadata_only", "sent_restricted", "skipped"}:
            stats[result] += 1
        if result == "sent_full":
            stats["sent"] += 1  # Legacy success means FULL audit only.
            stats["last_full"] = stats["last_success"] = self._observation_time()
        if result == "failed" or (result == "sent_restricted" and reason == "unknown_permissions") or reason == "burst_limit":
            stats["public_deletion_status"] = "failed"
            stats["latest_public_outcome"] = result
        elif result in {"sent_full", "sent_metadata_only"}:
            stats["public_deletion_status"] = "confirmed" if result == "sent_full" else "check_required"
            stats["latest_public_outcome"] = result
        log = LOGGER.warning if result == "failed" else LOGGER.info
        log("event=deletion_audit result=%s reason=%s stage=%s guild_id=%s message_id=%s "
            "source_channel_id=%s destination_channel_id=%s exception_type=%s",
            result, reason, stage, guild_id, message_id, channel_id, destination_id, error_type or "none")

    def _known_channel(self, channel_id: int) -> Any:
        channel = self._resolve_cached(channel_id)
        if channel is not None:
            return channel
        cached = self._fetched_channels.get(channel_id)
        return cached[1] if cached is not None and self.clock() - cached[0] < 30 else None

    def invalidate_channel(self, channel_id: int) -> None:
        """Invalidate bounded REST evidence, including synced category children."""
        # At most 64 entries. Clearing them also covers copied category
        # overwrites, channel moves and destinations whose readership changed.
        self._fetched_channels.clear()
        self._channel_errors.clear()

    def _resolve_cached(self, channel_id: int) -> Any:
        try:
            channel = self.channel_resolver(channel_id)
            if inspect.isawaitable(channel):
                if inspect.iscoroutine(channel):
                    channel.close()
                return None
            return channel
        except Exception as error:
            LOGGER.warning("event=deletion_audit_channel_resolve result=failed reason=cache_resolver_failed "
                           "stage=cache_resolve channel_id=%s exception_type=%s", channel_id, type(error).__name__)
            return None

    async def _resolve_channel(self, channel_id: int) -> Any:
        # One REST lookup at a time, with a second cache check: a RAW burst
        # must not fetch the same missing channel once per message.
        async with self._resolve_lock:
            channel = self._known_channel(channel_id)
            if channel is not None:
                return channel
            previous = self._fetched_channels.get(channel_id)
            if previous is not None and self.clock() - previous[0] < 30:
                return previous[1]
            if self.channel_fetcher is None:
                return None
            fetched = None
            try:
                fetched = self.channel_fetcher(channel_id)
                if inspect.isawaitable(fetched):
                    fetched = await fetched
                if fetched is not None and getattr(fetched, "id", None) != channel_id:
                    self._channel_errors[channel_id] = "channel_id_mismatch"
                    fetched = None
                else:
                    self._channel_errors.pop(channel_id, None)
            except Exception as exc:
                self._channel_errors[channel_id] = self._delivery_error(exc)
                LOGGER.warning("event=deletion_audit_channel_resolve result=failed reason=%s stage=channel_fetch "
                               "channel_id=%s exception_type=%s", self._delivery_error(exc), channel_id, type(exc).__name__)
            # Cache positive/negative results briefly; never persist content.
            self._fetched_channels[channel_id] = (self.clock(), fetched)
            self._fetched_channels.move_to_end(channel_id)
            while len(self._fetched_channels) > 64:
                expired_id, _ = self._fetched_channels.popitem(last=False)
                self._channel_errors.pop(expired_id, None)
            return fetched

    async def _bot_member(self, guild: Any) -> Any:
        if guild is None:
            return None
        member = getattr(guild, "me", None)
        if member is not None:
            return member
        async with self._member_lock:
            previous = self._bot_members.get(guild.id)
            if previous is not None and self.clock() - previous[0] < 30:
                return previous[1]
            member = None
            if self.bot_member_fetcher is not None:
                try:
                    member = self.bot_member_fetcher(guild)
                    if inspect.isawaitable(member):
                        member = await member
                    if getattr(getattr(member, "guild", guild), "id", None) != guild.id:
                        member = None
                except Exception as exc:
                    LOGGER.warning("event=deletion_audit_member_resolve result=failed reason=bot_member_fetch_failed "
                                   "stage=bot_member_fetch guild_id=%s exception_type=%s", guild.id, type(exc).__name__)
                    self._record(guild.id, "bot_member_fetch_failed")
            self._bot_members[guild.id] = (self.clock(), member)
            self._bot_members.move_to_end(guild.id)
            while len(self._bot_members) > 16:
                self._bot_members.popitem(last=False)
            return member

    async def probe_configured_destinations(self) -> dict[int, str]:
        """Startup diagnostics for the EXISTING hard-coded record channels.

        No messages or user content are sent and no new channel is created.
        """
        results: dict[int, str] = {}
        for destination_id in sorted(set(self.routes.values())):
            channel = await self._resolve_channel(destination_id)
            if getattr(channel, "id", None) != destination_id:
                reason = "channel_missing_or_forbidden"
            elif not isinstance(channel, discord.TextChannel):
                reason = "destination_not_text_channel"
            else:
                try:
                    guild = channel.guild
                    bot_member = await self._bot_member(guild)
                    perms = channel.permissions_for(bot_member) if bot_member is not None else None
                    missing = next((name for name in ("view_channel", "send_messages", "embed_links")
                                    if getattr(perms, name, None) is not True), None)
                    reason = "destination_bot_" + missing if missing else "ready"
                except Exception:
                    reason = "destination_permissions_unavailable"
            results[destination_id] = reason
            LOGGER.info("event=deletion_audit_destination_probe destination_id=%s result=%s",
                        destination_id, reason)
        return results

    async def _async_destination_ids(self, guild_id: int) -> set[int]:
        ids = self._destination_ids(guild_id)
        for destination_id in set(self.routes.values()):
            destination = await self._resolve_channel(destination_id)
            if (getattr(destination, "id", None) == destination_id and
                    getattr(getattr(destination, "guild", None), "id", None) == guild_id):
                ids.add(destination_id)
        return ids

    def _destination_ids(self, guild_id: int) -> set[int]:
        destinations = set()
        for destination_id in set(self.routes.values()):
            destination = self._resolve_cached(destination_id)
            if destination is None:
                cached = self._fetched_channels.get(destination_id)
                if cached is not None and self.clock() - cached[0] < 30:
                    destination = cached[1]
            if (getattr(destination, "id", None) == destination_id and
                    getattr(getattr(destination, "guild", None), "id", None) == guild_id):
                destinations.add(destination_id)
        return destinations

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
        destination = self._known_channel(destination_id)
        if getattr(destination, "id", None) != destination_id:
            return False
        previous = self._bot_members.get(guild_id)
        member = previous[1] if previous is not None and self.clock() - previous[0] < 30 else None
        member = member if member is not None else getattr(destination.guild, "me", None)
        if self._record_destination_reason(destination, guild_id, destination_id, member) is not None:
            return False
        if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
            return False
        return channel_id in self.routes or evaluate_public_destination(channel, destination, guild_id, bot_member=member).allowed

    @staticmethod
    def _record_destination_reason(destination: Any, guild_id: int, destination_id: int,
                                   bot_member: Any) -> str | None:
        """Validate transport independently of the source disclosure policy."""
        if destination is None:
            return "destination_missing"
        if getattr(destination, "id", None) != destination_id:
            return "destination_id_mismatch"
        if not isinstance(destination, discord.TextChannel):
            return "destination_not_text_channel"
        if getattr(getattr(destination, "guild", None), "id", None) != guild_id:
            return "destination_guild_mismatch"
        if bot_member is None:
            return "bot_member_unavailable"
        member_guild_id = getattr(getattr(bot_member, "guild", None), "id", None)
        if isinstance(member_guild_id, int) and member_guild_id != guild_id:
            return "bot_member_guild_mismatch"
        try:
            permissions = destination.permissions_for(bot_member)
            for name in ("view_channel", "send_messages", "embed_links"):
                if getattr(permissions, name, None) is not True:
                    return "destination_bot_" + name
        except Exception:
            return "destination_permissions_unavailable"
        return None

    async def refresh_diagnostics(self, guild_or_id: Any) -> dict[str, Any]:
        guild_id = getattr(guild_or_id, "id", guild_or_id)
        ids = await self._async_destination_ids(guild_id)
        for destination_id in ids:
            destination = await self._resolve_channel(destination_id)
            await self._bot_member(getattr(destination, "guild", None))
        return self.diagnostics(guild_or_id)

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
        destination_id = next(iter(destinations)) if len(destinations) == 1 else None
        destination = self._known_channel(destination_id) if destination_id is not None else None
        permissions = None
        if destination is not None:
            try:
                member = destination.guild.me
                previous = self._bot_members.get(guild_id)
                if member is None and previous is not None and self.clock() - previous[0] < 30:
                    member = previous[1]
                permissions = destination.permissions_for(member) if member is not None else None
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
                "destination_ids": tuple(sorted(destinations)),
                "destination_resolution": ("cache" if destination_id is not None and self._resolve_cached(destination_id) is not None
                                           else "rest" if destination is not None else "failed"),
                "bot_view_channel": getattr(permissions, "view_channel", False) is True,
                "bot_send_messages": getattr(permissions, "send_messages", False) is True,
                "bot_embed_links": getattr(permissions, "embed_links", False) is True,
                "enabled": self.enabled, "include_content": self.include_content,
                "all_guild_channels": self.all_guild_channels, "extra_sources_valid": self.extra_sources_valid,
                "private_full_sources_valid": self.private_full_sources_valid,
                "private_full_source_count": sum(getattr(getattr(self._known_channel(source_id), "guild", None), "id", None) == guild_id
                                                 for source_id in self.private_full_sources),
                "configured_source_count": sum(getattr(getattr(self._resolve_cached(source_id), "guild", None), "id", None) == guild_id
                                               for source_id in self.routes),
                "extra_source_count": sum(getattr(getattr(self._resolve_cached(source_id), "guild", None), "id", None) == guild_id
                                           for source_id in self.extra_sources),
                "eligible_source_count": tracked, "excluded_source_count": max(0, len(channels) - tracked),
                "sent": stats["sent"], "failures": stats["failures"],
                **{name: stats[name] for name in ("sent_full", "sent_metadata_only", "sent_restricted", "failed", "skipped", "dropped")},
                "last_full": stats["last_full"], "last_full_success": stats["last_full"],
                "last_test": stats["last_test"], "test_result": stats["test_result"],
                "last_stage": stats["last_stage"], "last_decision": stats["last_decision"],
                "last_permission_reason": stats["last_permission_reason"], "last_permission_stage": stats["last_permission_stage"],
                "public_deletion_status": stats["public_deletion_status"],
                "latest_public_outcome": stats["latest_public_outcome"],
                "last_success": stats["last_success"], "last_reason": stats["last_reason"],
                "reason_counts": dict(stats["reasons"])}

    async def send_test(self, guild_or_id: Any) -> dict[str, Any]:
        """Send one synthetic card to this guild's existing destination only."""
        if hasattr(guild_or_id, "guild"):
            guild_or_id = guild_or_id.guild
        status = self.diagnostics(guild_or_id)
        guild_id = getattr(guild_or_id, "id", guild_or_id)

        def result(reason: str, *, sent: bool = False) -> dict[str, Any]:
            stats = self._stat(guild_id)
            stats["test_result"] = reason
            stats["last_test"] = self._observation_time()
            return {**self.diagnostics(guild_or_id), "result": "ready" if sent else "failed",
                    "reason": reason, "deletion_pipeline_verified": False}

        if not self.enabled:
            return result("disabled")
        destination_ids = await self._async_destination_ids(guild_id)
        if len(destination_ids) != 1:
            reason = "destination_ambiguous" if destination_ids else status["reason"]
            if reason == "ready":
                reason = "destination_missing"
            return result(reason)
        destination_id = next(iter(destination_ids))
        destination = await self._resolve_channel(destination_id)
        if getattr(destination, "id", None) != destination_id:
            return result("destination_id_mismatch")
        if getattr(getattr(destination, "guild", None), "id", None) != guild_id:
            return result("destination_guild_mismatch")
        try:
            bot_member = await self._bot_member(destination.guild)
            if bot_member is None:
                return result("bot_member_unavailable")
            perms = destination.permissions_for(bot_member)
            for required in ("view_channel", "send_messages", "embed_links"):
                if getattr(perms, required, None) is not True:
                    return result("destination_bot_" + required)
        except Exception:
            return result("destination_permissions_unavailable")
        embed = discord.Embed(title="🧪 삭제 로그 전송 테스트", description=(
            "기존 공용 기록 채널 전송 확인입니다.\n실제 사용자 원문·파일은 포함하지 않습니다.\n"
            "실제 삭제 파이프라인은 별도 일반 삭제·자동 검열로 확인해야 합니다.\n"
            f"확인 시각: {self._observation_time().astimezone(KST):%Y-%m-%d %H:%M:%S} KST"), colour=0xED4245)
        try:
            await destination.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self._record(guild_id, "test_sent")
            return result("test_sent", sent=True)
        except Exception as error:
            reason = self._delivery_error(error)
            self._stat(guild_id)["test_result"] = reason
            self._record(guild_id, reason)
            LOGGER.warning("event=deletion_audit_test result=failed reason=%s guild_id=%s", reason, guild_id)
            return result(reason)

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
        if not self.enabled or not guild_id or not channel_id or channel_id in self.routes.values():
            return False
        if channel_id in self.routes:
            return True
        # Explicit and all-guild sources proceed to bounded async resolution
        # even when source/destination gateway caches have not been populated.
        if channel_id in self.extra_sources:
            return True
        if self.all_guild_channels:
            # Admission is separate from async resolution and privacy proof.
            # RAW events and on_message snapshots must survive cold caches.
            return True
        return False

    def _admit(self, guild_id: int, channel_id: int, message_id: int, *, bot: bool = False,
               deletion: bool = False) -> bool:
        if bot or not self._routed(guild_id, channel_id):
            reason = ("ignored_log_channel" if channel_id in self.routes.values() else
                      "ignored_bot_message" if bot else "disabled" if not self.enabled else "not_routed")
            if deletion:
                self._event(guild_id, message_id, channel_id, result="skipped", reason=reason, stage="admission")
            else:
                # Ignored new messages must not replace the most recent actual
                # deletion reason or inflate the deletion outcome counters.
                self._stat(guild_id)["reasons"][reason] += 1
            return False
        return True

    def remember(self, message: Any) -> MessageAuditSnapshot | None:
        guild_id = getattr(getattr(message, "guild", None), "id", 0)
        channel_id = getattr(getattr(message, "channel", None), "id", 0)
        if not self._admit(guild_id, channel_id, getattr(message, "id", 0), bot=getattr(getattr(message, "author", None), "bot", False)):
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
        except Exception as error:
            self._event(guild_id, getattr(message, "id", 0), channel_id, result="failed",
                        reason="snapshot_capture_failed", stage="snapshot", error_type=type(error).__name__)
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
        if key in self._deleting:
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
            self._event(*key, snapshot.channel_id, result="skipped", reason="attempt_capacity", stage="delete")
        correlated = snapshot is not None and len(self._deleting) < self.delivery_limit
        if correlated:
            self._deleting[key] = None
        elif snapshot is not None:
            self._stat(key[0])["dropped"] += 1
            self._event(*key, snapshot.channel_id, result="failed", reason="delete_correlation_limit", stage="delete")
            self.cache.remember(original)
            self._attempts.pop(key, None)
            attempt = None
            snapshot = None
        # Snapshot and reason exist before the HTTP await, since a gateway
        # deletion can arrive before Discord's HTTP response is processed.
        try:
            deleted = await delete_message(message, reason=_code(reason), logger=LOGGER)
            observed_at = self._observation_time()
        except BaseException:
            self._deleting.pop(key, None)
            if attempt is not None:
                self._attempts.pop(key, None)
                self.cache.remember(attempt.original)
            elif original is not None:
                current = self.cache.get(*key) or original
                self.cache.remember(replace(current, reason=original.reason, delete_origin=original.delete_origin))
            raise
        overflow_observed_at = self._deleting.pop(key, None)
        if attempt is None and snapshot is not None:
            # A lightweight overflow reservation suppresses premature gateway
            # publication. Keep any newer cached edit/tombstone, then restore
            # attribution only after the delete API confirms success.
            current = self.cache.get(*key) or snapshot
            if deleted:
                snapshot = replace(current, reason=snapshot.reason, delete_origin="bot", trigger=snapshot.trigger)
            else:
                current = replace(current, reason=original.reason, delete_origin=original.delete_origin)
                self.cache.remember(current)
                if overflow_observed_at is not None:
                    await self._confirmed(current, observed_at=overflow_observed_at)
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
            await self._publish(snapshot, deleted_at=(attempt.gateway_observed_at if attempt is not None else overflow_observed_at) or observed_at)
        return deleted

    async def on_message_delete(self, message: Any, *, _observed_at: datetime | None = None) -> None:
        observed_at = _observed_at or self._observation_time()
        guild_id = getattr(getattr(message, "guild", None), "id", 0)
        channel_id = getattr(getattr(message, "channel", None), "id", 0)
        if not self._admit(guild_id, channel_id, getattr(message, "id", 0), bot=getattr(getattr(message, "author", None), "bot", False), deletion=True):
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
        if not message_id or not self._admit(guild_id, channel_id, message_id, deletion=True):
            return
        snapshot = self.cache.get(guild_id, message_id)
        if snapshot is None:
            self._event(guild_id, message_id, channel_id, result="observed", reason="snapshot_not_found", stage="snapshot")
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
        key = (snapshot.guild_id, snapshot.message_id)
        if key in self._deleting:
            if self._deleting[key] is None:
                self._deleting[key] = observed_at
            return
        if not snapshot.reason.code and snapshot.content_available:
            snapshot = replace(snapshot, reason=replace(snapshot.reason, code="external"), delete_origin="external")
        await self._publish(snapshot, deleted_at=observed_at)

    def _retry_delay(self, error: Exception, attempt: int) -> float | None:
        status = getattr(error, "status", None)
        if status == 429:
            delay = getattr(error, "retry_after", None)
            if delay is None:
                headers = getattr(getattr(error, "response", None), "headers", {}) or {}
                delay = headers.get("Retry-After")
            try:
                delay = float(delay)
            except (TypeError, ValueError, OverflowError):
                return None
            return delay if math.isfinite(delay) and 0 <= delay <= self.max_retry_after else None
        if status in (500, 502, 503, 504) or isinstance(error, (TimeoutError, asyncio.TimeoutError, OSError)):
            return min(float(2 ** attempt), self.max_retry_after)
        return None

    async def _prepare_publication(self, snapshot: MessageAuditSnapshot, *, deleted_at: datetime,
                                   destination_id: int) -> tuple[Any, discord.Embed, str, str]:
        """Resolve transport, then apply separate legacy/full disclosure consent.

        Called again after every retry delay, since ACLs can change while an
        outbound request is rate-limited. A formerly full card can downgrade.
        """
        if destination_id not in self.routes.values():
            raise _DeliveryPolicyChanged("destination_not_configured", "routing")
        cached_destination = self._resolve_cached(destination_id)
        if cached_destination is not None and getattr(cached_destination, "id", None) != destination_id:
            raise _DeliveryPolicyChanged("destination_id_mismatch", "destination_resolve")
        channel = await self._resolve_channel(destination_id)
        if channel is None:
            error = self._channel_errors.get(destination_id)
            reason = ("destination_forbidden" if error == "delivery_forbidden" else
                      "destination_id_mismatch" if error == "channel_id_mismatch" else "destination_missing")
            raise _DeliveryPolicyChanged(reason, "destination_resolve")
        member = await self._bot_member(getattr(channel, "guild", None))
        failure = self._record_destination_reason(channel, snapshot.guild_id, destination_id, member)
        if failure is not None:
            raise _DeliveryPolicyChanged(failure, "destination_bot_permissions")
        if snapshot.channel_id in self.routes.values() or snapshot.channel_id == destination_id:
            raise _DeliveryPolicyChanged("destination_recursion", "routing", skipped=True)
        cached_source = self._resolve_cached(snapshot.channel_id)
        if cached_source is not None and getattr(cached_source, "id", None) != snapshot.channel_id:
            raise _DeliveryPolicyChanged("source_id_mismatch", "source_resolve")
        source = await self._resolve_channel(snapshot.channel_id)
        if source is None and self._channel_errors.get(snapshot.channel_id) == "channel_id_mismatch":
            raise _DeliveryPolicyChanged("source_id_mismatch", "source_resolve")
        if source is not None and getattr(getattr(source, "guild", None), "id", None) != snapshot.guild_id:
            raise _DeliveryPolicyChanged("source_guild_mismatch", "source_resolve")
        if source is not None and not isinstance(source, discord.TextChannel):
            raise _DeliveryPolicyChanged("source_not_text_channel", "source_resolve")
        decision = evaluate_public_destination(source, channel, snapshot.guild_id, bot_member=member)
        stats = self._stat(snapshot.guild_id)
        stats["last_decision"] = decision.status
        stats["last_permission_reason"], stats["last_permission_stage"] = decision.reason_code, decision.stage
        explicit = snapshot.channel_id in self.routes
        private_opt_in = explicit and snapshot.channel_id in self.private_full_sources
        if not explicit and not decision.allowed:
            # Automatic discovery is not consent to publish private activity,
            # even if the old V7 card did not identify the channel or author.
            reason = "private_unrouted_skipped" if decision.status == "PRIVATE_VERIFIED" else "unverified_unrouted_skipped"
            raise _DeliveryPolicyChanged(reason, decision.stage, skipped=True)
        full_allowed = decision.allowed or (private_opt_in and decision.status in {"PRIVATE_VERIFIED", "UNKNOWN_PERMISSIONS"})
        if not self.include_content:
            reason = "content_disabled"
        elif not full_allowed:
            reason = ("private_source" if decision.status == "PRIVATE_VERIFIED" else
                      "source_not_found" if source is None else "unknown_permissions")
        elif not snapshot.content_available:
            reason = "snapshot_stale" if "raw_edit" in snapshot.truncated_fields else "snapshot_not_found"
        else:
            reason = "private_full_opt_in" if not decision.allowed else "public_verified"
        outcome = "sent_full" if full_allowed and self.include_content and snapshot.content_available else "sent_metadata_only"
        # Disclosure consent and body availability are independent: a public
        # (or opted-in) snapshot may still have known media/rule metadata even
        # when Discord never supplied its body. Keep that useful detail while
        # truthfully classifying the delivery as sent_metadata_only.
        if full_allowed and self.include_content:
            embed = build_deletion_embed(snapshot, deleted_at=deleted_at, include_content=True)
        else:
            embed = build_metadata_deletion_embed(snapshot, deleted_at=deleted_at, reason_code=reason)
        return channel, embed, outcome, reason

    async def _send_with_retry(self, channel: Any, embed: discord.Embed, snapshot: MessageAuditSnapshot, *,
                               deleted_at: datetime, destination_id: int, outcome: str, reason: str) -> tuple[str, str]:
        for attempt in range(self.retry_attempts + 1):
            try:
                if attempt:
                    channel, embed, outcome, reason = await self._prepare_publication(
                        snapshot, deleted_at=deleted_at, destination_id=destination_id)
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return outcome, reason
            except Exception as error:
                if getattr(error, "status", None) == 429:
                    self._record(snapshot.guild_id, "rate_limited")
                delay = self._retry_delay(error, attempt)
                if attempt >= self.retry_attempts or delay is None:
                    raise
                self._event(snapshot.guild_id, snapshot.message_id, snapshot.channel_id, result="retrying",
                            reason="retry_scheduled", stage="send", destination_id=channel.id,
                            error_type=type(error).__name__)
                await self.retry_sleep(delay)

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
            self._event(*key, snapshot.channel_id, result="skipped", reason="duplicate", stage="deduplicate")
            return False
        if len(self._inflight) >= self.pending_limit:
            self._stat(snapshot.guild_id)["dropped"] += 1
            self._event(*key, snapshot.channel_id, result="skipped", reason="burst_limit", stage="queue")
            return False
        self._inflight.add(key)
        destination_id, stage = None, "route"

        def fail(reason: str, *, error_type: str | None = None) -> bool:
            self._event(*key, snapshot.channel_id, result="failed", reason=reason, stage=stage,
                        destination_id=destination_id, error_type=error_type)
            return False

        try:
            async with self._semaphore:
                destination_id = self.destination_for(snapshot.guild_id, snapshot.channel_id)
                if destination_id is None and (self.all_guild_channels or snapshot.channel_id in self.extra_sources):
                    candidates = await self._async_destination_ids(snapshot.guild_id)
                    if len(candidates) == 1:
                        destination_id = next(iter(candidates))
                    elif len(candidates) > 1:
                        return fail("destination_ambiguous")
                if destination_id is None:
                    return fail("destination_missing")
                stage = "embed"
                channel, embed, outcome, reason = await self._prepare_publication(
                    snapshot, deleted_at=deleted_at, destination_id=destination_id)
                stage = "send"
                outcome, reason = await self._send_with_retry(channel, embed, snapshot, deleted_at=deleted_at,
                                                               destination_id=destination_id, outcome=outcome, reason=reason)
                self._delivered[key] = self.clock()
                self._delivered.move_to_end(key)
                while len(self._delivered) > self.delivery_limit:
                    self._delivered.popitem(last=False)
                self._event(*key, snapshot.channel_id, result=outcome, reason=reason,
                            stage="complete", destination_id=destination_id)
                return True
        except Exception as error:
            # No repr/str/traceback: Discord exceptions may contain message data.
            if isinstance(error, _DeliveryPolicyChanged):
                stage = error.stage
                if error.skipped:
                    self._event(*key, snapshot.channel_id, result="skipped", reason=error.reason_code,
                                stage=stage, destination_id=destination_id)
                    return False
                return fail(error.reason_code, error_type=type(error).__name__)
            return fail("embed_failed" if stage == "embed" else self._delivery_error(error), error_type=type(error).__name__)
        finally:
            self._inflight.discard(key)


__all__ = ["DeletionAuditService", "PublicDestinationDecision", "evaluate_public_destination",
           "build_deletion_embed", "build_metadata_deletion_embed", "build_restricted_deletion_embed",
           "private_destination_reason", "public_destination_reason"]
