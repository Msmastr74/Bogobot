import asyncio
import datetime
from contextlib import asynccontextmanager
from functools import cache
import json

import discord
from discord import File
from discord.abc import Snowflake
from discord.embeds import Embed
from requests import Response
from discord.mentions import AllowedMentions
from discord.poll import Poll
from discord.ui.view import BaseView
from pydantic import AliasPath, Field, field_validator

from typing import TYPE_CHECKING, Any, Awaitable, Optional, Sequence, TypedDict, TypeAlias, Callable, cast
from utils.ai.context import ContextRequest, close_system_tag, open_system_tag
from utils.discord import chunk_text, split_text_to_character_limit
from utils import groups
from utils.schemas import Schema
from utils.type import Coro

if TYPE_CHECKING:
    from bogobot_core import BotCore
from dataclasses import dataclass

BASE_INSTRUCTION_TEXT = (
    "You are Bogobot (@Bogobot, display name [DISPLAY_NAME]), a helpful Discord bot with a balanced friendly tone. "
    "Bogobot is a Discord bot designed for monitoring the Bogosort livestream by @swapjs, assisting with their discord server, as well as other features. "
    "You live in Discord and answer naturally when chatted with. Write in normal sentence casing, with clear, conversational replies. "
    "Be pretty friendly, but not overly friendly; not too casual, not too formal, and not corporate. "
    "Use light humor or warmth when it fits, but avoid hype, forced cheer, all-lowercase style, clipped one-word replies, and excessive emoji. "
    "You are triggered by a user mentioning you in a message or replying to a message by you. "
    "If a user triggered you by mention, their message may begin with your mention, like '@Bogobot hello!' instead of just 'hello!'. "
    "Treat the mention as addressing you, not as part of the request. "
    "Discord emojis are in the format <:emoji_name:123456789012345678>. If the user sends only emoji, you may reply with the same Discord emoji or Unicode emoji. "
)
_bot_mention_text = "@Bogobot"
_bot_display_name = "[DISPLAY_NAME]"


def instruction_text() -> str:
    text = instruction_text_base()
    custom = ai_config.custom_instruction_text.strip()
    if custom:
        text = f"{text}\n## Admin Instructions\n{custom}"
    return text


def instruction_text_base() -> str:
    return BASE_INSTRUCTION_TEXT.replace("@Bogobot", _bot_mention_text).replace(
        "[DISPLAY_NAME]",
        _bot_display_name,
    )

class BotActionParameters(TypedDict, total=False):
    capabilities: Sequence[str]

BotAction: TypeAlias = Callable[..., Coro[None]]
MAX_ASSISTANT_CONTEXT_CHARS = 4500
MAX_REPLY_CHARS = 2000
USER_AI_CAPABILITY = "user.ai"
ContextSource: TypeAlias = 'discord.Message | discord.Interaction | discord.abc.MessageableChannel'
ContextHandler: TypeAlias = Callable[[ContextRequest, ContextSource, str], Awaitable[str | None]]


class AIHistoryConfig(Schema):
    enabled: bool = Field(True, validation_alias=AliasPath("history", "enabled"))
    path: str = Field("ai_history.sqlite3", validation_alias=AliasPath("history", "path"))
    char_budget: int = Field(10_000, validation_alias=AliasPath("history", "char_budget"))
    persistent_char_budget: int = Field(5_000, validation_alias=AliasPath("history", "persistent_char_budget"))

    @field_validator("path", mode="before")
    @classmethod
    def stringify_path(cls, value: object) -> str:
        return str(value)

    @field_validator("char_budget", "persistent_char_budget", mode="before")
    @classmethod
    def nonnegative_char_budget(cls, value: Any) -> int:
        return max(0, int(value))


class AIBreakConfig(Schema):
    enabled: bool = Field(True, validation_alias=AliasPath("breaks", "enabled"))
    active_minutes: float = Field(20, validation_alias=AliasPath("breaks", "active_minutes"))
    break_minutes: float = Field(10, validation_alias=AliasPath("breaks", "break_minutes"))

    @field_validator("active_minutes", "break_minutes", mode="before")
    @classmethod
    def nonnegative_minutes(cls, value: Any) -> float:
        return max(0.0, float(value))


