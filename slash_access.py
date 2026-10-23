"""Read-only, local slash-command access diagnostics for the invoking member."""

from __future__ import annotations

from typing import Any

from bot_ui import admin_authorization_reason


_ADMIN_REASONS = frozenset({
    "guild_missing", "actor_missing", "special_id_unconfigured",
    "special_id_invalid", "special_id_mismatch", "guild_scope_invalid",
    "guild_scope_denied",
})


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
    if actor_view is False or actor_commands is False:
        lines.append("서버 운영진에게 서버 설정 → 역할 / 채널 권한 → 애플리케이션 명령어 사용을 확인해 달라고 요청해 주세요.")
    if bot_view is False or bot_send is False:
        lines.append("서버 운영진이 이 채널의 봇 보기·전송 권한도 확인해야 합니다.")
    lines.extend([
        "로컬 검사와 별개로 Discord 연동·명령별 허용·원격 게시 상태는 확인하지 못했습니다.",
        "일반 기능 대안: !ask 질문 · !img 설명 · !xp",
    ])
    return "\n".join(lines)
