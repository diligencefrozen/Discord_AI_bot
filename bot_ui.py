"""Button and slash-command adapters for the modern Discord UI.

This module deliberately keeps Discord objects at the edge of the application.  The
callbacks accept small service callables, which makes the UI usable in tests and
keeps the existing ``main.py`` services backwards compatible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from typing import Any, Awaitable, Callable, Iterable

SPECIAL_ADMIN_USER_ID = 855749166764654653
SPECIAL_ADMIN_USER_ID_ENV = "SPECIAL_ADMIN_USER_ID"
SPECIAL_ADMIN_USER_IDS_ENV = "SPECIAL_ADMIN_USER_IDS"
SPECIAL_ADMIN_GUILD_IDS_ENV = "SPECIAL_ADMIN_GUILD_IDS"
UNAUTHORIZED_ADMIN_MESSAGE = (
    "권한이 없습니다. 이 기능은 서버 소유자 또는 도리봇 파딱만 사용할 수 있어요."
)


def _parse_snowflake(value: str) -> int:
    value = value.strip()
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid Discord ID")
    parsed = int(value)
    if not 0 < parsed < 2**64:
        raise ValueError("invalid Discord ID")
    return parsed


def _special_admin_configuration() -> tuple[frozenset[int], str | None]:
    """Union legacy/new IDs; any malformed nonempty setting denies all exceptions.

    Neither variable grants an implicit default. Owners retain their existing
    access even when the optional exception configuration is missing or invalid.
    """
    legacy = (os.getenv(SPECIAL_ADMIN_USER_ID_ENV) or "").strip()
    multiple = (os.getenv(SPECIAL_ADMIN_USER_IDS_ENV) or "").strip()
    if not legacy and not multiple:
        return frozenset(), "special_id_unconfigured"
    try:
        ids = {_parse_snowflake(legacy)} if legacy else set()
        if multiple:
            ids.update(_parse_snowflake(value) for value in multiple.split(","))
    except ValueError:
        return frozenset(), "special_id_invalid"
    return frozenset(ids), None


def configured_special_admin_ids() -> frozenset[int]:
    """Expose the validated, deduplicated legacy/new union without raw settings."""
    return _special_admin_configuration()[0]


def _is_integer_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def admin_authorization_reason(actor: Any, guild: Any) -> str | None:
    """Return a private diagnostic category for an authorization decision.

    The category intentionally contains no user, guild, token, or role values;
    callers can log it internally without exposing server configuration.
    """

    if guild is None:
        return "guild_missing"
    actor_id = getattr(actor, "id", None)
    if not _is_integer_id(actor_id):
        return "actor_missing"
    if actor_id == getattr(guild, "owner_id", None):
        return None

    special_ids, config_error = _special_admin_configuration()
    if config_error is not None:
        return config_error
    if actor_id not in special_ids:
        return "special_id_mismatch"

    allowed_guilds = _configured_guild_ids()
    if allowed_guilds is not None and getattr(guild, "id", None) not in allowed_guilds:
        return "guild_scope_denied"
    return None


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
            ids.add(_parse_snowflake(item))
    except ValueError:
        return set()
    return ids


def is_admin_authorized(actor: Any, guild: Any) -> bool:
    """Apply the v6 administrator policy.

    Only ``guild.owner_id`` and configured 파딱 user IDs are accepted. DM
    interactions, roles, and Discord Administrator permissions are never used as
    authorization evidence.
    """
    reason = admin_authorization_reason(actor, guild)
    if reason is not None:
        logging.info("Administrator authorization denied: %s", reason)
        return False
    return True


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
                self.timeout = kwargs.get("timeout", 180)

            def add_item(self, item: Any) -> Any:
                self.children.append(item)
                return self

            def is_persistent(self) -> bool:
                return self.timeout is None

        class LayoutView(View):
            pass

        class Container:
            def __init__(self, *children: Any):
                self.children = list(children)

        class ActionRow(Container):
            pass

        class TextDisplay:
            def __init__(self, content: str):
                self.content = content

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
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class Button:
            def __init__(self, **kwargs: Any):
                self.disabled = False
                for key, value in kwargs.items():
                    setattr(self, key, value)

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
    _LayoutViewBase = discord.ui.LayoutView
    _ModalBase = discord.ui.Modal
    _Button = discord.ui.Button
    _Container = discord.ui.Container
    _ActionRow = discord.ui.ActionRow
    _TextDisplay = discord.ui.TextDisplay
else:
    _ViewBase = discord.View
    _LayoutViewBase = discord.LayoutView
    _ModalBase = discord.Modal
    _Button = discord.Button
    _Container = discord.Container
    _ActionRow = discord.ActionRow
    _TextDisplay = discord.TextDisplay


class QuestionModal(_ModalBase):
    """Question form; ``on_submit`` delegates to the neutral question service."""

    if DISCORD_AVAILABLE:
        prompt = discord.ui.TextInput(
            label="질문", placeholder="AI 답변은 이 채널에 공개됩니다. 궁금한 내용을 입력하세요", max_length=2000, required=True
        )

    def __init__(self, question_service: Callable[..., Awaitable[Any]] | None = None):
        if DISCORD_AVAILABLE:
            super().__init__(title="AI에게 질문하기")
        else:
            super().__init__()
            self.prompt = discord.TextInput()
        self.public_answer_notice = "AI 답변은 이 채널에 공개됩니다"
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
        if not is_admin_authorized(getattr(interaction, "user", None), getattr(interaction, "guild", None)):
            await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE)
            return
        content = str(getattr(self.content, "value", "")).strip()
        if not content:
            await _ephemeral(interaction, "검사할 문장을 입력해 주세요.")
            return
        if self.test_service is None:
            await _ephemeral(interaction, "탐지 테스트 기능이 준비되지 않았습니다.")
            return
        await self.test_service(interaction, content)


class _IntroActions:
    """Shared callbacks for old message buttons and Components V2 notices."""

    BUTTON_LABELS = ("💬 AI 질문", "🎨 그림 만들기", "🏆 내 성장", "🧭 전체 메뉴")
    BUTTON_IDS = ("ui:question", "ui:image", "ui:growth", "ui:menu")

    def _configure_services(
        self,
        question_service: Callable[..., Awaitable[Any]] | None = None,
        image_service: Callable[..., Awaitable[Any]] | None = None,
        growth_service: Callable[..., Awaitable[Any]] | None = None,
        menu_service: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.question_service = question_service
        self.image_service = image_service
        self.growth_service = growth_service
        self.menu_service = menu_service

    def _intro_buttons(self, *, explicit_rows: bool = False) -> list[Any]:
        buttons = []
        callbacks = (self.question, self.image, self.growth, self.menu)
        for index, (label, custom_id, callback) in enumerate(zip(self.BUTTON_LABELS, self.BUTTON_IDS, callbacks)):
            button = _Button(label=label, custom_id=custom_id, style=discord.ButtonStyle.secondary,
                             row=index // 2 if explicit_rows else None)
            button.callback = callback
            buttons.append(button)
        return buttons

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


class IntroView(_IntroActions, _ViewBase):
    """Legacy persistent view, retained for old notices and the start menu."""

    def __init__(self, question_service=None, image_service=None, growth_service=None, menu_service=None):
        super().__init__(timeout=None)
        self._configure_services(question_service, image_service, growth_service, menu_service)
        for button in self._intro_buttons(explicit_rows=True):
            self.add_item(button)


class TypingIntroView(_IntroActions, _LayoutViewBase):
    """One Components V2 box containing notice text and both button rows.

    Register an instance with callbacks and ``timeout=None`` using ``bot.add_view``
    after restart. Keep registering ``IntroView`` too for legacy public messages.
    Both layouts dispatch the same stable custom IDs to the same callbacks.
    """

    def __init__(self, user: Any = None, *, question_service=None, image_service=None,
                 growth_service=None, menu_service=None):
        super().__init__(timeout=None)
        self._configure_services(question_service, image_service, growth_service, menu_service)
        mention = getattr(user, "mention", "사용자")
        self.text = f"{mention}님, 채팅을 작성 중이셨군요! ⌨️\n도리봇으로 무엇을 해볼까요?"
        buttons = self._intro_buttons()
        self.add_item(_Container(_TextDisplay(self.text), _ActionRow(*buttons[:2]), _ActionRow(*buttons[2:])))


class FeatureMenuView(_ViewBase):
    """Private, click-through feature menu used by the public intro button."""

    def __init__(
        self,
        *,
        question_service: Callable[..., Awaitable[Any]] | None = None,
        image_service: Callable[..., Awaitable[Any]] | None = None,
        growth_service: Callable[..., Awaitable[Any]] | None = None,
    ):
        super().__init__(timeout=300)
        self.question_service = question_service
        self.image_service = image_service
        self.growth_service = growth_service
        if DISCORD_AVAILABLE:
            select = discord.ui.Select(
                custom_id="ui:feature-menu",
                placeholder="기능을 선택하세요",
                min_values=1,
                max_values=1,
                options=(
                    discord.SelectOption(label="AI 질문", value="question", emoji="💬"),
                    discord.SelectOption(label="그림 만들기", value="image", emoji="🎨"),
                    discord.SelectOption(label="내 성장", value="growth", emoji="🏆"),
                ),
            )
            select.callback = self._select_callback
            self.add_item(select)
        else:
            self.children = [type("SelectStub", (), {"custom_id": "ui:feature-menu"})()]

    async def _select_callback(self, interaction: Any) -> None:
        data = getattr(interaction, "data", {}) or {}
        values = data.get("values", []) if isinstance(data, dict) else []
        selected = values[0] if values else ""
        if selected == "question" and self.question_service is not None:
            await interaction.response.send_modal(QuestionModal(self.question_service))
            return
        if selected == "image" and self.image_service is not None:
            await interaction.response.send_modal(ImageModal(self.image_service))
            return
        if selected == "growth" and self.growth_service is not None:
            await self.growth_service(interaction)
            return
        await _ephemeral(interaction, "이 기능은 현재 사용할 수 없습니다.")


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


def _rule_display_text(root: str) -> str:
    # Display policy words literally; do not let their punctuation format the
    # page or their mention syntax notify other users.
    return "".join("\\" + char if char in "\\`*_~|>" else char for char in root)


class ActiveRulesView(_ViewBase):
    """Read-only, private snapshot of the authoritative active policy list."""

    def __init__(self, roots: Iterable[str], *, test_service=None, page_size: int = 20):
        super().__init__(timeout=300)
        self.roots = tuple(sorted(set(roots), key=lambda root: (unicodedata.normalize("NFKC", root).casefold(), root)))
        self.test_service = test_service
        self.page_index = 0
        self._page_lock = asyncio.Lock()
        # Reserve room for the heading and page indicator under Discord's 2000
        # character message limit. Long entries continue without truncation.
        pages: list[str] = []
        lines: list[str] = []
        length = 0
        for root in self.roots:
            rendered = "• " + _rule_display_text(root)
            chunks = [rendered[index:index + 1600] for index in range(0, len(rendered), 1600)]
            for line in chunks:
                if lines and (length + len(line) + 1 > 1600 or len(lines) >= max(1, page_size)):
                    pages.append("\n".join(lines))
                    lines, length = [], 0
                lines.append(line)
                length += len(line) + 1
        if lines or not pages:
            pages.append("\n".join(lines) if lines else "활성 금칙어가 없습니다.")
        self.pages = tuple(pages)
        self.previous_button = _Button(label="◀ 이전", custom_id="admin:rules:previous", style=discord.ButtonStyle.secondary)
        self.next_button = _Button(label="다음 ▶", custom_id="admin:rules:next", style=discord.ButtonStyle.secondary)
        self.test_button = _Button(label="🔎 탐지 테스트", custom_id="admin:test", style=discord.ButtonStyle.secondary)
        for button in (self.previous_button, self.next_button, self.test_button):
            button.callback = self._button_callback
            self.add_item(button)
        self._refresh_buttons()

    @property
    def content(self) -> str:
        return (f"🛡️ 도리봇 파딱 · 금칙어 관리\n✅ 활성화된 금칙어: {len(self.roots)}개\n"
                f"📄 {self.page_index + 1} / {len(self.pages)} 페이지\n\n{self.pages[self.page_index]}")

    def _refresh_buttons(self) -> None:
        self.previous_button.disabled = self.page_index == 0
        self.next_button.disabled = self.page_index == len(self.pages) - 1

    async def interaction_check(self, interaction: Any) -> bool:
        if is_admin_authorized(getattr(interaction, "user", None), getattr(interaction, "guild", None)):
            return True
        await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE)
        return False

    async def _button_callback(self, interaction: Any) -> None:
        if not await self.interaction_check(interaction):
            return
        data = getattr(interaction, "data", {}) or {}
        action_id = getattr(interaction, "custom_id", None) or data.get("custom_id")
        if action_id == "admin:test":
            if self.test_service is None:
                await _ephemeral(interaction, "탐지 테스트 기능이 준비되지 않았습니다.")
            else:
                await interaction.response.send_modal(ModerationTestModal(self.test_service))
            return
        if action_id not in ("admin:rules:previous", "admin:rules:next"):
            await _ephemeral(interaction, "관리 작업을 선택해 주세요.")
            return
        async with self._page_lock:
            delta = -1 if action_id == "admin:rules:previous" else 1
            self.page_index = max(0, min(len(self.pages) - 1, self.page_index + delta))
            self._refresh_buttons()
            await interaction.response.edit_message(content=self.content, view=self, allowed_mentions=_no_mentions())


def _no_mentions() -> Any:
    return discord.AllowedMentions(users=False, everyone=False, roles=False, replied_user=False)


async def send_active_rules_response(interaction: Any, roots: Iterable[str], *, test_service=None) -> None:
    """Send the actual engine's roots as an ephemeral, authorized pager."""
    if not is_admin_authorized(getattr(interaction, "user", None), getattr(interaction, "guild", None)):
        await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE)
        return
    view = ActiveRulesView(roots, test_service=test_service)
    await _ephemeral(interaction, view.content, view=view, allowed_mentions=_no_mentions())


