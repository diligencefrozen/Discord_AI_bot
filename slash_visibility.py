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
    restricted = sum(getattr(command, "default_permissions", None) is not None
                     or getattr(command, "default_member_permissions", None) is not None
                     for command in commands if command.name in EXPECTED_NAMES)
    return {"count": len(commands), "missing_count": len(EXPECTED_NAMES - names),
            "obsolete_count": len(names & OBSOLETE_NAMES), "restricted_count": restricted,
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
                app_id, bot_id = bot.application_id, getattr(bot.user, "id", None)
                if app_id is not None and bot_id is not None and app_id != bot_id:
                    raise RuntimeError("application_bot_mismatch")
                remote = await bot.tree.fetch_commands()
                self.summary = summarize_slash_visibility(remote)
                LOGGER.info("event=slash_visibility_audit scope=global mode=%s count=%d missing=%d obsolete=%d restricted=%d foreign=%d",
                            mode, self.summary["count"], self.summary["missing_count"], self.summary["obsolete_count"],
                            self.summary["restricted_count"], self.summary["foreign_count"])
                if self.summary["foreign_count"]:
                    raise RuntimeError("unexpected_remote_commands")
                needs_repair = any(self.summary[key] for key in ("missing_count", "obsolete_count", "restricted_count"))
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
                if any(result[key] for key in ("missing_count", "obsolete_count", "restricted_count", "foreign_count")):
                    raise RuntimeError("unexpected_sync_result")
                self.published, self.last_reason = True, "published"
                LOGGER.info("event=slash_sync_success scope=global count=%d application_id=%s bot_id=%s",
                            len(synced), app_id, bot_id)
                return True
            except Exception as error:
                self.last_error = error
                known = {"unexpected_local_commands", "application_bot_mismatch", "unexpected_remote_commands", "unexpected_sync_result"}
                self.last_reason = str(error) if isinstance(error, RuntimeError) and str(error) in known else "remote_failed"
                status = getattr(error, "status", None)
                LOGGER.error("event=slash_sync_failed scope=global reason=%s error_type=%s status=%s application_id=%s bot_id=%s",
                             self.last_reason, type(error).__name__, status if type(status) is int else None,
                             getattr(bot, "application_id", None), getattr(getattr(bot, "user", None), "id", None))
                return False

    async def audit_guild_aliases(self, bot: Any, mode: str) -> None:
        """After READY, inspect guild aliases; delete only this app's exact old alias."""
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
                    LOGGER.info("event=slash_visibility_audit scope=guild guild_id=%s count=%d obsolete=%d", guild.id, len(commands), len(aliases))
                    if mode != "0":
                        for command in aliases:
                            await command.delete()
                            self.guild_aliases_removed += 1
                    await asyncio.sleep(0)
                except Exception as error:
                    LOGGER.warning("event=slash_visibility_audit scope=guild guild_id=%s result=failed error_type=%s",
                                   guild.id, type(error).__name__)

    def status_text(self) -> str:
        return (f"슬래시 게시 진단: {self.last_reason} · 모드 {self.mode or '미실행'}\n"
                f"전역 누락 {self.summary.get('missing_count', 0)} · 기본 권한 제한 {self.summary.get('restricted_count', 0)}\n"
                f"길드 구형 별칭 정리 {self.guild_aliases_removed}개 · 서버 연동의 개별 권한은 운영자 확인 필요")
