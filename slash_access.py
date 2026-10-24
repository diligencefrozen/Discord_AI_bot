"""Read-only slash access diagnostics with bounded REST evidence for denials."""

from __future__ import annotations

import asyncio
import copy
import datetime
from typing import Any

import discord

from bot_ui import admin_authorization_reason


_ADMIN_REASONS = frozenset({
    "guild_missing", "actor_missing", "special_id_unconfigured",
    "special_id_invalid", "special_id_mismatch", "guild_scope_invalid",
    "guild_scope_denied",
})

# Only an explicit self-diagnostic can fetch these snapshots. The command's
# existing per-member cooldown and this process-wide bound limit API traffic.
_REFRESH_SLOTS = asyncio.Semaphore(2)
_REFRESH_TIMEOUT_SECONDS = 4.0
DIAGNOSTIC_VERSION = "2"
_CHANNEL_DENIAL_GUIDANCE = (
    "운영진은 채널 편집 → 권한 → 고급 권한과 동기화된 카테고리에서 본인의 역할·회원별 애플리케이션 명령어 사용 거부를 확인하고 해제해야 합니다.",
    "서버 역할의 기본 허용이나 앱·명령별 허용만으로 채널의 거부는 해제되지 않습니다.",
    "서버 소유자·Administrator는 채널 거부를 우회하므로 해당 계정에서 보인다고 일반 회원도 보이는 것은 아닙니다.",
)


