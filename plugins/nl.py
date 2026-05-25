from __future__ import annotations

import asyncio
import datetime
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

INSTRUCTION_TEXT = (
    "You are Bogobot (@Bogobot), a helpful Discord bot with a dry, casual tone. "
    "You live in Discord and answer naturally when chatted with. Keep replies calm, concise, and not overly enthusiastic. "
    "You are triggered by a user mentioning you in a message or replying to a message by you. "
    "If a user triggered you by mention, they will often send a message like '@Bogobot hello!' instead of just 'hello!'. "
    "Discord emojis are in the format <:emoji_name:123456789012345678>. If the user sends only emoji, you may reply with the same Discord emoji or Unicode emoji. "
    'Discord users or members are approximately in the format <@id "User Name"> or <@!id "User Name">. Discord roles are in the format <@&id>. Discord channels are in the format <#id>.'
)

class BotActionParameters(TypedDict, total=False):
    perm_requirement: int

BotAction: TypeAlias = Callable[..., Coro[None]]
ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")
USER_MENTION_RE = re.compile(r"<(@!?)([0-9]{15,20})>")
ROLE_MENTION_RE = re.compile(r"<@&([0-9]{15,20})>")
CHANNEL_MENTION_RE = re.compile(r"<#([0-9]{15,20})>")
MAX_ASSISTANT_CONTEXT_CHARS = 3000
_nl_break_until: datetime.datetime | None = None

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

def mentioned_message_text(
    bot: 'BotCore',
    message: discord.Message,
    *,
    normalize_discord: bool = True,
) -> str | None:
    if bot.user is None or bot.user not in message.mentions:
        return None

    text = message.content
    if normalize_discord:
        text = annotate_discord_references(message, text)

    text = " ".join(text.split())
    return text or None


def replied_assistant_text(
    bot: 'BotCore',
    message: discord.Message,
    *,
    normalize_discord: bool = True,
) -> str | None:
    if bot.user is None or message.reference is None:
        return None

    resolved = message.reference.resolved
    if not isinstance(resolved, discord.Message):
        return None
    if resolved.author.id != bot.user.id:
        return None

    text = resolved.content
    if normalize_discord:
        text = annotate_discord_references(resolved, text)

    text = " ".join(text.split())
    if not text:
        return None
    return text[:MAX_ASSISTANT_CONTEXT_CHARS]


def annotate_discord_references(message: discord.Message, text: str) -> str:
    user_names = {
        str(user.id): _discord_reference_name(user)
        for user in message.mentions
    }
    role_names = {
        str(role.id): role.name
        for role in getattr(message, "role_mentions", ())
    }
    channel_names = {
        str(channel.id): channel.name
        for channel in getattr(message, "channel_mentions", ())
        if getattr(channel, "name", None) is not None
    }

    def annotate_user(match: re.Match[str]) -> str:
        prefix, snowflake = match.groups()
        name = user_names.get(snowflake)
        if name is None:
            return match[0]
        return f"<{prefix}{snowflake} {json_string(name)}>"

    def annotate_role(match: re.Match[str]) -> str:
        snowflake = match[1]
        name = role_names.get(snowflake)
        if name is None:
            return match[0]
        return f"<@&{snowflake} {json_string(name)}>"

    def annotate_channel(match: re.Match[str]) -> str:
        snowflake = match[1]
        name = channel_names.get(snowflake)
        if name is None:
            return match[0]
        return f"<#{snowflake} {json_string(name)}>"

    text = USER_MENTION_RE.sub(annotate_user, text)
    text = ROLE_MENTION_RE.sub(annotate_role, text)
    return CHANNEL_MENTION_RE.sub(annotate_channel, text)


def strip_discord_reference_annotations(text: str) -> str:
    return ANNOTATED_DISCORD_REFERENCE_RE.sub(r"<\1\2>", text)


def json_string(text: str) -> str:
    import json
    return json.dumps(text, ensure_ascii=False)


