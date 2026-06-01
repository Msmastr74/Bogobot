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

from typing import TYPE_CHECKING, Any, Optional, Sequence, TypedDict, TypeAlias, Callable, cast
from utils.ai_context import ContextRequest, close_system_tag, open_system_tag
from utils.discord import chunk_text, split_text_to_character_limit
from utils.type import Coro

if TYPE_CHECKING:
    from bogobot_core import BotCore
from dataclasses import dataclass

INSTRUCTION_TEXT = (
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

class BotActionParameters(TypedDict, total=False):
    perm_requirement: int

BotAction: TypeAlias = Callable[..., Coro[None]]
MAX_ASSISTANT_CONTEXT_CHARS = 4500
MAX_REPLY_CHARS = 2000
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


def ai_config(bot: 'BotCore') -> dict[str, Any]:
    config = bot.config.get("ai", {})
    if not isinstance(config, dict):
        raise TypeError("Config key 'ai' must be an object.")
    return config


def ai_break_config(bot: 'BotCore') -> dict[str, Any]:
    config = ai_config(bot).get("breaks", {})
    return config if isinstance(config, dict) else {}


def ai_enabled(bot: 'BotCore') -> bool:
    return bool(ai_config(bot).get("enabled", True))


class ContextRequestExecutor:
    def __init__(self, bot: "BotCore"):
        from utils.ai import ai as ai_core

        self.bot = bot
        self.ai_core = ai_core

    async def execute(
        self,
        source: 'discord.Message | discord.Interaction | discord.abc.MessageableChannel',
        text: str,
    ) -> str | None:
        channel_id = self._source_channel_id(source)
        requests = self.ai_core.context.query_context_requests(
            channel_id=channel_id
        )
        if not requests:
            return None

        blocks: list[str] = []
        for request in requests:
            content = await self._execute_request(request, source, text)
            self.ai_core.context.discard_context_request(request)
            if content is None:
                continue
            blocks.append(self._format_requested_context(request, content))

        return "\n".join(blocks) if blocks else None

    async def _execute_request(
        self,
        request: ContextRequest,
        source: 'discord.Message | discord.Interaction | discord.abc.MessageableChannel',
        text: str,
    ) -> str | None:
        if request.type == "user":
            return self._user_context(request, source)
        if request.type == "stream":
            return self._stream_context()
        if request.type == "minigame":
            return await self._minigame_context(request)
        if request.type == "milestone":
            return await self._milestone_context()
        return None

    def _user_context(
        self,
        request: ContextRequest,
        source: 'discord.Message | discord.Interaction | discord.abc.MessageableChannel',
    ) -> str | None:
        user_id = self._payload_user_id(request) or self._source_user_id(source)
        if user_id is None:
            return None
        guild = getattr(source, "guild", None)
        user = guild.get_member(user_id) if guild is not None else self.bot.get_user(user_id)
        if user is None:
            return f"user_id: {user_id}\nknown: false"
        return (
            f"user_id: {user.id}\n"
            f"username: {user.name}\n"
            f"display_name: {json_string(user.display_name)}\n"
            f"bot: {user.bot}"
        )

    def _stream_context(self) -> str:
        return f"stream_uptime: {self.bot.get_stream_uptime()}"

    async def _minigame_context(self, request: ContextRequest) -> str | None:
        game = str(request.payload.get("game") or request.payload.get("query") or "").strip().casefold()
        if game in ("bogotree", "tree"):
            data = await self._json_file("bogotree.json")
            raw_state = data.get("state")
            state = raw_state if isinstance(raw_state, dict) else {}
            return self._bogotree_context(state)
        if game in ("cbogo", "community_bogosort", "community-bogosort"):
            data = await self._json_file("cbogo.json")
            raw_state = data.get("state")
            state = raw_state if isinstance(raw_state, dict) else data
            return self._cbogo_context(state)
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

    async def _milestone_context(self) -> str | None:
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
        source: 'discord.Message | discord.Interaction | discord.abc.MessageableChannel',
    ) -> int | None:
        if isinstance(source, discord.Message):
            return source.author.id
        if isinstance(source, discord.Interaction):
            return source.user.id
        return None

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
    from utils.ai import ai as ai_core

    bot.event(bot.on_message)
    break_task: asyncio.Task[None] | None = None
    context_request_executor = ContextRequestExecutor(bot)

    async def ai_break_cycle() -> None:
        global _ai_break_until
        breaks = ai_break_config(bot)
        active_minutes = max(0.0, float(breaks.get("active_minutes", 20)))
        break_minutes = max(0.0, float(breaks.get("break_minutes", 10)))
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
        
        if not ai_enabled(bot):
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
        
        async with message.channel.typing():
            matches = await ai_core.ai_turn(
                text,
                source=message,
                assistant_context=assistant_context,
                assistant_context_source=assistant_context_source,
                requested_context=requested_context,
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
                ai_core.context.record_message(
                    "assistant", match.reply, sent_message,
                    channel_id=message.channel.id
                )
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
                await bot.setup._run_command(
                    interaction,
                    match.action,
                    (),
                    match.kwargs or {},
                    perm_requirement=match.context.get("perm_requirement", 0),
                    eph=False,
                    defer=False,
                )
            if len(output_messages) > 0:
                followup_only = True
            ai_core.context.record_message(
                "assistant",
                ai_core.context.format_command_call(match.command_name, match.kwargs),
                output_messages[-1] if output_messages else None,
                channel_id=message.channel.id,
            )

    @bot.setup.command(
        name="ai",
        description="Ask Bogobot",
        perm_requirement=0,
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
        matches = await ai_core.ai_turn(prompt, source=interaction, requested_context=requested_context)
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
                ai_core.context.record_message(
                    "assistant",
                    match.reply,
                    sent_message.message if sent_message is not None else None,
                    channel_id=interaction.channel_id,
                )
                continue
            if match.action is None:
                continue

            async with capture_interaction_output(interaction) as output_messages:
                await bot.setup._run_command(
                    interaction,
                    match.action,
                    (),
                    match.kwargs or {},
                    perm_requirement=match.context.get("perm_requirement", 0),
                    eph=False,
                    defer=False,
                )
            has_responded = has_responded or len(output_messages) > 0
            ai_core.context.record_message(
                "assistant",
                ai_core.context.format_command_call(match.command_name, match.kwargs),
                output_messages[-1] if output_messages else None,
                channel_id=interaction.channel_id,
            )
        if not has_responded:
            await bot.discord.cleanup_defer_status(interaction)
            await bot.discord.send(
                contents="The assistant did not provide a response.",
                ephemeral=True
            )

    @bot.init_callback
    async def init():
        nonlocal break_task
        global INSTRUCTION_TEXT
        if not bot.user:
            return
        INSTRUCTION_TEXT = INSTRUCTION_TEXT.replace(
            "@Bogobot",
            f'<@{bot.user.id} {json_string(bot.user.name)}>' if ai_core.normalize_discord else f'<@{bot.user.id}>'
        ).replace(
            "[DISPLAY_NAME]",
            bot.user.display_name
        )
        if bool(ai_break_config(bot).get("enabled", True)) and (break_task is None or break_task.done()):
            break_task = asyncio.create_task(ai_break_cycle())

    @bot.close_callback
    async def close():
        global _ai_break_until
        _ai_break_until = None
        if break_task is not None and not break_task.done():
            break_task.cancel()
