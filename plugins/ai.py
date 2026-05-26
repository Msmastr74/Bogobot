from __future__ import annotations

import asyncio
import datetime
from contextlib import asynccontextmanager

import discord
from discord import File
from discord.abc import Snowflake
from discord.embeds import Embed
from discord.mentions import AllowedMentions
from discord.poll import Poll
from discord.ui.view import BaseView

from typing import TYPE_CHECKING, Any, Optional, Sequence, TypedDict, TypeAlias, Callable, cast
from utils.type import Coro

if TYPE_CHECKING:
    from bogobot_core import BotCore
from dataclasses import dataclass

INSTRUCTION_TEXT = (
    "You are Bogobot (@Bogobot, display name [DISPLAY_NAME]), a helpful Discord bot with a casual tone. "
    "You live in Discord and answer naturally when chatted with. Keep replies calm, concise, and not overly enthusiastic. "
    "You are triggered by a user mentioning you in a message or replying to a message by you. "
    "If a user triggered you by mention, they will often send a message like '@Bogobot hello!' instead of just 'hello!'. "
    "Discord emojis are in the format <:emoji_name:123456789012345678>. If the user sends only emoji, you may reply with the same Discord emoji or Unicode emoji. "
    'Discord users or members are approximately in the format <@id "User Name"> or <@!id "User Name">. Discord roles are in the format <@&id>. Discord channels are in the format <#id>.'
)

class BotActionParameters(TypedDict, total=False):
    perm_requirement: int

BotAction: TypeAlias = Callable[..., Coro[None]]
MAX_ASSISTANT_CONTEXT_CHARS = 4000
_ai_break_until: datetime.datetime | None = None

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
        message = await self.interaction.source_message.channel.send(
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
        self._message = await self._message_interaction.source_message.reply(
            content,
            **kwargs,
        )


class MessageInteraction(discord.Interaction[discord.Client]):
    __slots__ = (
        "source_message",
        "_message_command",
        "_followup_only",
    )

    def __init__(
        self,
        bot: "BotCore",
        message: discord.Message,
        command_name: str,
        *,
        followup_only: bool = False,
    ):
        self._state = message._state
        self._client = bot
        self._session = getattr(self._state.http, "_HTTPClient__session")
        self._baton = None
        self._original_response = None
        self.id = message.id
        self.type = discord.InteractionType.application_command
        self.data = None
        self.application_id = bot.user.id if bot.user is not None else 0
        self.message = message
        self.source_message = message
        self.user = message.author
        self.channel = cast(Any, message.channel)
        self.guild_id = message.guild.id if message.guild is not None else None
        self.token = ""
        self.version = 1
        self.locale = discord.Locale.american_english
        self.guild_locale = None
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
            return self.source_message
        return response._message

    async def delete_original_response(self) -> None:
        response = cast(MessageInteractionResponse, self.response)
        if response._message is not None:
            await response._message.delete()
            response._message = None

    @property
    def created_at(self) -> datetime.datetime:
        return self.source_message.created_at

    @property
    def expires_at(self) -> datetime.datetime:
        return self.created_at + datetime.timedelta(minutes=15)

    def is_expired(self) -> bool:
        return False

def mentioned_message_text(bot: 'BotCore', message: discord.Message) -> str | None:
    if bot.user is None or bot.user not in message.mentions:
        return None

    text = " ".join(message.content.split())
    return text or None


def replied_assistant_message(bot: 'BotCore', message: discord.Message) -> tuple[discord.Message, str] | None:
    if bot.user is None or message.reference is None:
        return None

    resolved = message.reference.resolved
    if not isinstance(resolved, discord.Message):
        return None
    if resolved.author.id != bot.user.id:
        return None

    text = " ".join(resolved.content.split())
    if not text:
        return None
    return resolved, text[:MAX_ASSISTANT_CONTEXT_CHARS]

def json_string(text: str) -> str:
    import json
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


@asynccontextmanager
async def capture_interaction_output(interaction: discord.Interaction):
    output_messages: list[discord.Message] = []
    response = interaction.response
    followup = interaction.followup
    original_send_message = response.send_message
    original_followup_send = followup.send

    def add_message(message: discord.Message | None) -> None:
        if message is None:
            return
        if any(existing.id == message.id for existing in output_messages):
            return
        output_messages.append(message)

    async def send_message(*args: Any, **kwargs: Any) -> Any:
        result = await original_send_message(*args, **kwargs)
        if isinstance(result, discord.Message):
            add_message(result)
            return result
        try:
            response_message = await interaction.original_response()
        except discord.HTTPException:
            return result
        if isinstance(response_message, discord.Message):
            add_message(response_message)
        return result

    async def followup_send(*args: Any, **kwargs: Any) -> Any:
        caller_requested_wait = bool(kwargs.get("wait", False))
        kwargs["wait"] = True
        result = await original_followup_send(*args, **kwargs)
        add_message(result if isinstance(result, discord.Message) else None)
        return result if caller_requested_wait else None

    cast(Any, response).send_message = send_message
    cast(Any, followup).send = followup_send
    try:
        yield output_messages
    finally:
        cast(Any, response).send_message = original_send_message
        cast(Any, followup).send = original_followup_send


async def setup(bot: 'BotCore'):
    from utils.ai import ai as ai_core

    bot.event(bot.on_message)
    break_task: asyncio.Task[None] | None = None

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
        
        async with message.channel.typing():
            matches = await ai_core.ai_turn(
                text,
                source=message,
                assistant_context=assistant_context,
                assistant_context_source=assistant_context_source,
            )
            if not matches:
                return

        for index, match in enumerate(matches):
            followup_only = index > 0
            if match.reply is not None:
                reply = ai_core.visual_reply(match.reply)
                if reply is None:
                    continue
                if followup_only:
                    sent_message = await message.channel.send(
                        reply,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    sent_message = await message.reply(
                        reply,
                        allowed_mentions=discord.AllowedMentions.none(),
                        mention_author=False
                    )
                ai_core.record_message("assistant", match.reply, sent_message, channel_id=message.channel.id)
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
            ai_core.record_message(
                "assistant",
                ai_core.format_command_call(match.command_name, match.kwargs),
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
        matches = await ai_core.ai_turn(prompt, source=interaction)
        if not matches:
            await bot.discord.send(
                contents="I'm not sure I understand.",
                response=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        for match in matches:
            if match.reply is not None:
                reply = ai_core.visual_reply(match.reply)
                if reply is None:
                    continue
                sent_message = await bot.discord.send(
                    contents=reply,
                    response=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                ai_core.record_message(
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
            ai_core.record_message(
                "assistant",
                ai_core.format_command_call(match.command_name, match.kwargs),
                output_messages[-1] if output_messages else None,
                channel_id=interaction.channel_id,
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
