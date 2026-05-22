from __future__ import annotations

import datetime
import random
import re

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

class BotActionParameters(TypedDict, total=False):
    perm_requirement: int

BotAction: TypeAlias = Callable[[discord.Interaction], Coro[None]]
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):[0-9]{15,20}>")

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
        self._response_type = None
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
    )

    def __init__(self, bot: "BotCore", message: discord.Message, command_name: str):
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

    text = message.clean_content
    bot_mention = discord.utils.get(message.mentions, id=bot.user.id)
    bot_names = {
        getattr(bot_mention, "display_name", None),
        getattr(bot_mention, "name", None),
        getattr(bot.user, "display_name", None),
        getattr(bot.user, "name", None),
        str(bot.user),
    }
    for name in filter(None, bot_names):
        text = text.replace(f"@{name}", " ")

    text = CUSTOM_EMOJI_RE.sub(r":\1:", text)
    text = " ".join(text.split())
    return text or None


async def default_handler(message: discord.Message) -> None:
    responses = (
        "I'm not sure I understand.",
        "I'm not sure what you mean.",
        "I don't think I know that one.",
        "Hmm. I don't understand that yet.",
        "I'm not sure how to respond to that.",
    )
    await message.reply(
        random.choice(responses),
        mention_author=False,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def setup(bot: 'BotCore'):
    from utils.nl import nl

    bot.event(bot.on_message)

    @bot.message_callback
    async def on_message(message: discord.Message):
        if message.author.bot or bot.user is None:
            return

        text = mentioned_message_text(bot, message)
        if text is None:
            return

        match = await nl.match_info(text)
        if match is None:
            await default_handler(message)
            return

        interaction = MessageInteraction(bot, message, match.name)

        await bot.setup._run_command(
            interaction,
            match.action,
            (),
            {},
            perm_requirement=match.context.get("perm_requirement", 0),
            eph=False,
            defer=False,
        )