def _discord_reference_name(entity: Any) -> str:
    return str(
        getattr(entity, "display_name", None) or
        getattr(entity, "global_name", None) or
        getattr(entity, "name", None) or
        entity
    )


def nl_on_break() -> bool:
    return _nl_break_until is not None and discord.utils.utcnow() < _nl_break_until


async def setup(bot: 'BotCore'):
    from utils.nl import nl

    bot.event(bot.on_message)
    break_task: asyncio.Task[None] | None = None

    async def nl_break_cycle() -> None:
        global _nl_break_until
        active_minutes = max(0.0, float(bot.config.get("nl_active_minutes", 20)))
        break_minutes = max(0.0, float(bot.config.get("nl_break_minutes", 10)))
        if active_minutes <= 0 or break_minutes <= 0:
            return

        while not bot.is_closed():
            _nl_break_until = None
            await asyncio.sleep(active_minutes * 60)
            _nl_break_until = discord.utils.utcnow() + datetime.timedelta(minutes=break_minutes)
            await bot.discord.change_presence(status=discord.Status.idle)
            await asyncio.sleep(break_minutes * 60)
            _nl_break_until = None
            await bot.discord.change_presence(status=discord.Status.online)

    @bot.message_callback
    async def on_message(message: discord.Message):
        if message.author.bot or bot.user is None:
            return
        if not bool(bot.config.get("nl", True)):
            return

        normalize_discord = bool(bot.config.get("nl_normalize_discord", True))
        text = mentioned_message_text(
            bot,
            message,
            normalize_discord=normalize_discord,
        )
        if text is None:
            return
        if nl_on_break():
            return
        assistant_context = replied_assistant_text(
            bot,
            message,
            normalize_discord=normalize_discord,
        )
        
        async with message.channel.typing():
            matches = await nl.match_infos(
                text,
                message=message,
                assistant_context=assistant_context,
            )
            if not matches:
                return

        for index, match in enumerate(matches):
            followup_only = index > 0
            if match.reply is not None:
                reply = strip_discord_reference_annotations(match.reply)
                if followup_only:
                    await message.channel.send(
                        reply,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await message.reply(
                        reply,
                        allowed_mentions=discord.AllowedMentions.none(),
                        mention_author=False
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

            await bot.setup._run_command(
                interaction,
                match.action,
                (),
                match.kwargs or {},
                perm_requirement=match.context.get("perm_requirement", 0),
                eph=False,
                defer=False,
            )

    @bot.setup.command(
        name="ai",
        description="Ask Bogobot",
        perm_requirement=0,
        defer=False,
        eph=False,
    )
    async def ai(interaction: discord.Interaction, prompt: str):
        if not bool(bot.config.get("nl", True)):
            await bot.discord.send(
                contents="NL is disabled.",
                response=True,
                ephemeral=True,
            )
            return
        if nl_on_break():
            return

        await bot.discord.defer(ephemeral=False)
        matches = await nl.match_infos(prompt, interaction=interaction)
        if not matches:
            await bot.discord.send(
                contents="I'm not sure I understand.",
                response=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        for match in matches:
            if match.reply is not None:
                await bot.discord.send(
                    contents=strip_discord_reference_annotations(match.reply),
                    response=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                continue
            if match.action is None:
                continue

            await bot.setup._run_command(
                interaction,
                match.action,
                (),
                match.kwargs or {},
                perm_requirement=match.context.get("perm_requirement", 0),
                eph=False,
                defer=False,
            )

    @bot.init_callback
    async def init():
        nonlocal break_task
        global INSTRUCTION_TEXT
        if not bot.user:
            return
        INSTRUCTION_TEXT = INSTRUCTION_TEXT.replace(
            "@Bogobot",
            f'<@{bot.user.id} {json_string(bot.user.name)}>'
        )
        if bool(bot.config.get("nl_breaks", True)) and (break_task is None or break_task.done()):
            break_task = asyncio.create_task(nl_break_cycle())

    @bot.close_callback
    async def close():
        global _nl_break_until
        _nl_break_until = None
        if break_task is not None and not break_task.done():
            break_task.cancel()