class AIConfig(Schema):
    enabled: bool = True
    model: str = "gpt-4o-mini"
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str | None = None
    base_url: str | None = None
    custom_instruction_text: str = ""
    request_interval_seconds: float = 60.0
    normalize_discord: bool = True
    multipart_responses: bool = True
    history: AIHistoryConfig = Field(default_factory=lambda: AIHistoryConfig.model_validate({}))
    breaks: AIBreakConfig = Field(default_factory=lambda: AIBreakConfig.model_validate({}))

    @field_validator("model", "api_key_env", mode="before")
    @classmethod
    def stringify_required(cls, value: object) -> str:
        return str(value)

    @field_validator("api_key", "base_url", mode="before")
    @classmethod
    def stringify_optional(cls, value: object) -> str | None:
        return str(value) if value is not None else None

    @field_validator("custom_instruction_text", mode="before")
    @classmethod
    def stringify_custom_instruction_text(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("request_interval_seconds", mode="before")
    @classmethod
    def nonnegative_request_interval(cls, value: Any) -> float:
        return max(0.0, float(value))


ai_config = AIConfig()


def setup_ai(bot: "BotCore"):
    import os
    from utils.ai import ai as ai_core

    global ai_config

    raw_config = bot.config.get("ai", {})
    if not isinstance(raw_config, dict):
        raise TypeError("Config key 'ai' must be an object.")

    ai_config = AIConfig.model_validate(raw_config)
    ai_core.configure(
        enabled=ai_config.enabled,
        model_name=ai_config.model,
        api_key_env=ai_config.api_key_env,
        base_url=ai_config.base_url,
        request_interval_seconds=ai_config.request_interval_seconds,
        normalize_discord=ai_config.normalize_discord,
        multipart_responses=ai_config.multipart_responses,
        history_enabled=ai_config.history.enabled,
        history_path=ai_config.history.path,
        history_char_budget=ai_config.history.char_budget,
        memory_char_budget=ai_config.history.persistent_char_budget,
        logger=bot.logger.getChild("AI"),
    )
    ai_core.context.configure(user_capabilities=lambda user_id: bot.accounts[user_id].permissions.capabilities)
    if ai_core.enabled and ai_config.api_key:
        os.environ[ai_core.api_key_env] = ai_config.api_key
    return ai_core


_ai_break_until: datetime.datetime | None = None
_capture_add_message_by_id: dict[int, Callable[[discord.Message | None], None]] = {}


@cache
def capturing_response_class(response_class: type) -> type:
    class CapturingResponse(response_class):
        __slots__ = ()

        async def send_message(self, *args: Any, **kwargs: Any) -> Any:
            result = await super(CapturingResponse, self).send_message(*args, **kwargs)
            add_message = _capture_add_message_by_id.get(id(self))
            if add_message is None:
                return result
            if isinstance(result, discord.Message):
                add_message(result)
                return result

            message = getattr(self, "_message", None)
            if isinstance(message, discord.Message):
                add_message(message)
                return result

            parent = getattr(self, "_parent", None)
            if parent is None:
                return result
            try:
                response_message = await parent.original_response()
            except (discord.HTTPException, discord.ClientException):
                return result
            add_message(response_message if isinstance(response_message, discord.Message) else None)
            return result

    return CapturingResponse


@cache
def capturing_followup_class(followup_class: type) -> type:
    class CapturingFollowup(followup_class):
        __slots__ = ()

        async def send(self, *args: Any, **kwargs: Any) -> Any:
            add_message = _capture_add_message_by_id.get(id(self))
            capturing = add_message is not None
            caller_requested_wait = bool(kwargs.get("wait", False))
            if capturing:
                kwargs["wait"] = True
            result = await super(CapturingFollowup, self).send(*args, **kwargs)
            if capturing:
                add_message(result if isinstance(result, discord.Message) else None)
                return result if caller_requested_wait else None
            return result

    return CapturingFollowup

@dataclass(frozen=True, slots=True)
class MessageInteractionCommand:
    name: str

    @property
    def qualified_name(self) -> str:
        return self.name


class MessageInteractionFollowup(discord.Webhook):
    __slots__ = ("interaction",)

    def __init__(self, interaction: "MessageInteraction"):
        self.interaction = interaction

    async def send(
        self,
        content: str = discord.utils.MISSING,
        *,
        username: str = discord.utils.MISSING,
        avatar_url: Any = discord.utils.MISSING,
        tts: bool = False,
        wait: bool = False,
        file: File = discord.utils.MISSING,
        files: Sequence[File] = discord.utils.MISSING,
        embed: Embed = discord.utils.MISSING,
        embeds: Sequence[Embed] = discord.utils.MISSING,
        ephemeral: bool = False,
        allowed_mentions: AllowedMentions = discord.utils.MISSING,
        view: BaseView = discord.utils.MISSING,
        thread: Snowflake = discord.utils.MISSING,
        thread_name: str = discord.utils.MISSING,
        suppress_embeds: bool = False,
        silent: bool = False,
        applied_tags: list[Any] = discord.utils.MISSING,
        poll: Poll = discord.utils.MISSING,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "tts": tts,
            "suppress_embeds": suppress_embeds,
            "silent": silent,
        }
        optional_kwargs = {
            "file": file,
            "files": files,
            "embed": embed,
            "embeds": embeds,
            "allowed_mentions": allowed_mentions,
            "view": view,
            "poll": poll,
        }
        kwargs.update({
            key: value
            for key, value in optional_kwargs.items()
            if value is not discord.utils.MISSING
        })
        message = await cast(Any, self.interaction.send_channel).send(
            None if content is discord.utils.MISSING else content,
            **kwargs,
        )
        return message if wait else None


class MessageInteractionResponse(discord.InteractionResponse[discord.Client]):
    __slots__ = ("_message", "_message_interaction")

    def __init__(self, parent: "MessageInteraction"):
        super().__init__(parent)
        self._message_interaction = parent
        self._response_type = (
            discord.InteractionResponseType.channel_message
            if parent._followup_only else
            None
        )
        self._message: discord.Message | None = None

    async def defer(
        self,
        *,
        ephemeral: bool = False,
        thinking: bool = False,
    ) -> None:
        self._response_type = discord.InteractionResponseType.deferred_channel_message

    async def send_message(
        self,
        content: Any = None,
        *,
        embed: Embed = discord.utils.MISSING,
        embeds: Sequence[Embed] = discord.utils.MISSING,
        file: File = discord.utils.MISSING,
        files: Sequence[File] = discord.utils.MISSING,
        view: BaseView = discord.utils.MISSING,
        tts: bool = False,
        ephemeral: bool = False,
        allowed_mentions: AllowedMentions = discord.utils.MISSING,
        suppress_embeds: bool = False,
        silent: bool = False,
        delete_after: float | None = None,
        poll: Poll = discord.utils.MISSING,
    ) -> Any:
        self._response_type = discord.InteractionResponseType.channel_message
        kwargs: dict[str, Any] = {
            "mention_author": False,
            "tts": tts,
            "suppress_embeds": suppress_embeds,
            "silent": silent,
            "delete_after": delete_after,
        }
        optional_kwargs = {
            "embed": embed,
            "embeds": embeds,
            "file": file,
            "files": files,
            "view": view,
            "allowed_mentions": allowed_mentions,
            "poll": poll,
        }
        kwargs.update({
            key: value
            for key, value in optional_kwargs.items()
            if value is not discord.utils.MISSING
        })
        if self._message_interaction.source_message is not None:
            self._message = await self._message_interaction.source_message.reply(
                content,
                **kwargs,
            )
        else:
            self._message = await cast(Any, self._message_interaction.send_channel).send(
                content,
                **kwargs,
            )


class MessageInteraction(discord.Interaction[discord.Client]):
    __slots__ = (
        "source_message",
        "send_channel",
        "_message_command",
        "_followup_only",
    )

    def __init__(
        self,
        bot: "BotCore",
        source: 'discord.Message | discord.abc.MessageableChannel',
        command_name: str,
        *,
        user: discord.User | discord.Member | None = None,
        guild: discord.Guild | None = None,
        state: Any | None = None,
        followup_only: bool = False,
    ):
        if isinstance(source, discord.Message):
            source_message: discord.Message | None = source
            send_channel = source.channel
        else:
            source_message = None
            send_channel = source
        self._state = state or getattr(source, "_state", None) or getattr(send_channel, "_state")
        self._client = bot
        self._session = getattr(self._state.http, "_HTTPClient__session")
        self._baton = None
        self._original_response = None
        self.id = source_message.id if source_message is not None else discord.utils.time_snowflake(discord.utils.utcnow())
        self.type = discord.InteractionType.application_command
        self.data = None
        self.application_id = bot.user.id if bot.user is not None else 0
        self.message = source_message
        self.source_message = source_message
        self.send_channel = send_channel
        resolved_user = user or (source_message.author if source_message is not None else bot.user)
        if resolved_user is None:
            raise RuntimeError("MessageInteraction requires a user when bot.user is unavailable.")
        self.user = cast(Any, resolved_user)
        self.channel = cast(Any, send_channel)
        resolved_guild = guild or (source_message.guild if source_message is not None else getattr(send_channel, "guild", None))
        self.guild_id = resolved_guild.id if resolved_guild is not None else None
        self.token = ""
        self.version = 1
        self.locale = discord.Locale.american_english
        self.guild_locale = resolved_guild.preferred_locale if resolved_guild is not None else None
        self.extras = {}
        self.command_failed = False
        self.entitlement_sku_ids = []
        self.entitlements = []
        self.context = discord.app_commands.AppCommandContext()
        self.filesize_limit = discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        self._integration_owners = {}
        self._permissions = 0
        self._app_permissions = 0
        self._message_command = MessageInteractionCommand(command_name)
        self._followup_only = followup_only

    @discord.utils.cached_slot_property("_cs_response")
    def response(self: discord.Interaction[discord.Client]) -> MessageInteractionResponse:
        return MessageInteractionResponse(cast(MessageInteraction, self))

    @discord.utils.cached_slot_property("_cs_followup")
    def followup(self: discord.Interaction[discord.Client]) -> MessageInteractionFollowup:
        return MessageInteractionFollowup(cast(MessageInteraction, self))

    @discord.utils.cached_slot_property("_cs_command")
    def command(self: discord.Interaction[discord.Client]) -> Any:
        return cast(MessageInteraction, self)._message_command

    @discord.utils.cached_slot_property("_cs_namespace")
    def namespace(self: discord.Interaction[discord.Client]) -> discord.app_commands.Namespace:
        return discord.app_commands.Namespace(self, {}, [])

    @discord.utils.cached_slot_property("_cs_command_id")
    def command_id(self: discord.Interaction[discord.Client]) -> Optional[int]:
        return self.id

    @discord.utils.cached_slot_property("_cs_custom_id")
    def custom_id(self: discord.Interaction[discord.Client]) -> Optional[str]:
        return None

    async def original_response(self) -> Any:
        response = cast(MessageInteractionResponse, self.response)
        if response._message is None:
            resp = Response()
            resp.status_code = 404
            resp.reason = "Not Found"
            raise discord.NotFound(resp, "Interaction response has not sent a message.")
        return response._message

    async def delete_original_response(self) -> None:
        response = cast(MessageInteractionResponse, self.response)
        if response._message is not None:
            await response._message.delete()
            response._message = None

    @property
    def created_at(self) -> datetime.datetime:
        if self.source_message is not None:
            return self.source_message.created_at
        return discord.utils.snowflake_time(self.id)

    @property
    def expires_at(self) -> datetime.datetime:
        return self.created_at + datetime.timedelta(minutes=15)

    def is_expired(self) -> bool:
        return False

def mentioned_message_text(bot: 'BotCore', message: discord.Message) -> str | None:
    if bot.user is None or bot.user not in message.mentions:
        return None

    return read_text_from_message(message)

def read_text_from_message(message: discord.Message) -> str | None:
    parts: list[str] = []

    # Message content
    if message.content:
        parts.append(message.content)

    # Embeds
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)

        if embed.description:
            parts.append(embed.description)

        if embed.author and embed.author.name:
            parts.append(embed.author.name)

        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)

        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)

    # Components / views
    def read_component(component) -> None:
        for attr in (
            "label",
            "custom_id",
            "placeholder",
            "text",
            "content",
            "description",
            "title",
        ):
            value = getattr(component, attr, None)
            if isinstance(value, str) and value:
                parts.append(value)

        # Select menu options
        for option in getattr(component, "options", []) or []:
            for attr in ("label", "value", "description"):
                value = getattr(option, attr, None)
                if isinstance(value, str) and value:
                    parts.append(value)

        # Nested components / action rows / Components v2 containers
        for child in (
            getattr(component, "children", None)
            or getattr(component, "components", None)
            or []
        ):
            read_component(child)

    for component in getattr(message, "components", []) or []:
        read_component(component)

    text = " ".join(" ".join(parts).split())
    return text or None