async def admin_panel_response(interaction: Any, *, action_service: Callable[..., Awaitable[Any]] | None = None,
                               test_service: Callable[..., Awaitable[Any]] | None = None) -> None:
    """Handle ``/관리`` and never expose panel content to unauthorized users."""
    if not is_admin_authorized(getattr(interaction, "user", None), getattr(interaction, "guild", None)):
        await _ephemeral(interaction, UNAUTHORIZED_ADMIN_MESSAGE)
        return
    await _ephemeral(interaction, "도리봇 파딱 패널입니다.", view=AdminPanelView(action_service=action_service, test_service=test_service))


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

    callbacks = (("시작", "도리봇 기능을 시작합니다.", _start), ("질문", "AI에게 질문합니다. 답변은 이 채널에 공개됩니다.", _question), ("그림", "AI 그림을 만듭니다.", _image), ("내정보", "내 성장 정보를 봅니다.", _growth), ("관리", "도리봇 파딱 패널입니다.", _admin))
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


class SlashCommandRegistrationState:
    """Track slash registration separately from gateway synchronization.

    Command objects must only be added to the tree once, while a failed sync
    must remain retryable after reconnect.  ``ensure`` therefore remembers
    tree registration even when ``sync`` fails and only marks the operation
    complete after a successful sync.
    """

    def __init__(self) -> None:
        self.commands_registered = False
        self.synced = False
        self.last_error: Exception | None = None

    async def ensure(self, register: Callable[[], Any], sync: Callable[[], Awaitable[Any]]) -> bool:
        if self.synced:
            return False
        if not self.commands_registered:
            register()
            self.commands_registered = True
        try:
            await sync()
        except Exception as error:
            self.last_error = error
            return False
        self.synced = True
        self.last_error = None
        return True


def typing_intro_payload(user: Any, **services: Any) -> dict[str, Any]:
    """Build a V2-only public notice, permitting mentions of its target alone."""
    return {
        "view": TypingIntroView(user, **services),
        "allowed_mentions": discord.AllowedMentions(users=[user], everyone=False, roles=False, replied_user=False),
    }


__all__ = [
    "SPECIAL_ADMIN_USER_ID", "IntroView", "TypingIntroView", "FeatureMenuView", "QuestionModal", "ImageModal", "ModerationTestModal", "AdminPanelView", "ActiveRulesView",
    "admin_panel_response", "send_active_rules_response", "admin_authorization_reason", "configured_special_admin_ids", "is_admin_authorized", "is_authorized_admin", "is_admin_user",
    "register_slash_commands", "SlashCommandRegistrationState", "typing_intro_payload",
]
