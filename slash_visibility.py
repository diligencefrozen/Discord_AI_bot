"""One startup audit of this application's five global commands, with safe repair."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import discord

LOGGER = logging.getLogger(__name__)
EXPECTED_NAMES = frozenset(("시작", "질문", "그림", "내정보", "관리"))
OBSOLETE_NAMES = frozenset(("ai질문",))
ACCESS_GATES = ("restricted_count", "context_mismatch_count", "install_mismatch_count", "age_restricted_count")
REPAIR_REASONS = ("missing_count", "obsolete_count", *ACCESS_GATES)


def slash_sync_mode() -> str:
    value = os.getenv("SLASH_SYNC_ON_STARTUP", "auto").strip().casefold()
    if value not in {"auto", "0", "1"}:
        LOGGER.warning("event=slash_sync_skipped reason=invalid_configuration scope=global")
        return "0"
    return value


def _chat_input(command: Any) -> bool:
    kind = getattr(command, "type", 1)
    return getattr(kind, "value", kind) == 1


def summarize_slash_visibility(commands: list[Any]) -> dict[str, Any]:
    names = {command.name for command in commands if _chat_input(command)}
    expected = [command for command in commands if _chat_input(command) and command.name in EXPECTED_NAMES]
    restricted = sum(getattr(command, "default_permissions", None) is not None
                     or getattr(command, "default_member_permissions", None) is not None
                     for command in expected)
    contexts = sum(
        context is not None and (not context.guild or context.private_channel or
            context.dm_channel != (command.name != "관리"))
        for command in expected for context in (getattr(command, "allowed_contexts", None),))
    installs = sum(install is not None and (not install.guild or install.user)
                   for command in expected for install in (getattr(command, "allowed_installs", None),))
    return {"count": len(commands), "missing_count": len(EXPECTED_NAMES - names),
            "obsolete_count": len(names & OBSOLETE_NAMES), "restricted_count": restricted,
            "context_mismatch_count": contexts, "install_mismatch_count": installs,
            "age_restricted_count": sum(getattr(command, "nsfw", False) is True for command in expected),
            "foreign_count": sum(not _chat_input(command) or command.name not in EXPECTED_NAMES | OBSOLETE_NAMES
                                 for command in commands)}


class SlashVisibilityAudit:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._guild_lock = asyncio.Lock()
        self.audited = False
        self.published = False
        self.mode: str | None = None
        self.last_reason = "not_audited"
        self.last_error: Exception | None = None
        self.summary: dict[str, Any] = {}
        self.sync_attempts = 0
        self.guilds_checked: set[int] = set()
        self.guild_aliases_removed = 0
        self.guild_shadow_count = 0
        self.guild_restricted_count = 0
        self.guild_repaired_count = 0
        self.guild_repair_failed_count = 0

    async def run(self, bot: Any, mode: str) -> bool:
        async with self._lock:
            if self.audited and (mode != "1" or (self.mode == "1" and self.last_error is None)):
                return False
            self.audited, self.mode, self.last_error = True, mode, None
            try:
                local = bot.tree.get_commands()
                names = frozenset(command.name for command in local)
                if names != EXPECTED_NAMES or len(local) != len(EXPECTED_NAMES) or not all(_chat_input(command) for command in local):
                    raise RuntimeError("unexpected_local_commands")
                if any(summarize_slash_visibility(local)[key] for key in ACCESS_GATES):
                    raise RuntimeError("unexpected_local_access_policy")
                app_id, bot_id = bot.application_id, getattr(bot.user, "id", None)
                if app_id is not None and bot_id is not None and app_id != bot_id:
                    raise RuntimeError("application_bot_mismatch")
                remote = await bot.tree.fetch_commands()
                self.summary = summarize_slash_visibility(remote)
                LOGGER.info("event=slash_visibility_audit scope=global mode=%s count=%d missing=%d obsolete=%d restricted=%d foreign=%d contexts=%d installs=%d age_restricted=%d",
                            mode, self.summary["count"], self.summary["missing_count"], self.summary["obsolete_count"],
                            self.summary["restricted_count"], self.summary["foreign_count"],
                            self.summary["context_mismatch_count"], self.summary["install_mismatch_count"], self.summary["age_restricted_count"])
                if self.summary["foreign_count"]:
                    raise RuntimeError("unexpected_remote_commands")
                needs_repair = any(self.summary[key] for key in REPAIR_REASONS)
                if mode == "0":
                    self.last_reason = "read_only_missing" if needs_repair else "already_published"
                    LOGGER.info("event=slash_sync_skipped scope=global reason=%s", self.last_reason)
                    return False
                if mode == "auto" and not needs_repair:
                    self.last_reason = "already_published"
                    LOGGER.info("event=slash_visibility_audit scope=global result=already_published")
                    return False
                self.sync_attempts += 1
                synced = await bot.tree.sync()
                result = summarize_slash_visibility(synced)
                if any(result[key] for key in (*REPAIR_REASONS, "foreign_count")):
                    raise RuntimeError("unexpected_sync_result")
                self.summary = result
                self.published, self.last_reason = True, "published"
                LOGGER.info("event=slash_sync_success scope=global count=%d application_id=%s bot_id=%s",
                            len(synced), app_id, bot_id)
                return True
            except Exception as error:
                self.last_error = error
                known = {"unexpected_local_commands", "unexpected_local_access_policy", "application_bot_mismatch", "unexpected_remote_commands", "unexpected_sync_result"}
                self.last_reason = str(error) if isinstance(error, RuntimeError) and str(error) in known else "remote_failed"
                status = getattr(error, "status", None)
                LOGGER.error("event=slash_sync_failed scope=global reason=%s error_type=%s status=%s application_id=%s bot_id=%s",
                             self.last_reason, type(error).__name__, status if type(status) is int else None,
                             getattr(bot, "application_id", None), getattr(getattr(bot, "user", None), "id", None))
                return False

    async def audit_guild_aliases(self, bot: Any, mode: str) -> None:
        """Inspect guild copies without bulk deletion or changing role/user overrides."""
        if self.last_reason == "application_bot_mismatch":
            return
        async with self._guild_lock:
            for guild in getattr(bot, "guilds", ()):
                if guild.id in self.guilds_checked:
                    continue
                self.guilds_checked.add(guild.id)
                try:
                    commands = await bot.tree.fetch_commands(guild=discord.Object(id=guild.id))
                    aliases = [command for command in commands if _chat_input(command) and command.name in OBSOLETE_NAMES
                               and getattr(command, "application_id", bot.application_id) == bot.application_id]
                    shadows = [command for command in commands if _chat_input(command) and command.name in EXPECTED_NAMES
                               and getattr(command, "application_id", bot.application_id) == bot.application_id]
                    restricted = [command for command in shadows if getattr(command, "default_member_permissions", None) is not None
                                  or getattr(command, "default_permissions", None) is not None]
                    self.guild_shadow_count += len(shadows)
                    self.guild_restricted_count += len(restricted)
                    LOGGER.info("event=slash_visibility_audit scope=guild guild_id=%s count=%d obsolete=%d shadows=%d restricted=%d",
                                guild.id, len(commands), len(aliases), len(shadows), len(restricted))
                    if mode != "0":
                        for command in aliases:
                            try:
                                await command.delete()
                                self.guild_aliases_removed += 1
                            except Exception as error:
                                LOGGER.warning("event=slash_guild_alias_cleanup guild_id=%s result=failed error_type=%s",
                                               guild.id, type(error).__name__)
                        # Old guild copies can shadow a repaired global command.
                        # Edit only our five known defaults, preserving command IDs,
                        # options and explicit Integrations user/role/channel rules.
                        if self.last_reason in {"published", "already_published"}:
                            for command in restricted:
                                try:
                                    updated = await command.edit(default_member_permissions=None)
                                    if getattr(updated, "default_member_permissions", None) is not None:
                                        raise RuntimeError("guild_default_still_restricted")
                                    self.guild_repaired_count += 1
                                    LOGGER.info("event=slash_guild_access_repair guild_id=%s result=updated", guild.id)
                                except Exception as error:
                                    self.guild_repair_failed_count += 1
                                    LOGGER.warning("event=slash_guild_access_repair guild_id=%s result=failed error_type=%s",
                                                   guild.id, type(error).__name__)
                    await asyncio.sleep(0)
                except Exception as error:
                    LOGGER.warning("event=slash_visibility_audit scope=guild guild_id=%s result=failed error_type=%s",
                                   guild.id, type(error).__name__)

    def status_text(self) -> str:
        return (f"슬래시 게시 진단: {self.last_reason} · 모드 {self.mode or '미실행'}\n"
                f"전역 누락 {self.summary.get('missing_count', 0)} · 기본 권한 제한 {self.summary.get('restricted_count', 0)}\n"
                f"서버 실행 범위 오류 {self.summary.get('context_mismatch_count', 0)} · 서버 설치 유형 오류 {self.summary.get('install_mismatch_count', 0)} · 연령 제한 {self.summary.get('age_restricted_count', 0)}\n"
                f"길드 동명 명령 {self.guild_shadow_count}개 · 제한 발견 {self.guild_restricted_count}개 / 기본 권한 복구 {self.guild_repaired_count}개 / 실패 {self.guild_repair_failed_count}개\n"
                f"길드 구형 별칭 정리 {self.guild_aliases_removed}개\n"
                "일부 회원만 안 보이면 해당 채널에서 !명령진단 · 서버 연동의 개별 권한은 운영자 확인 필요")