def truncate_text_to_character_limit(text: str, max_len: int) -> str:
    return "".join(split_text_to_character_limit(text, max_len, max_pieces=1))


def replied_assistant_message(bot: 'BotCore', message: discord.Message) -> tuple[discord.Message, str] | None:
    if bot.user is None or message.reference is None:
        return None

    resolved = message.reference.resolved
    if not isinstance(resolved, discord.Message):
        return None
    if resolved.author.id != bot.user.id:
        return None

    text = read_text_from_message(resolved)
    if not text:
        return None
    return resolved, truncate_text_to_character_limit(text, MAX_ASSISTANT_CONTEXT_CHARS)

def json_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def ai_on_break() -> bool:
    return _ai_break_until is not None and discord.utils.utcnow() < _ai_break_until


def ai_break_config(bot: 'BotCore') -> AIBreakConfig:
    return ai_config.breaks


def ai_enabled(bot: 'BotCore') -> bool:
    return ai_config.enabled


def banned(bot: 'BotCore', user: discord.User | discord.Member):
    return False


def can_use_ai(bot: 'BotCore', user: discord.User | discord.Member, guild_id: int | None) -> bool:
    return bot.accounts[user.id].local(guild_id).permissions.can_use(USER_AI_CAPABILITY)


class ContextRequestExecutor:
    def __init__(self, bot: "BotCore"):
        from utils.ai import ai as ai_core

        self.bot = bot
        self.ai_core = ai_core
        self.handlers: dict[str, ContextHandler] = {
            "user": self._user_context,
            "stream": self._stream_context,
            "stats": self._stats_context,
            "sort": self._sort_context,
            "minigame": self._minigame_context,
            "milestone": self._milestone_context,
        }

    async def execute(
        self,
        source: ContextSource,
        text: str,
    ) -> str | None:
        channel_id = self._source_channel_id(source)
        if channel_id is None:
            return None
        source_user_id = self._source_user_id(source)
        requests = self.ai_core.context.query_context_requests(
            channel_id=channel_id
        )
        if not requests:
            return None

        blocks: list[str] = []
        for request in requests:
            if request.user_id is not None and request.user_id != source_user_id:
                continue
            content = await self._execute_request(request, source, text)
            self.ai_core.context.discard_context_request(request)
            if content is None:
                continue
            blocks.append(self._format_requested_context(request, content))

        return "\n".join(blocks) if blocks else None

    async def _execute_request(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str | None:
        handler = self.handlers.get(request.type)
        if handler is None:
            return None
        try:
            return await handler(request, source, text)
        except Exception:
            self.bot.logger.getChild("AI.Context").exception(
                f"AI context request {request.type!r} failed"
            )
            return None

    async def _user_context(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str | None:
        user_id = self._payload_user_id(request) or self._source_user_id(source)
        if user_id is None:
            return None
        guild = getattr(source, "guild", None)
        user = guild.get_member(user_id) if guild is not None else self.bot.get_user(user_id)
        account = self.bot.accounts[user_id].local(getattr(guild, "id", None))
        capability_text = ", ".join(
            f"{capability}:{depth}"
            for capability, depth in sorted(account.permissions.capabilities.items())
        ) or "none"
        if user is None:
            return (
                f"user_id: {user_id}\n"
                "known: false\n"
                f"capabilities: {capability_text}"
            )

        lines = [
            f"user_id: {user.id}",
            "known: true",
            f"mention: <@{user.id}>",
            f"username: {user.name}",
            f"display_name: {json_string(user.display_name)}",
            f"bot: {user.bot}",
            f"created_at: {user.created_at.isoformat()}",
            f"capabilities: {capability_text}",
        ]
        if isinstance(user, discord.Member):
            lines.extend((
                f"guild_id: {user.guild.id}",
                f"guild_name: {json_string(user.guild.name)}",
                f"joined_at: {user.joined_at.isoformat() if user.joined_at is not None else 'None'}",
                "roles: " + ", ".join(
                    f"{role.name}:{role.id}"
                    for role in user.roles
                    if role.name != "@everyone"
                ),
            ))
        return "\n".join(lines)

    async def _stream_context(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str:
        return "\n".join((
            await self._stats_context(request, source, text),
            "",
            await self._sort_context(request, source, text),
        ))

    async def _stats_context(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str:
        lines = [
            f"stream_uptime: {self.bot.get_stream_uptime()}",
            f"stats_source: {self.bot.config.get('stats_source', 'api')}",
            f"last_stats_refresh_unix: {self.bot._last_ocr_refresh or 0}",
            "stats:",
        ]
        if self.bot.stats:
            for key, value in sorted(self.bot.stats.items()):
                lines.append(f"- {key}: {value}")
        else:
            lines.append("- none")
        return "\n".join(lines)

    async def _sort_context(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str:
        correct_count = sum(1 for correct, _value in self.bot.new_values if correct)
        return "\n".join((
            f"sort_section_count: {self.bot.SORT_SECTION_COUNT}",
            f"best_shuffle_correct_count: {correct_count}",
            f"best_shuffle_sections: {self.bot.best_shuffle_sections}",
            f"sort_values: {self.bot.sort_values}",
            f"new_values: {self.bot.new_values}",
        ))

    async def _minigame_context(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str | None:
        game = str(request.payload.get("game") or request.payload.get("query") or "").strip().casefold()
        guild_id = self._source_guild_id(source)
        if guild_id is None:
            return "minigame: unavailable outside a server"
        if game in ("bogotree", "tree"):
            data = await self._server_json_file("bogotree.json", "servers", guild_id)
            raw_state = data.get("state")
            state = raw_state if isinstance(raw_state, dict) else {}
            leaderboard = await self._minigame_account_context(guild_id, "bogotree")
            return self._join_context(self._bogotree_context(state), leaderboard)
        if game in ("cbogo", "community_bogosort", "community-bogosort"):
            data = await self._server_json_file("cbogo.json", "servers", guild_id)
            raw_state = data.get("state")
            state = raw_state if isinstance(raw_state, dict) else data
            leaderboard = await self._minigame_account_context(guild_id, "cbogo")
            return self._join_context(self._cbogo_context(state), leaderboard)
        return None

    def _bogotree_context(self, state: dict[str, Any]) -> str:
        x = self._int_list(state.get("x"))
        best_x = self._int_list(state.get("best_x"))
        return "\n".join((
            "game: bogotree",
            f"solved: {bool(state.get('solved', False))}",
            f"current_values: {x}",
            f"current_step: {self._int_value(state.get('current_step'))}",
            f"total_steps: {self._int_value(state.get('total_steps'))}",
            f"best_values: {best_x}",
            f"best_step: {self._int_value(state.get('best_step'))}",
            f"best_score: {self._float_value(state.get('best_score'))}",
            f"best_equal_count: {self._int_value(state.get('best_equal_count'))}",
        ))

    def _cbogo_context(self, state: dict[str, Any]) -> str:
        return "\n".join((
            "game: cbogo",
            f"solved: {bool(state.get('solved', False))}",
            f"current_array: {self._int_list(state.get('current_array'))}",
            f"shuffles: {self._int_value(state.get('shuffles'))}",
            f"uses: {self._int_value(state.get('uses'))}",
            f"best_array: {self._int_list(state.get('best_array'))}",
            f"best_score: {self._int_value(state.get('best_score'))}",
            f"best_run_shuffle: {self._int_value(state.get('best_run_shuffle'))}",
            f"best_run_count: {self._int_value(state.get('best_run_count'))}",
            f"winner_id: {state.get('winner_id')}",
            f"last_user: {state.get('last_user')}",
        ))

    async def _milestone_context(
        self,
        request: ContextRequest,
        source: ContextSource,
        text: str,
    ) -> str | None:
        if self.bot.milestones is None:
            return "milestones: unavailable"

        names = sorted(await self.bot.milestones.names(), key=str.casefold)
        if not names:
            return "milestones: none"

        lines: list[str] = []
        for milestone_name in names:
            current_value = await self.bot.milestones.get(milestone_name)
            history = self.bot.milestones.history.get(milestone_name)
            history_items = list(history) if history else []
            lines.extend((
                f"milestone: {milestone_name}",
                f"current_value: {current_value or 'None'}",
                f"history_count: {len(history_items)}",
                "recent_history:",
            ))
            if history_items:
                for value, timestamp, _image in history_items[-10:]:
                    lines.append(f"- time: {timestamp}, value: {value}")
            else:
                lines.append("- (empty)")
            lines.append("")
        return "\n".join(lines)

    async def _server_json_file(self, path: str, servers_key: str, guild_id: int) -> dict[str, Any]:
        data = await self._json_file(path)
        raw_servers = data.get(servers_key)
        servers = raw_servers if isinstance(raw_servers, dict) else {}
        raw_server = servers.get(str(guild_id))
        if isinstance(raw_server, dict):
            return raw_server
        return data

    async def _minigame_account_context(self, guild_id: int, key: str) -> str:
        rows = await self.bot.accounts.query_local(guild_id, key)
        if not rows:
            return "leaderboard: none"
        lines = [f"leaderboard_entries: {len(rows)}"]
        for uid, raw_stats in rows[:10]:
            compact_stats = json.dumps(raw_stats, ensure_ascii=False, separators=(",", ":"), default=str)
            lines.append(f"- user_id: {uid}, stats: {compact_stats}")
        return "\n".join(lines)

    def _join_context(self, *parts: str | None) -> str:
        return "\n".join(part.strip() for part in parts if part and part.strip())

    async def _json_file(self, path: str) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            try:
                with open(path, "r", encoding="utf-8") as file:
                    data = json.load(file)
            except (OSError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        return await asyncio.to_thread(load)

    def _int_list(self, value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result

    def _int_value(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _float_value(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _format_requested_context(self, request: ContextRequest, content: str) -> str:
        attrs = [
            f'time={json_string(discord.utils.utcnow().isoformat())}',
            f'type={json_string(request.type)}',
        ]
        if request.id is not None:
            attrs.append(f'id={json_string(str(request.id))}')
        if request.payload:
            attrs.append(f'payload={json_string(json.dumps(request.payload, ensure_ascii=False, separators=(",", ":")))}')
        open_tag = open_system_tag("requested_context").replace(">", f" {' '.join(attrs)}>")
        return f"{open_tag}\n{content.strip()}\n{close_system_tag('requested_context')}"

    def _source_channel_id(
        self,
        source: 'discord.Message | discord.Interaction | discord.abc.MessageableChannel',
    ) -> int | None:
        if isinstance(source, discord.Message) or isinstance(source, discord.Interaction):
            return self.ai_core.context.source_channel_id(source)
        channel_id = getattr(source, "id", None)
        return channel_id if isinstance(channel_id, int) else None

    def _source_user_id(
        self,
        source: ContextSource,
    ) -> int | None:
        if isinstance(source, discord.Message):
            return source.author.id
        if isinstance(source, discord.Interaction):
            return source.user.id
        return None

    def _source_guild_id(self, source: ContextSource) -> int | None:
        if isinstance(source, discord.Message):
            return source.guild.id if source.guild is not None else None
        if isinstance(source, discord.Interaction):
            return source.guild_id
        guild = getattr(source, "guild", None)
        guild_id = getattr(guild, "id", None)
        return guild_id if isinstance(guild_id, int) else None

    def _payload_user_id(self, request: ContextRequest) -> int | None:
        raw_user_id = request.payload.get("user_id")
        if isinstance(raw_user_id, int):
            return raw_user_id
        if isinstance(raw_user_id, str) and raw_user_id.isdigit():
            return int(raw_user_id)
        return None


@asynccontextmanager
async def capture_interaction_output(interaction: discord.Interaction):
    output_messages: list[discord.Message] = []
    response = interaction.response
    followup = interaction.followup
    response_class = type(response)
    followup_class = type(followup)

    def add_message(message: discord.Message | None) -> None:
        if message is None:
            return
        if any(existing.id == message.id for existing in output_messages):
            return
        output_messages.append(message)

    response.__class__ = capturing_response_class(response_class)
    followup.__class__ = capturing_followup_class(followup_class)
    _capture_add_message_by_id[id(response)] = add_message
    _capture_add_message_by_id[id(followup)] = add_message
    try:
        yield output_messages
    finally:
        _capture_add_message_by_id.pop(id(response), None)
        _capture_add_message_by_id.pop(id(followup), None)
        response.__class__ = response_class
        followup.__class__ = followup_class

async def setup(bot: 'BotCore'):
    ai_core = setup_ai(bot)
    manage = groups.manage(bot)
    bot.accounts.capabilities.register(USER_AI_CAPABILITY)

    bot.event(bot.on_message)
    break_task: asyncio.Task[None] | None = None
    context_request_executor = ContextRequestExecutor(bot)

    async def save_ai_config() -> None:
        raw_config = bot.config.get("ai")
        config = raw_config if isinstance(raw_config, dict) else {}
        config["enabled"] = ai_config.enabled
        config["custom_instruction_text"] = ai_config.custom_instruction_text
        breaks_config = config.get("breaks")
        breaks = breaks_config if isinstance(breaks_config, dict) else {}
        breaks["enabled"] = ai_config.breaks.enabled
        breaks["active_minutes"] = ai_config.breaks.active_minutes
        breaks["break_minutes"] = ai_config.breaks.break_minutes
        config["breaks"] = breaks
        bot.config["ai"] = config
        await bot.save_config()

    def prompt_preview(text: str, *, limit: int = 1800) -> str:
        text = text.strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit - 20].rstrip()}\n-# Truncated preview"

    async def restart_break_task() -> None:
        nonlocal break_task
        global _ai_break_until
        _ai_break_until = None
        if break_task is not None and not break_task.done():
            break_task.cancel()
            try:
                await break_task
            except asyncio.CancelledError:
                pass
        if ai_config.breaks.enabled:
            break_task = asyncio.create_task(ai_break_cycle())
        else:
            break_task = None
        await bot.discord.change_presence(status=discord.Status.online)

    class AIManagementView(discord.ui.LayoutView):
        def __init__(self) -> None:
            super().__init__(timeout=300)
            on_button = discord.ui.Button(
                label="On",
                style=discord.ButtonStyle.primary if ai_config.enabled else discord.ButtonStyle.secondary,
                disabled=ai_config.enabled,
            )
            off_button = discord.ui.Button(
                label="Off",
                style=discord.ButtonStyle.danger if not ai_config.enabled else discord.ButtonStyle.secondary,
                disabled=not ai_config.enabled,
            )
            edit_button = discord.ui.Button(
                label="Edit Custom Prompt",
                style=discord.ButtonStyle.secondary,
            )
            breaks_on_button = discord.ui.Button(
                label="Breaks On",
                style=(
                    discord.ButtonStyle.primary
                    if ai_config.breaks.enabled else
                    discord.ButtonStyle.secondary
                ),
                disabled=ai_config.breaks.enabled,
            )
            breaks_off_button = discord.ui.Button(
                label="Breaks Off",
                style=(
                    discord.ButtonStyle.danger
                    if not ai_config.breaks.enabled else
                    discord.ButtonStyle.secondary
                ),
                disabled=not ai_config.breaks.enabled,
            )
            breaks_edit_button = discord.ui.Button(
                label="Edit Breaks",
                style=discord.ButtonStyle.secondary,
            )
            on_button.callback = self.enable_ai
            off_button.callback = self.disable_ai
            edit_button.callback = self.edit_custom_prompt
            breaks_on_button.callback = self.enable_breaks
            breaks_off_button.callback = self.disable_breaks
            breaks_edit_button.callback = self.edit_breaks

            custom = prompt_preview(ai_config.custom_instruction_text) or "-# Empty"
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay("## AI"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"### Base Instructions\n{prompt_preview(instruction_text_base())}"),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"### Custom Instructions\n{custom}"),
                discord.ui.ActionRow(edit_button),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"### Status\nAI enabled: `{ai_config.enabled}`"),
                discord.ui.ActionRow(on_button, off_button),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "### Breaks\n"
                    f"Enabled: `{ai_config.breaks.enabled}`\n"
                    f"Active minutes: `{ai_config.breaks.active_minutes:g}`\n"
                    f"Break minutes: `{ai_config.breaks.break_minutes:g}`"
                ),
                discord.ui.ActionRow(breaks_on_button, breaks_off_button, breaks_edit_button),
            ))

        async def set_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
            ai_config.enabled = enabled
            ai_core.configure(enabled=enabled, base_url=ai_config.base_url)
            await save_ai_config()
            await interaction.response.edit_message(view=AIManagementView())

        async def enable_ai(self, interaction: discord.Interaction) -> None:
            await self.set_enabled(interaction, True)

        async def disable_ai(self, interaction: discord.Interaction) -> None:
            await self.set_enabled(interaction, False)

        async def edit_custom_prompt(self, interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AICustomInstructionModal())

        async def set_breaks_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
            ai_config.breaks.enabled = enabled
            await save_ai_config()
            await restart_break_task()
            await interaction.response.edit_message(view=AIManagementView())

        async def enable_breaks(self, interaction: discord.Interaction) -> None:
            await self.set_breaks_enabled(interaction, True)

        async def disable_breaks(self, interaction: discord.Interaction) -> None:
            await self.set_breaks_enabled(interaction, False)

        async def edit_breaks(self, interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AIBreaksModal())

    class AICustomInstructionModal(discord.ui.Modal, title="AI Custom Instructions"):
        def __init__(self) -> None:
            super().__init__()
            self.custom_instruction_text = discord.ui.TextInput(
                label="Custom prompt portion",
                style=discord.TextStyle.paragraph,
                required=False,
                default=truncate_text_to_character_limit(
                    ai_config.custom_instruction_text,
                    4000
                ),
                max_length=4000,
            )
            self.add_item(self.custom_instruction_text)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            ai_config.custom_instruction_text = self.custom_instruction_text.value.strip()
            await save_ai_config()
            if interaction.message:
                await interaction.message.edit(
                    view=AIManagementView(),
                )
            await interaction.response.send_message("Updated custom instructions.")

    class AIBreaksModal(discord.ui.Modal, title="AI Breaks"):
        def __init__(self) -> None:
            super().__init__()
            self.active_minutes = discord.ui.TextInput(
                label="Active minutes",
                required=True,
                default=f"{ai_config.breaks.active_minutes:g}",
                max_length=16,
            )
            self.break_minutes = discord.ui.TextInput(
                label="Break minutes",
                required=True,
                default=f"{ai_config.breaks.break_minutes:g}",
                max_length=16,
            )
            self.add_item(self.active_minutes)
            self.add_item(self.break_minutes)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            try:
                active_minutes = max(0.0, float(self.active_minutes.value.strip()))
                break_minutes = max(0.0, float(self.break_minutes.value.strip()))
            except ValueError:
                await interaction.response.send_message(
                    "Break values must be numbers.",
                    ephemeral=True,
                )
                return

            ai_config.breaks.active_minutes = active_minutes
            ai_config.breaks.break_minutes = break_minutes
            await save_ai_config()
            await restart_break_task()
            if interaction.message:
                await interaction.message.edit(
                    view=AIManagementView(),
                )
            await interaction.response.send_message("Updated AI break settings.")

    @manage.command(
        name="ai",
        description="Manage AI settings",
        capabilities=["ai.manage"],
        defer=False,
    )
    async def manage_ai(interaction: discord.Interaction) -> None:
        await bot.discord.send(
            view=AIManagementView(),
            response=True,
            ephemeral=True,
        )

    async def ai_break_cycle() -> None:
        global _ai_break_until
        breaks = ai_break_config(bot)
        active_minutes = breaks.active_minutes
        break_minutes = breaks.break_minutes
        if active_minutes <= 0 or break_minutes <= 0:
            return

        while not bot.is_closed():
            _ai_break_until = None
            await asyncio.sleep(active_minutes * 60)
            _ai_break_until = discord.utils.utcnow() + datetime.timedelta(minutes=break_minutes)
            await bot.discord.change_presence(status=discord.Status.idle)
            await asyncio.sleep(break_minutes * 60)
            _ai_break_until = None
            await bot.discord.change_presence(status=discord.Status.online)

    @bot.message_callback
    async def on_message(message: discord.Message):
        if message.author.bot or bot.user is None:
            return
        
        from plugins._relay import LIVE_CHAT_SUB # type: ignore
        if await bot.notifications.has_subscription(LIVE_CHAT_SUB, message.channel.id):
            return
        
        if not ai_enabled(bot) or banned(bot, message.author):
            return
        guild_id = message.guild.id if message.guild is not None else None
        if not can_use_ai(bot, message.author, guild_id):
            return

        text = mentioned_message_text(bot, message)
        if text is None:
            return
        if ai_on_break():
            return
        assistant_context_message = replied_assistant_message(bot, message)
        assistant_context = assistant_context_message[1] if assistant_context_message is not None else None
        assistant_context_source = assistant_context_message[0] if assistant_context_message is not None else None
        requested_context = await context_request_executor.execute(message, text)
        lock_token = ai_core.lock_token()
        try:
            async with message.channel.typing():
                matches = await ai_core.ai_turn(
                    text,
                    source=message,
                    assistant_context=assistant_context,
                    assistant_context_source=assistant_context_source,
                    requested_context=requested_context,
                    lock_token=lock_token,
                )
                if not matches:
                    return

            followup_only = False
            for match in matches:
                if match.reply is not None:
                    reply = ai_core.visual_reply(match.reply)
                    if reply is None:
                        continue
                    chunks = chunk_text(reply, MAX_REPLY_CHARS)
                    if len(chunks) < 1:
                        continue
                    
                    sent_message: discord.Message | None = None
                    if match.respond:
                        if not followup_only:
                            sent_message = await message.reply(
                                chunks[0],
                                allowed_mentions=discord.AllowedMentions.none(),
                                mention_author=False
                            )
                            followup_only = True
                            chunks = chunks[1:]
                        for reply in chunks:
                            sent_message = await message.channel.send(
                                reply,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                    if match.after_execution is not None:
                        match.after_execution(sent_message)
                    continue
                if match.action is None:
                    continue

                interaction = MessageInteraction(
                    bot,
                    message,
                    match.command_name,
                    followup_only=followup_only,
                )

                async with capture_interaction_output(interaction) as output_messages:
                    capabilities = bot.setup._normalize_capabilities((
                        bot.setup._default_capability(match.command_name),
                        *match.context.get("capabilities", ()),
                    ))
                    await bot.setup._run_command(
                        interaction,
                        match.action,
                        (),
                        match.kwargs or {},
                        capabilities=capabilities,
                        eph=False,
                        defer=False,
                    )
                if len(output_messages) > 0:
                    followup_only = True
                if match.after_execution is not None:
                    match.after_execution(output_messages[-1] if output_messages else None)
        finally:
            lock_token.release()

    @bot.setup.command(
        name="ai",
        description="Ask Bogobot",
        capabilities=[USER_AI_CAPABILITY],
        defer=False,
        eph=False,
    )
    async def ai(interaction: discord.Interaction, prompt: str):
        if not ai_enabled(bot):
            await bot.discord.send(
                contents="AI is disabled.",
                response=True,
                ephemeral=True,
            )
            return
        if ai_on_break():
            return

        await bot.discord.defer(ephemeral=False)
        requested_context = await context_request_executor.execute(interaction, prompt)
        lock_token = ai_core.lock_token()
        try:
            matches = await ai_core.ai_turn(
                prompt,
                source=interaction,
                requested_context=requested_context,
                lock_token=lock_token,
            )
            if not matches:
                await bot.discord.send(
                    contents="I'm not sure I understand.",
                    response=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            has_responded = False
            for match in matches:
                if match.reply is not None:
                    reply = ai_core.visual_reply(match.reply)
                    if reply is None:
                        continue
                    chunks = chunk_text(reply, MAX_REPLY_CHARS)
                    if len(chunks) < 1:
                        continue
                    
                    sent_message = None
                    if match.respond:
                        for reply in chunks:
                            sent_message = await bot.discord.send(
                                contents=reply,
                                response=True,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                    has_responded = has_responded or sent_message is not None
                    if match.after_execution is not None:
                        match.after_execution(sent_message.message if sent_message is not None else None)
                    continue
                if match.action is None:
                    continue

                async with capture_interaction_output(interaction) as output_messages:
                    capabilities = bot.setup._normalize_capabilities((
                        bot.setup._default_capability(match.command_name),
                        *match.context.get("capabilities", ()),
                    ))
                    await bot.setup._run_command(
                        interaction,
                        match.action,
                        (),
                        match.kwargs or {},
                        capabilities=capabilities,
                        eph=False,
                        defer=False,
                    )
                has_responded = has_responded or len(output_messages) > 0
                if match.after_execution is not None:
                    match.after_execution(output_messages[-1] if output_messages else None)
            if not has_responded:
                await bot.discord.cleanup_defer_status(interaction)
                await bot.discord.send(
                    contents="The assistant did not provide a response.",
                    ephemeral=True
                )
        finally:
            lock_token.release()

    @bot.init_callback
    async def init():
        nonlocal break_task
        global _bot_mention_text, _bot_display_name
        if not bot.user:
            return
        _bot_mention_text = (
            f'<@{bot.user.id} {json_string(bot.user.name)}>'
            if ai_core.normalize_discord else
            f'<@{bot.user.id}>'
        )
        _bot_display_name = bot.user.display_name
        if ai_break_config(bot).enabled and (break_task is None or break_task.done()):
            break_task = asyncio.create_task(ai_break_cycle())

    @bot.close_callback
    async def close():
        global _ai_break_until
        _ai_break_until = None
        if break_task is not None and not break_task.done():
            break_task.cancel()
