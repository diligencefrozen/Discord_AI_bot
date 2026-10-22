"""Private Discord moderation tools with a second, authoritative service boundary.

Discord retains timeout/nickname/ban/slowmode state. Confirmations and the bounded
audit trail are deliberately process-local and expire on restart. No message body,
nickname, free-form reason, token or API exception is written to the audit trail.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
import os
import secrets
from time import monotonic
from typing import Any

import discord

from bot_ui import (
    UNAUTHORIZED_ADMIN_MESSAGE, _ephemeral, _no_mentions, _parse_snowflake,
    admin_authorization_reason, configured_special_admin_ids, log_admin_access,
)

TIMEOUT_MINUTES = (1, 5, 10, 60, 1440, 10080, 40320)
ACTION_LABELS = {
    "timeout": "채팅금지", "untimeout": "타임아웃 해제", "nickname": "서버 별명 변경",
    "reset_nickname": "서버 별명 초기화", "ban": "영구 밴", "unban": "밴 해제",
    "kick": "추방", "cleanup": "최근 메시지 정리", "slowmode": "채널 슬로우모드",
}
PERMISSIONS = {
    "timeout": "moderate_members", "untimeout": "moderate_members",
    "nickname": "manage_nicknames", "reset_nickname": "manage_nicknames",
    "ban": "ban_members", "unban": "ban_members", "kick": "kick_members",
    "cleanup": "manage_messages", "slowmode": "manage_channels",
}


class AdminActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


def _api_error(error: Exception) -> AdminActionError:
    if isinstance(error, discord.Forbidden):
        return AdminActionError("discord_forbidden", "Discord에서 작업을 거부했습니다. 봇 권한과 역할 순서를 확인해 주세요.")
    if isinstance(error, discord.NotFound):
        return AdminActionError("target_missing", "대상이 사라졌거나 이미 처리되었습니다. 대상을 다시 선택해 주세요.")
    if isinstance(error, discord.HTTPException):
        return AdminActionError("discord_http_error", "Discord 요청에 실패했습니다. 잠시 후 새 확인 화면에서 다시 시도해 주세요.")
    return AdminActionError("internal_error", "관리 작업을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")


def _safe_text(value: Any) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


async def _defer(interaction: Any) -> None:
    response = interaction.response
    if not response.is_done() and hasattr(response, "defer"):
        await response.defer(ephemeral=True, thinking=True)


async def _guard(interaction: Any, actor_id: int | None = None, guild_id: int | None = None) -> bool:
    denied = log_admin_access(interaction, source="component", event="admin_action") is not None
    denied = denied or (actor_id is not None and getattr(interaction.user, "id", None) != actor_id)
    denied = denied or (guild_id is not None and getattr(getattr(interaction, "guild", None), "id", None) != guild_id)
    if denied:
        await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE, allowed_mentions=_no_mentions())
    return not denied


@dataclass(frozen=True)
class AdminRequest:
    action: str
    actor_id: int
    guild_id: int
    channel_id: int | None
    target_id: int | None
    value: str | int | None
    reason: str
    nonce: str
    created: float
    before_value: str | None = None

    @property
    def preview(self) -> str:
        target = f"사용자 ID `{self.target_id}`" if self.target_id else f"채널 ID `{self.channel_id}`"
        value = ""
        if self.action == "timeout":
            value = f"\n기한: {self.value}분 · Discord에 만료 시각 저장"
        elif self.action in ("nickname", "reset_nickname"):
            value = (f"\n변경 전 서버 별명: {_safe_text(self.before_value) if self.before_value else '설정 없음'}"
                     f"\n변경 후 서버 별명: {_safe_text(self.value) if self.value else '기본 계정 표시명 사용'}")
        elif self.action == "cleanup":
            value = f"\n범위: 이 채널 최근 100개 중 대상의 메시지 최대 {self.value}개"
        elif self.action == "slowmode":
            value = f"\n간격: {self.value}초 (0초는 해제)"
        elif self.action == "ban":
            value = "\n기한: 영구 · 자동 해제 없음 · 기존 메시지는 보존"
        return (f"**{ACTION_LABELS[self.action]} 확인**\n대상: {target}{value}\n"
                f"사유: {_safe_text(self.reason)}\n\n아래 확인을 누르면 실행합니다. 확인 화면은 2분 후 만료됩니다.")


class AdminService:
    """Resolve current guild state and revalidate at prepare AND commit."""

    def __init__(self, *, audit_limit: int = 200):
        self.audit: deque[dict[str, Any]] = deque(maxlen=audit_limit)
        self._pending: OrderedDict[str, AdminRequest] = OrderedDict()
        self._claim_lock = asyncio.Lock()

    async def _member(self, guild: Any, member_id: int) -> Any:
        fetch = getattr(guild, "fetch_member", None)
        member = await fetch(member_id) if fetch else guild.get_member(member_id)
        if member is None or getattr(getattr(member, "guild", None), "id", None) != guild.id:
            raise AdminActionError("member_missing", "이 서버의 멤버를 선택해 주세요.")
        return member

    def _authorized(self, interaction: Any) -> None:
        reason = admin_authorization_reason(getattr(interaction, "user", None), getattr(interaction, "guild", None))
        if reason is not None:
            raise AdminActionError(reason, UNAUTHORIZED_ADMIN_MESSAGE)

    async def member(self, interaction: Any, target_id: int) -> Any:
        self._authorized(interaction)
        try:
            return await self._member(interaction.guild, target_id)
        except AdminActionError:
            raise
        except Exception as error:
            raise _api_error(error) from None

    @staticmethod
    def _has_permission(permissions: Any, name: str) -> bool:
        return bool(getattr(permissions, "administrator", False) or getattr(permissions, name, False))

    async def _validate(self, interaction: Any, request: AdminRequest) -> tuple[Any, Any]:
        self._authorized(interaction)
        guild = interaction.guild
        if guild.id != request.guild_id or interaction.user.id != request.actor_id:
            raise AdminActionError("context_mismatch", UNAUTHORIZED_ADMIN_MESSAGE)
        if request.action not in PERMISSIONS:
            raise AdminActionError("invalid_action", "지원하지 않는 관리 작업입니다.")
        if not request.reason or len(request.reason) > 200:
            raise AdminActionError("invalid_reason", "사유는 1~200자로 입력해 주세요.")
        action = request.action
        if action == "timeout" and (type(request.value) is not int or request.value not in TIMEOUT_MINUTES):
            raise AdminActionError("invalid_duration", "타임아웃은 제공된 1분~28일 항목에서 선택해 주세요.")
        if action == "nickname" and (not isinstance(request.value, str) or not 1 <= len(request.value) <= 32):
            raise AdminActionError("invalid_nickname", "서버 별명은 1~32자로 입력해 주세요.")
        if action == "cleanup" and (type(request.value) is not int or not 1 <= request.value <= 50):
            raise AdminActionError("invalid_count", "삭제 수는 1~50개로 입력해 주세요.")
        if action == "slowmode" and (type(request.value) is not int or not 0 <= request.value <= 21600):
            raise AdminActionError("invalid_slowmode", "슬로우모드는 0~21600초로 입력해 주세요.")

        actor = await self._member(guild, request.actor_id)
        bot_id = getattr(getattr(guild, "me", None), "id", None)
        if bot_id is None:
            raise AdminActionError("bot_member_missing", "봇의 서버 멤버 정보를 확인할 수 없습니다.")
        bot_member = await self._member(guild, bot_id)
        channel = getattr(interaction, "channel", None)
        if action in ("cleanup", "slowmode"):
            if (channel is None or getattr(channel, "id", None) != request.channel_id
                    or getattr(getattr(channel, "guild", None), "id", None) != guild.id):
                raise AdminActionError("channel_mismatch", "원래 서버 채널에서 다시 실행해 주세요.")
            if action == "slowmode" and not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise AdminActionError("unsupported_channel", "텍스트 채널 또는 스레드에서 실행해 주세요.")
            bot_permissions = channel.permissions_for(bot_member)
            actor_permissions = channel.permissions_for(actor)
        else:
            bot_permissions, actor_permissions = bot_member.guild_permissions, actor.guild_permissions
        permission = PERMISSIONS[action]
        if action == "slowmode" and isinstance(channel, discord.Thread):
            permission = "manage_threads"
        if not self._has_permission(bot_permissions, permission):
            raise AdminActionError("bot_permission_missing", "봇에 이 작업의 Discord 권한이 없습니다.")
        if request.actor_id != guild.owner_id and not self._has_permission(actor_permissions, permission):
            raise AdminActionError("actor_permission_missing", "도리봇 관리 인증 외에 이 작업의 Discord 권한도 필요합니다.")
        if action == "cleanup" and not all(self._has_permission(bot_permissions, name) for name in ("view_channel", "read_message_history")):
            raise AdminActionError("bot_history_permission_missing", "봇에 채널 보기와 메시지 기록 보기 권한이 필요합니다.")
        if action == "slowmode":
            return None, channel
        protected = {request.actor_id, bot_id, guild.owner_id, *configured_special_admin_ids()}
        if request.target_id is None or request.target_id in protected:
            raise AdminActionError("protected_target", "본인·봇·서버 소유자·도리봇 파딱은 이 작업의 대상이 될 수 없습니다.")
        if action == "unban":
            await guild.fetch_ban(discord.Object(id=request.target_id))
            return discord.Object(id=request.target_id), channel
        target = await self._member(guild, request.target_id)
        if self._has_permission(target.guild_permissions, "administrator"):
            raise AdminActionError("protected_administrator", "Discord 관리자 권한을 가진 멤버는 보호 대상입니다.")
        try:
            bot_above = bot_member.top_role > target.top_role
            actor_above = request.actor_id == guild.owner_id or actor.top_role > target.top_role
        except (AttributeError, TypeError):
            bot_above = actor_above = False
        if not bot_above or not actor_above:
            raise AdminActionError("role_hierarchy", "대상의 최상위 역할보다 봇과 실행자의 역할이 높아야 합니다.")
        return target, channel

    async def prepare(self, interaction: Any, action: str, *, target_id: int | None = None,
                      value: str | int | None = None, reason: str = "운영 정책에 따른 조치") -> AdminRequest:
        self._authorized(interaction)
        request = AdminRequest(action, interaction.user.id, interaction.guild.id,
                               getattr(getattr(interaction, "channel", None), "id", None),
                               target_id, value, reason.strip(), secrets.token_hex(16), monotonic())
        try:
            target, _ = await self._validate(interaction, request)
            if action in ("nickname", "reset_nickname"):
                request = replace(request, before_value=getattr(target, "nick", None))
        except AdminActionError:
            raise
        except Exception as error:
            raise _api_error(error) from None
        async with self._claim_lock:
            now = monotonic()
            self._pending = OrderedDict((key, item) for key, item in self._pending.items() if now - item.created <= 120)
            self._pending[request.nonce] = request
            while len(self._pending) > 1000:
                self._pending.popitem(last=False)
        return request

    async def _record(self, interaction: Any, request: AdminRequest, code: str) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor_id": request.actor_id,
                 "guild_id": request.guild_id, "target_id": request.target_id or request.channel_id,
                 "action": request.action, "result": code}
        self.audit.append(entry)
        logging.info("event=admin_execution guild_id=%s action=%s result=%s", request.guild_id, request.action, code)
        raw_id = os.getenv("MOD_LOG_CHANNEL_ID", "").strip()
        if not raw_id:
            return
        try:
            log_id = _parse_snowflake(raw_id)
            log_channel = interaction.guild.get_channel(log_id)
            if log_channel is not None:
                await log_channel.send("관리 기록 · " + " · ".join(f"{key}={value}" for key, value in entry.items()),
                                       allowed_mentions=_no_mentions())
        except Exception:
            logging.warning("event=admin_audit_delivery result=failed reason=log_channel_unavailable")

    async def execute(self, interaction: Any, request: AdminRequest) -> str:
        self._authorized(interaction)
        if interaction.guild.id != request.guild_id or interaction.user.id != request.actor_id:
            raise AdminActionError("context_mismatch", UNAUTHORIZED_ADMIN_MESSAGE)
        async with self._claim_lock:
            pending = self._pending.get(request.nonce)
            if pending is not request:
                raise AdminActionError("confirmation_used", "이미 처리되었거나 만료된 확인입니다. 새로 작업을 선택해 주세요.")
            self._pending.pop(request.nonce)
        try:
            if monotonic() - request.created > 120:
                raise AdminActionError("confirmation_expired", "확인 시간이 만료되었습니다. 새로 작업을 선택해 주세요.")
            target, channel = await self._validate(interaction, request)
            # Do not forward free-form reasons/nicknames to Discord's audit log.
            audit_reason = f"Doribot {request.action}; actor={request.actor_id}"
            action = request.action
            if action in ("timeout", "untimeout"):
                until = timedelta(minutes=request.value) if action == "timeout" else None
                await target.timeout(until, reason=audit_reason)
            elif action in ("nickname", "reset_nickname"):
                await target.edit(nick=request.value if action == "nickname" else None, reason=audit_reason)
            elif action == "ban":
                await interaction.guild.ban(target, reason=audit_reason, delete_message_seconds=0)
            elif action == "unban":
                await interaction.guild.unban(target, reason=audit_reason)
            elif action == "kick":
                await target.kick(reason=audit_reason)
            elif action == "cleanup":
                matched = 0

                def check(message: Any) -> bool:
                    nonlocal matched
                    if getattr(message.author, "id", None) == request.target_id and matched < request.value:
                        matched += 1
                        return True
                    return False

                # Individual deletion also supports messages older than 14 days.
                deleted = await channel.purge(limit=100, check=check, bulk=False, reason=audit_reason)
                await self._record(interaction, request, "success")
                return f"최근 메시지 {len(deleted)}개를 삭제했습니다."
            elif action == "slowmode":
                await channel.edit(slowmode_delay=request.value, reason=audit_reason)
            await self._record(interaction, request, "success")
            return f"{ACTION_LABELS[action]} 작업을 완료했습니다."
        except Exception as error:
            safe = error if isinstance(error, AdminActionError) else _api_error(error)
            await self._record(interaction, request, safe.code)
            raise safe from None

    def recent_log_text(self, guild_id: int) -> str:
        entries = [entry for entry in self.audit if entry["guild_id"] == guild_id][-15:]
        lines = ["**📋 최근 관리 작업**", "최근 15건 · 인메모리 최대 200건 · 봇 재시작 시 초기화"]
        for entry in entries:
            lines.append(f"{entry['at']} · 관리자 `{entry['actor_id']}` · 대상 `{entry['target_id']}` · {ACTION_LABELS[entry['action']]} · {entry['result']}")
        while len("\n".join(lines)) > 1900 and len(lines) > 2:
            lines.pop(2)
        return "\n".join(lines) if entries else "\n".join(lines) + "\n아직 관리 작업이 없습니다."


ADMIN_SERVICE = AdminService()


class _BoundView(discord.ui.View):
    def __init__(self, interaction: Any, service: AdminService):
        super().__init__(timeout=120)
        self.actor_id, self.guild_id = interaction.user.id, interaction.guild.id
        self.service = service

    async def interaction_check(self, interaction: Any) -> bool:
        return await _guard(interaction, self.actor_id, self.guild_id)

    async def on_error(self, interaction: Any, error: Exception, item: Any) -> None:
        safe = _api_error(error)
        logging.warning("event=admin_component result=failed reason=%s", safe.code)
        await _ephemeral(interaction, safe.message, allowed_mentions=_no_mentions())

    def button(self, label: str, custom_id: str, callback: Any, *, danger: bool = False) -> None:
        button = discord.ui.Button(label=label, custom_id=custom_id,
                                   style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.secondary)
        button.callback = callback
        self.add_item(button)


class AdminConfirmView(_BoundView):
    def __init__(self, interaction: Any, service: AdminService, request: AdminRequest):
        super().__init__(interaction, service)
        self.request = request
        self.button("확인 · 실행", "admin:confirm", self.confirm, danger=True)
        self.button("취소", "admin:cancel", self.cancel)

    async def confirm(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        await _defer(interaction)
        try:
            text = await self.service.execute(interaction, self.request)
        except AdminActionError as error:
            text = error.message
        for item in self.children:
            item.disabled = True
        await _ephemeral(interaction, text, allowed_mentions=_no_mentions())
        self.stop()

    async def cancel(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        async with self.service._claim_lock:
            self.service._pending.pop(self.request.nonce, None)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="관리 작업을 취소했습니다.", view=self,
                                                allowed_mentions=_no_mentions())
        self.stop()


class AdminActionModal(discord.ui.Modal):
    def __init__(self, interaction: Any, service: AdminService, action: str,
                 *, target_id: int | None = None, value: int | None = None):
        super().__init__(title=ACTION_LABELS.get(action, "사용자 선택"), timeout=120)
        self.actor_id, self.guild_id = interaction.user.id, interaction.guild.id
        self.service, self.action, self.target_id, self.action_value = service, action, target_id, value
        self.value_input = None
        if action in ("select", "unban", "nickname", "cleanup", "slowmode"):
            labels = {"select": "이 서버의 사용자 ID", "unban": "밴 해제할 사용자 ID", "nickname": "변경할 서버 별명 (1~32자)",
                      "cleanup": "삭제할 최대 개수 (1~50)", "slowmode": "슬로우모드 초 (0~21600, 0은 해제)"}
            self.value_input = discord.ui.TextInput(label=labels[action], max_length=32 if action == "nickname" else 20, required=True)
            self.add_item(self.value_input)
        self.reason_input = None
        if action != "select":
            self.reason_input = discord.ui.TextInput(label="조치 사유 (관리 확인에만 표시)", max_length=200,
                                                     default="운영 정책에 따른 조치", required=True)
            self.add_item(self.reason_input)

    async def on_submit(self, interaction: Any) -> None:
        if not await _guard(interaction, self.actor_id, self.guild_id):
            return
        await _defer(interaction)
        try:
            raw = str(self.value_input.value).strip() if self.value_input is not None else ""
            if self.action == "select":
                await send_member_actions(interaction, _parse_snowflake(raw), self.service)
                return
            target_id, value = self.target_id, self.action_value
            if self.action == "unban":
                target_id = _parse_snowflake(raw)
            elif self.action in ("cleanup", "slowmode"):
                if not raw.isascii() or not raw.isdecimal():
                    raise ValueError
                value = int(raw)
            elif self.action == "nickname":
                value = raw
            request = await self.service.prepare(interaction, self.action, target_id=target_id,
                                                 value=value, reason=str(self.reason_input.value))
            await _ephemeral(interaction, request.preview, view=AdminConfirmView(interaction, self.service, request),
                             allowed_mentions=_no_mentions())
        except ValueError:
            await _ephemeral(interaction, "올바른 사용자 ID 또는 숫자를 입력해 주세요.")
        except AdminActionError as error:
            await _ephemeral(interaction, error.message, allowed_mentions=_no_mentions())

    async def on_error(self, interaction: Any, error: Exception) -> None:
        logging.warning("event=admin_modal result=failed reason=%s", _api_error(error).code)
        await _ephemeral(interaction, _api_error(error).message, allowed_mentions=_no_mentions())


class AdminMemberSelectView(_BoundView):
    def __init__(self, interaction: Any, service: AdminService):
        super().__init__(interaction, service)
        self.select = discord.ui.UserSelect(custom_id="admin:member-select", placeholder="서버 멤버를 선택하세요", max_values=1)
        self.select.callback = self.select_member
        self.add_item(self.select)
        self.button("사용자 ID로 선택", "admin:member-id", self.manual)
        self.button("밴 해제 · ID 입력", "admin:unban", self.unban)

    async def select_member(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        data = getattr(interaction, "data", {}) or {}
        try:
            member_id = _parse_snowflake(str(data.get("values", [""])[0]))
        except (ValueError, IndexError):
            await _ephemeral(interaction, "서버 멤버를 다시 선택해 주세요.")
            return
        await _defer(interaction)
        await send_member_actions(interaction, member_id, self.service)

    async def manual(self, interaction: Any) -> None:
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(AdminActionModal(interaction, self.service, "select"))

    async def unban(self, interaction: Any) -> None:
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(AdminActionModal(interaction, self.service, "unban"))


class AdminMemberActionsView(_BoundView):
    def __init__(self, interaction: Any, service: AdminService, target_id: int):
        super().__init__(interaction, service)
        self.target_id = target_id
        select = discord.ui.Select(custom_id="admin:member-action", placeholder="이 멤버에게 실행할 작업",
                                   options=[discord.SelectOption(label=ACTION_LABELS[action], value=action)
                                            for action in ("timeout", "untimeout", "nickname", "reset_nickname", "ban", "kick", "cleanup")])
        select.callback = self.select_action
        self.add_item(select)

    async def select_action(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        values = (getattr(interaction, "data", {}) or {}).get("values", [])
        action = values[0] if values else ""
        if action == "timeout":
            await _ephemeral(interaction, "**⏳ 채팅금지 기간**\n사유 입력과 최종 확인 후 적용됩니다.",
                             view=AdminTimeoutView(interaction, self.service, self.target_id))
        elif action in ("untimeout", "nickname", "reset_nickname", "ban", "kick", "cleanup"):
            await interaction.response.send_modal(AdminActionModal(interaction, self.service, action, target_id=self.target_id))
        else:
            await _ephemeral(interaction, "작업을 다시 선택해 주세요.")


class AdminTimeoutView(_BoundView):
    def __init__(self, interaction: Any, service: AdminService, target_id: int):
        super().__init__(interaction, service)
        self.target_id = target_id
        select = discord.ui.Select(custom_id="admin:timeout-duration", placeholder="채팅금지 기간",
                                   options=[discord.SelectOption(label=label, value=str(minutes)) for label, minutes in
                                            zip(("1분", "5분", "10분", "1시간", "1일", "7일", "28일"), TIMEOUT_MINUTES)])
        select.callback = self.select_duration
        self.add_item(select)

    async def select_duration(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        values = (getattr(interaction, "data", {}) or {}).get("values", [])
        try:
            value = int(values[0])
            if value not in TIMEOUT_MINUTES:
                raise ValueError
        except (ValueError, IndexError, TypeError):
            await _ephemeral(interaction, "제공된 기간 중 하나를 선택해 주세요.")
            return
        await interaction.response.send_modal(AdminActionModal(interaction, self.service, "timeout", target_id=self.target_id, value=value))


class AdminChannelView(_BoundView):
    def __init__(self, interaction: Any, service: AdminService):
        super().__init__(interaction, service)
        self.button("슬로우모드 설정 / 해제", "admin:slowmode", self.slowmode)

    async def slowmode(self, interaction: Any) -> None:
        if await self.interaction_check(interaction):
            await interaction.response.send_modal(AdminActionModal(interaction, self.service, "slowmode"))


async def send_member_actions(interaction: Any, target_id: int, service: AdminService = ADMIN_SERVICE) -> None:
    if not await _guard(interaction):
        return
    try:
        member = await service.member(interaction, target_id)
        until = getattr(member, "timed_out_until", None)
        timeout = discord.utils.format_dt(until, style="f") if until and until > datetime.now(timezone.utc) else "없음"
        nickname = _safe_text(getattr(member, "nick", None) or "설정 없음")
        display = _safe_text(getattr(member, "display_name", target_id))
        admin = "있음" if getattr(member.guild_permissions, "administrator", False) else "없음"
        text = (f"**👤 {display}**\n사용자 ID `{target_id}` · 이 서버 별명: {nickname}\n"
                f"Discord 관리자 권한: {admin} · 타임아웃: {timeout}\n"
                "조치에 필요한 권한과 역할 순서는 실행 직전에 다시 확인합니다.")
        await _ephemeral(interaction, text, view=AdminMemberActionsView(interaction, service, target_id), allowed_mentions=_no_mentions())
    except AdminActionError as error:
        await _ephemeral(interaction, error.message, allowed_mentions=_no_mentions())


async def handle_admin_action(interaction: Any, action_id: str, *, status_text: str | None = None) -> bool:
    """Return whether a main.py admin route was handled; always reauthenticate."""
    if not await _guard(interaction):
        return True
    if action_id == "admin:members":
        await _ephemeral(interaction, "**👤 사용자 관리**\n이 서버 멤버를 선택하거나 사용자 ID를 입력하세요.",
                         view=AdminMemberSelectView(interaction, ADMIN_SERVICE), allowed_mentions=_no_mentions())
    elif action_id == "admin:channel":
        await _ephemeral(interaction, "**🛡️ 이 채널 관리**\n슬로우모드는 Discord에 저장됩니다. 0초로 해제할 수 있어요.",
                         view=AdminChannelView(interaction, ADMIN_SERVICE), allowed_mentions=_no_mentions())
    elif action_id == "admin:logs":
        await _ephemeral(interaction, ADMIN_SERVICE.recent_log_text(interaction.guild.id), allowed_mentions=_no_mentions())
    elif action_id == "admin:settings" and status_text is not None:
        await _ephemeral(interaction, status_text, allowed_mentions=_no_mentions())
    else:
        return False
    return True