def _get(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _snowflake(value: Any) -> int | None:
    return value if type(value) is int and 0 < value < 2**64 else None


def _permissions(channel: Any, member: Any) -> Any:
    if member is None:
        return None
    try:
        return channel.permissions_for(member)
    except Exception:
        return None


def _permission(permissions: Any, name: str) -> bool | None:
    value = _get(permissions, name)
    return value if type(value) is bool else None


def _state(value: bool | None) -> str:
    return {True: "허용", False: "거부", None: "확인 불가"}[value]


def _is_thread(channel: Any) -> bool:
    channel_type = _get(channel, "type")
    return _get(channel_type, "name") in {"news_thread", "public_thread", "private_thread"}


def _scope_valid(actor: Any, guild: Any, channel: Any) -> bool:
    guild_id = _snowflake(_get(guild, "id"))
    return (
        guild_id is not None
        and _snowflake(_get(actor, "id")) is not None
        and _snowflake(_get(channel, "id")) is not None
        and _snowflake(_get(_get(actor, "guild"), "id")) == guild_id
        and _snowflake(_get(_get(channel, "guild"), "id")) == guild_id
    )


class _SnapshotUnavailable(Exception):
    """A fixed internal code, never an API exception body or user content."""


def _overwrite_state(value: bool | None) -> str:
    return "통과" if value is None else _state(value)


def _snapshot_basis(member: discord.Member, channel: Any) -> str:
    """Summarise only this member's permission layers, without names or IDs."""
    guild = member.guild
    if member.id == guild.owner_id:
        return "새 조회 근거: 서버 소유자 권한"
    base = guild.default_role.permissions.value
    for role in member.roles:
        base |= role.permissions.value
    if discord.Permissions(base).administrator:
        return "새 조회 근거: Administrator 권한"
    everyone = channel.overwrites_for(guild.default_role).use_application_commands
    allows = denies = False
    for role in member.roles:
        if role.id == guild.id:
            continue
        value = channel.overwrites_for(role).use_application_commands
        allows |= value is True
        denies |= value is False
    role_override = True if allows else False if denies else None
    member_override = channel.overwrites_for(member).use_application_commands
    return (
        f"새 조회 근거: 서버 기본 {_state(guild.default_role.permissions.use_application_commands)}"
        f" · 내 역할 기본 {_state(discord.Permissions(base).use_application_commands)}"
        f" · 채널 공통 {_overwrite_state(everyone)}"
        f" · 역할 우선 {_overwrite_state(role_override)}"
        f" · 회원 우선 {_overwrite_state(member_override)}"
        f" · 타임아웃 {'적용 중' if member.is_timed_out() else '없음'}"
    )


async def _fresh_permission_snapshot(actor: discord.Member, guild: discord.Guild, channel: Any) -> tuple[bool, str]:
    async with _REFRESH_SLOTS:
        results = await asyncio.gather(
            guild.fetch_roles(), guild.fetch_member(actor.id), guild.fetch_channel(channel.id),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        roles, member, fetched_channel = results
        if (not isinstance(member, discord.Member) or member.id != actor.id
                or not _scope_valid(member, guild, fetched_channel)
                or fetched_channel.id != channel.id
                or not isinstance(roles, list) or not 1 <= len(roles) <= 500
                or any(not isinstance(role, discord.Role) or role.guild.id != guild.id for role in roles)):
            raise _SnapshotUnavailable("snapshot_scope_invalid")
        role_map = {role.id: role for role in roles}
        if guild.id not in role_map or len(role_map) != len(roles) or any(role_id not in role_map for role_id in member._roles):
            raise _SnapshotUnavailable("snapshot_roles_incomplete")

        # REST fetches do not replace the gateway's permission caches. Rebind
        # copies to an isolated Guild so installed discord.py resolves the fresh
        # roles/overwrites; never assign to the original Guild/Member/Channel.
        shadow = copy.copy(guild)
        shadow._roles = {}
        for role in roles:
            cloned_role = copy.copy(role)
            cloned_role.guild = shadow
            shadow._roles[role.id] = cloned_role
        cloned_member = copy.copy(member)
        cloned_member.guild = shadow
        cloned_channel = copy.copy(fetched_channel)
        cloned_channel.guild = shadow
        shadow._channels = {cloned_channel.id: cloned_channel}
        shadow._members = {cloned_member.id: cloned_member}
        permission_channel = cloned_channel
        if isinstance(cloned_channel, discord.Thread):
            parent_id = _snowflake(cloned_channel.parent_id)
            if parent_id is None:
                raise _SnapshotUnavailable("thread_parent_unavailable")
            parent = await guild.fetch_channel(parent_id)
            if (not isinstance(parent, (discord.TextChannel, discord.ForumChannel))
                    or parent.id != parent_id or parent.guild.id != guild.id):
                raise _SnapshotUnavailable("thread_parent_unavailable")
            permission_channel = copy.copy(parent)
            permission_channel.guild = shadow
            shadow._channels[parent.id] = permission_channel
        elif not isinstance(cloned_channel, discord.abc.GuildChannel):
            raise _SnapshotUnavailable("snapshot_channel_unsupported")
        permissions = cloned_channel.permissions_for(cloned_member)
        basis = _snapshot_basis(cloned_member, permission_channel)
        return permissions.use_application_commands, f"{basis} · 채널 보기 {_state(permissions.view_channel)}"


async def slash_access_report_with_refresh(actor: Any, guild: Any, channel: Any) -> str:
    """Read-only evidence for a denied self-check, bounded to four seconds.

    REST permission evidence cannot prove Integrations overrides, publication,
    client picker visibility or successful command execution. No repair occurs.
    """
    report = slash_access_report(actor, guild, channel)
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    evidence = [f"진단 v{DIAGNOSTIC_VERSION} · 검사 시각 {stamp:%Y-%m-%d %H:%M:%S} KST"]
    bot_id = _snowflake(_get(_get(guild, "me"), "id"))
    if bot_id is not None:
        evidence.append(f"응답 봇 ID: {bot_id}")
    if not _scope_valid(actor, guild, channel):
        return report + "\n" + "\n".join(evidence)
    if _permission(_permissions(channel, actor), "use_application_commands") is not False:
        return report + "\n" + "\n".join(evidence)

    # Unsupported/test objects must not trigger arbitrary network-capable calls.
    if not isinstance(guild, discord.Guild) or not isinstance(actor, discord.Member):
        evidence.append("Discord 새 조회: 확인 불가 (snapshot_unavailable)")
    else:
        try:
            fresh, basis = await asyncio.wait_for(
                _fresh_permission_snapshot(actor, guild, channel), timeout=_REFRESH_TIMEOUT_SECONDS,
            )
            # Fresh evidence supersedes the cached-denial repair advice: a
            # denial can instead come from base permissions, view or timeout.
            report = "\n".join(line for line in report.splitlines() if line not in _CHANNEL_DENIAL_GUIDANCE)
            evidence.extend([f"Discord 새 조회 · 내 앱 명령 사용: {_state(fresh)}", basis])
            if fresh:
                evidence.append("캐시와 새 조회가 다릅니다. 이 차이만으로 명령 목록·실행 복구를 보장하지 않습니다.")
            else:
                evidence.append("운영진은 위 새 조회의 권한 단계·채널 보기·타임아웃 상태를 확인해 주세요.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = (
                "timeout" if isinstance(error, TimeoutError) else
                "forbidden" if isinstance(error, discord.Forbidden) else
                "not_found" if isinstance(error, discord.NotFound) else
                "http_error" if isinstance(error, discord.HTTPException) else
                str(error) if isinstance(error, _SnapshotUnavailable) else "refresh_failed"
            )
            evidence.append(f"Discord 새 조회: 확인 불가 ({code})")
    report = report.replace("내 애플리케이션 명령어 사용:", "캐시 · 내 애플리케이션 명령어 사용:", 1)
    return report + "\n" + "\n".join(evidence)


def slash_access_report(actor: Any, guild: Any, channel: Any) -> str:
    """Inspect only this member/current channel; never fetch or alter permissions.

    Discord integration overrides and remote command publication are not exposed
    by ``permissions_for``. Successful local checks therefore cannot guarantee
    that a command is visible in the Discord client.
    """
    if guild is None:
        return "서버 채널에서 !명령진단을 실행해 주세요. DM에서는 서버의 / 명령 권한을 확인할 수 없습니다."

    guild_id = _snowflake(_get(guild, "id"))
    channel_id = _snowflake(_get(channel, "id"))
    actor_guild_id = _snowflake(_get(_get(actor, "guild"), "id"))
    channel_guild_id = _snowflake(_get(_get(channel, "guild"), "id"))
    member_matches = guild_id is not None and actor_guild_id == guild_id
    channel_matches = guild_id is not None and channel_guild_id == guild_id
    scope_valid = member_matches and channel_matches and channel_id is not None

    actor_permissions = _permissions(channel, actor) if scope_valid else None
    bot_permissions = _permissions(channel, _get(guild, "me")) if scope_valid else None
    actor_view = _permission(actor_permissions, "view_channel")
    actor_commands = _permission(actor_permissions, "use_application_commands")
    bot_view = _permission(bot_permissions, "view_channel")
    send_permission = "send_messages_in_threads" if _is_thread(channel) else "send_messages"
    bot_send = _permission(bot_permissions, send_permission)
    send_label = "스레드 전송" if _is_thread(channel) else "메시지 전송"

    if scope_valid:
        try:
            reason = admin_authorization_reason(actor, guild)
        except Exception:
            reason = "authorization_unavailable"
        if reason is None:
            authorization = "허용"
        else:
            safe_reason = reason if isinstance(reason, str) and reason in _ADMIN_REASONS else "authorization_unavailable"
            authorization = f"비허용 ({safe_reason})"
    else:
        authorization = "확인 불가 (member_or_channel_unavailable)"

    lines = [
        "도리봇 / 명령 진단 · 현재 채널/본인 기준",
        f"서버 ID: {guild_id if guild_id is not None else '확인 불가'}",
        f"채널 ID: {channel_id if channel_id is not None else '확인 불가'}",
        f"내 채널 보기: {_state(actor_view)}",
        f"내 애플리케이션 명령어 사용: {_state(actor_commands)}",
        f"봇 채널 보기: {_state(bot_view)} · {send_label}: {_state(bot_send)}",
        f"도리봇 /관리 인증: {authorization}",
        "/시작 · /질문 · /그림 · /내정보는 관리자 권한이 필요하지 않습니다.",
    ]
    if not scope_valid:
        lines.append("현재 서버 회원/채널 정보를 확인하지 못했습니다. 해당 서버 채널에서 다시 실행해 주세요.")
    if actor_view is False:
        lines.append("서버 운영진에게 서버 설정 → 역할 / 채널 권한 → 애플리케이션 명령어 사용을 확인해 달라고 요청해 주세요.")
    if actor_commands is False:
        lines.extend(_CHANNEL_DENIAL_GUIDANCE)
    if bot_view is False or bot_send is False:
        lines.append("서버 운영진이 이 채널의 봇 보기·전송 권한도 확인해야 합니다.")
    lines.extend([
        "로컬 검사와 별개로 Discord 연동·명령별 허용·원격 게시 상태는 확인하지 못했습니다.",
        "일반 기능 대안: !ask 질문 · !img 설명 · !xp",
    ])
    return "\n".join(lines)
