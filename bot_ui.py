"""Button and slash-command adapters for the modern Discord UI.

This module deliberately keeps Discord objects at the edge of the application.  The
callbacks accept small service callables, which makes the UI usable in tests and
keeps the existing ``main.py`` services backwards compatible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

SPECIAL_ADMIN_USER_ID = 855749166764654653
SPECIAL_ADMIN_USER_ID_ENV = "SPECIAL_ADMIN_USER_ID"
SPECIAL_ADMIN_GUILD_IDS_ENV = "SPECIAL_ADMIN_GUILD_IDS"
UNAUTHORIZED_ADMIN_MESSAGE = (
    "권한이 없습니다. 이 기능은 서버 소유자 또는 도리봇 특별 관리자만 사용할 수 있어요."
)


def _configured_special_user_id() -> int | None:
    """Return the configured exception ID, failing closed on bad configuration."""
    value = os.getenv(SPECIAL_ADMIN_USER_ID_ENV)
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _configured_guild_ids() -> set[int] | None:
    """Parse optional guild scoping for the special account.

    When unset, the confirmed special account is valid in every guild.  Setting the
    variable to a comma separated list narrows it to those guilds.
    """
    raw = os.getenv(SPECIAL_ADMIN_GUILD_IDS_ENV)
    if raw is None or not raw.strip():
        return None
    ids: set[int] = set()
    try:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            value = int(item)
            if value <= 0:
                return set()
            ids.add(value)
    except ValueError:
        return set()
    return ids


def is_admin_authorized(actor: Any, guild: Any) -> bool:
    """Apply the v6 administrator policy.

    Only ``guild.owner_id`` and the configured special user ID are accepted.  DM
    interactions, roles, and Discord Administrator permissions are never used as
    authorization evidence.
    """
    if guild is None or actor is None:
        return False
    user_id = getattr(actor, "id", None)
    guild_id = getattr(guild, "id", None)
    if user_id is None or guild_id is None:
        return False
    if user_id == getattr(guild, "owner_id", None):
        return True
    special_id = _configured_special_user_id()
    if special_id is None or user_id != special_id:
        return False
    allowed_guilds = _configured_guild_ids()
    return allowed_guilds is None or guild_id in allowed_guilds


# Explicit aliases make the policy easy to reuse from cogs and service modules.
is_authorized_admin = is_admin_authorized
is_admin_user = is_admin_authorized


async def _ephemeral(interaction: Any, content: str, **kwargs: Any) -> Any:
    """Respond exactly once when possible, supporting lightweight test doubles."""
    response = getattr(interaction, "response", None)
    is_done = getattr(response, "is_done", False) if response is not None else False
    if callable(is_done):
        is_done = is_done()
    if response is not None and is_done:
        followup = getattr(interaction, "followup", None)
        if followup is not None and hasattr(followup, "send"):
            return await followup.send(content, ephemeral=True, **kwargs)
    if response is not None and hasattr(response, "send_message"):
        return await response.send_message(content, ephemeral=True, **kwargs)
    sender = getattr(interaction, "send", None)
    if sender is not None:
        return await sender(content, ephemeral=True, **kwargs)
    return None


try:  # discord.py is optional for static analysis and unit tests.
    import discord
    from discord import app_commands

    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in dependency-free CI
    DISCORD_AVAILABLE = False

    class _Fallback:  # enough surface for import and service-level tests
        class View:
            def __init__(self, *args: Any, **kwargs: Any):
                self.children: list[Any] = []

        class Modal:
            def __init__(self, *args: Any, **kwargs: Any):
                pass

        class TextInput:
            def __init__(self, *args: Any, **kwargs: Any):
                self.value = ""

        class ButtonStyle:
            primary = secondary = success = danger = 1

        class AllowedMentions:
            def __init__(self, **kwargs: Any):
                self.kwargs = kwargs

        class Button:  # decorator target compatibility
            pass

        class Interaction:  # typing aid
            pass

    discord = _Fallback()

    class _AppCommands:
        class CommandTree:  # pragma: no cover
            pass

        @staticmethod
        def command(*args: Any, **kwargs: Any):
            return lambda fn: fn

    app_commands = _AppCommands()


if DISCORD_AVAILABLE:
    _ViewBase = discord.ui.View
    _ModalBase = discord.ui.Modal
else:
    _ViewBase = discord.View
    _ModalBase = discord.Modal


class QuestionModal(_ModalBase):
    """Question form; ``on_submit`` delegates to the neutral question service."""

    if DISCORD_AVAILABLE:
        prompt = discord.ui.TextInput(
            label="질문", placeholder="궁금한 내용을 입력하세요", max_length=2000, required=True
        )

    def __init__(self, question_service: Callable[..., Awaitable[Any]] | None = None):
        if DISCORD_AVAILABLE:
            super().__init__(title="AI에게 질문하기")
        else:
            super().__init__()
            self.prompt = discord.TextInput()
        self.question_service = question_service

    async def on_submit(self, interaction: Any) -> None:
        prompt = str(getattr(self.prompt, "value", "")).strip()
        if not prompt:
            await _ephemeral(interaction, "질문을 입력해 주세요.")
            return
        if self.question_service is None:
            await _ephemeral(interaction, "질문 기능이 준비되지 않았습니다.")
            return
        await self.question_service(interaction, prompt)


class ImageModal(_ModalBase):
    if DISCORD_AVAILABLE:
        prompt = discord.ui.TextInput(
            label="그림 설명", placeholder="만들고 싶은 그림을 설명하세요", max_length=2000, required=True
        )

    def __init__(self, image_service: Callable[..., Awaitable[Any]] | None = None):
        if DISCORD_AVAILABLE:
            super().__init__(title="AI 그림 만들기")
        else:
            super().__init__()
            self.prompt = discord.TextInput()
        self.image_service = image_service

    async def on_submit(self, interaction: Any) -> None:
        prompt = str(getattr(self.prompt, "value", "")).strip()
        if not prompt:
            await _ephemeral(interaction, "그림 설명을 입력해 주세요.")
            return
        if self.image_service is None:
            await _ephemeral(interaction, "그림 기능이 준비되지 않았습니다.")
            return
        await self.image_service(interaction, prompt)


class ModerationTestModal(_ModalBase):
    """Admin-only test form; the service performs a second auth check."""

    if DISCORD_AVAILABLE:
        content = discord.ui.TextInput(
            label="탐지 테스트 문장",
            placeholder="검사할 문장을 입력하세요",
            max_length=2000,
            required=True,
        )

    def __init__(self, test_service: Callable[..., Awaitable[Any]] | None = None):
        if DISCORD_AVAILABLE:
            super().__init__(title="금칙어 탐지 테스트")
        else:
            super().__init__()
            self.content = discord.TextInput()
        self.test_service = test_service

    async def on_submit(self, interaction: Any) -> None:
        content = str(getattr(self.content, "value", "")).strip()
        if not content:
            await _ephemeral(interaction, "검사할 문장을 입력해 주세요.")
            return
        if self.test_service is None:
            await _ephemeral(interaction, "탐지 테스트 기능이 준비되지 않았습니다.")
            return
        await self.test_service(interaction, content)


class IntroView(_ViewBase):
    """The four public buttons required by v6 typing guidance."""

    BUTTON_LABELS = ("💬 AI 질문하기", "🎨 그림 만들기", "🏆 내 성장", "🧭 전체 기능")

    def __init__(
        self,
        question_service: Callable[..., Awaitable[Any]] | None = None,
        image_service: Callable[..., Awaitable[Any]] | None = None,
        growth_service: Callable[..., Awaitable[Any]] | None = None,
        menu_service: Callable[..., Awaitable[Any]] | None = None,
    ):
        super().__init__(timeout=300)
        self.question_service = question_service
        self.image_service = image_service
        self.growth_service = growth_service
        self.menu_service = menu_service
        if DISCORD_AVAILABLE:
            self._add_buttons()
        else:
            # Keep the four-button contract visible to dependency-free tests.
            self.children = [type("ButtonStub", (), {"label": label, "custom_id": custom_id})() for label, custom_id in (("💬 AI 질문하기", "ui:question"), ("🎨 그림 만들기", "ui:image"), ("🏆 내 성장", "ui:growth"), ("🧭 전체 기능", "ui:menu"))]

    def _add_buttons(self) -> None:
        specs = (
            ("💬 AI 질문하기", "ui:question", discord.ButtonStyle.primary, self.question),
            ("🎨 그림 만들기", "ui:image", discord.ButtonStyle.primary, self.image),
            ("🏆 내 성장", "ui:growth", discord.ButtonStyle.success, self.growth),
            ("🧭 전체 기능", "ui:menu", discord.ButtonStyle.secondary, self.menu),
        )
        for label, custom_id, style, callback in specs:
            button = discord.ui.Button(label=label, custom_id=custom_id, style=style, row=0 if label in self.BUTTON_LABELS[:2] else 1)
            button.callback = callback
            self.add_item(button)

    async def question(self, interaction: Any) -> None:
        if self.question_service is not None:
            await interaction.response.send_modal(QuestionModal(self.question_service))
        else:
            await _ephemeral(interaction, "질문 기능이 준비되지 않았습니다.")

    async def image(self, interaction: Any) -> None:
        if self.image_service is not None:
            await interaction.response.send_modal(ImageModal(self.image_service))
        else:
            await _ephemeral(interaction, "그림 기능이 준비되지 않았습니다.")

    async def growth(self, interaction: Any) -> None:
        if self.growth_service is not None:
            await self.growth_service(interaction)
        else:
            await _ephemeral(interaction, "성장 정보를 불러올 수 없습니다.")

    async def menu(self, interaction: Any) -> None:
        if self.menu_service is not None:
            await self.menu_service(interaction)
        else:
            await _ephemeral(interaction, "기능 메뉴가 준비되지 않았습니다.")


class AdminPanelView(_ViewBase):
    """Ephemeral administrator panel with a repeated authorization boundary."""

    def __init__(self, *, action_service: Callable[..., Awaitable[Any]] | None = None,
                 test_service: Callable[..., Awaitable[Any]] | None = None):
        super().__init__(timeout=300)
        self.action_service = action_service
        self.test_service = test_service
        if DISCORD_AVAILABLE:
            for label, custom_id in (("⚙️ 금칙어", "admin:rules"), ("🧪 탐지 테스트", "admin:test"), ("📋 관리 로그", "admin:logs"), ("⚙️ 운영 설정", "admin:settings")):
                button = discord.ui.Button(label=label, custom_id=custom_id, style=discord.ButtonStyle.secondary)
                button.callback = self._button_callback
                self.add_item(button)
        else:
            self.children = [type("ButtonStub", (), {"label": label, "custom_id": custom_id})() for label, custom_id in (("⚙️ 금칙어", "admin:rules"), ("🧪 탐지 테스트", "admin:test"), ("📋 관리 로그", "admin:logs"), ("⚙️ 운영 설정", "admin:settings"))]

    async def interaction_check(self, interaction: Any) -> bool:
        allowed = is_admin_authorized(getattr(interaction, "user", None), getattr(interaction, "guild", None))
        if not allowed:
            await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE)
        return allowed

    async def _button_callback(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        if self.action_service is not None:
            data = getattr(interaction, "data", {}) or {}
            action_id = getattr(interaction, "custom_id", None) or data.get("custom_id")
            if action_id == "admin:test" and self.test_service is not None:
                await interaction.response.send_modal(ModerationTestModal(self.test_service))
                return
            await self.action_service(interaction, action_id)
        else:
            await _ephemeral(interaction, "관리 기능이 준비되지 않았습니다.")


async def admin_panel_response(interaction: Any, *, action_service: Callable[..., Awaitable[Any]] | None = None,
                               test_service: Callable[..., Awaitable[Any]] | None = None) -> None:
    """Handle ``/관리`` and never expose panel content to unauthorized users."""
    if not is_admin_authorized(getattr(interaction, "user", None), getattr(interaction, "guild", None)):
        await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE)
        return
    await _ephemeral(interaction, "관리자 패널입니다.", view=AdminPanelView(action_service=action_service, test_service=test_service))


def register_slash_commands(
    bot: Any,
    *,
    start_service: Callable[..., Awaitable[Any]] | None = None,
    question_service: Callable[..., Awaitable[Any]] | None = None,
    image_service: Callable[..., Awaitable[Any]] | None = None,
    growth_service: Callable[..., Awaitable[Any]] | None = None,
    admin_service: Callable[..., Awaitable[Any]] | None = None,
    admin_test_service: Callable[..., Awaitable[Any]] | None = None,
) -> list[Any]:
    """Register the minimal v6 slash adapters and return command objects.

    Service callbacks receive the Interaction as their first argument.  Existing
    prefix commands can call the same callbacks without constructing a Context.
    """
    tree = getattr(bot, "tree", bot)
    registered: list[Any] = []

    async def _start(interaction: Any) -> None:
        await (start_service(interaction) if start_service else _ephemeral(interaction, "도리봇 기능을 버튼으로 이용해 보세요."))

    async def _question(interaction: Any) -> None:
        if question_service:
            await interaction.response.send_modal(QuestionModal(question_service))
        else:
            await _ephemeral(interaction, "질문 기능이 준비되지 않았습니다.")

    async def _image(interaction: Any) -> None:
        if image_service:
            await interaction.response.send_modal(ImageModal(image_service))
        else:
            await _ephemeral(interaction, "그림 기능이 준비되지 않았습니다.")

    async def _growth(interaction: Any) -> None:
        await (growth_service(interaction) if growth_service else _ephemeral(interaction, "성장 정보를 불러올 수 없습니다."))

    async def _admin(interaction: Any) -> None:
        await admin_panel_response(
            interaction, action_service=admin_service, test_service=admin_test_service
        )

    callbacks = (("시작", "도리봇 기능을 시작합니다.", _start), ("질문", "AI에게 질문합니다.", _question), ("그림", "AI 그림을 만듭니다.", _image), ("내정보", "내 성장 정보를 봅니다.", _growth), ("관리", "도리봇 관리자 패널입니다.", _admin))
    for name, description, callback in callbacks:
        if DISCORD_AVAILABLE and hasattr(app_commands, "Command"):
            if name == "관리" and hasattr(app_commands, "guild_only"):
                callback = app_commands.guild_only()(callback)
            command = app_commands.Command(name=name, description=description, callback=callback)
            tree.add_command(command)
        else:
            command = callback
            setattr(command, "name", name)
        registered.append(command)
    return registered


def typing_intro_payload(user: Any) -> dict[str, Any]:
    """Build the public typing notice while constraining mentions to one user."""
    mention = getattr(user, "mention", "")
    return {
        "content": f"{mention}님, 채팅을 작성 중이셨군요! ⌨️",
        "embed_description": "💡 도리봇으로 이런 것도 할 수 있어요!\n궁금한 것을 질문하거나 AI 그림을 만들어 보세요. 내 XP와 업적도 확인할 수 있어요.",
        "view": IntroView(),
        "allowed_mentions": discord.AllowedMentions(users=[user], everyone=False, roles=False),
    }


__all__ = [
    "SPECIAL_ADMIN_USER_ID", "IntroView", "QuestionModal", "ImageModal", "ModerationTestModal", "AdminPanelView",
    "admin_panel_response", "is_admin_authorized", "is_authorized_admin", "is_admin_user",
    "register_slash_commands", "typing_intro_payload",
]
