import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal
from utils.type import ObjectWithCommandDecorator, P, R, T

import discord

from utils.tracker import Tracker

from bogobot_core import BotCore
from utils.edit_coalescer import MessageEditCoalescer

MessagePayload = Mapping[str, Any]
UpdatePayload = MessagePayload
PayloadFactory = Callable[[], MessagePayload | Awaitable[MessagePayload]]

async def _resolve(value: Awaitable[T] | T) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


class PersistentChannelMonitor:
    def __init__(
        self,
        bot: BotCore,
        *,
        storage_key: str,
        display_name: str,
        initial_payload: PayloadFactory,
    ):
        self.bot = bot
        self.storage_key = storage_key
        self.display_name = display_name
        self.initial_payload = initial_payload
        self.tracker = Tracker[int, int](
            load=self._load_messages,
            save=self._save_messages,
            normalize=self._normalize_message,
            validate=self._validate_message,
        )

    async def initialize(self) -> None:
        await self.tracker.load()
        await self.tracker.prune_stale()

    def command(
        self,
        root: ObjectWithCommandDecorator[P, R],
        *args: P.args,
        **kwargs: P.kwargs
    ):
        @root.command(*args, **kwargs)
        async def monitor_command(
            interaction: discord.Interaction,
            action: Literal["start", "stop", "resend"],
        ):
            await self.handle_command(interaction, action)
        return monitor_command

    async def handle_command(
        self,
        interaction: discord.Interaction,
        action: Literal["start", "stop", "resend"],
    ) -> None:
        channel_id = interaction.channel_id

        if channel_id is None:
            await self.bot.discord.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        existing_message_id = await self.tracker.get(channel_id)

        if action == "stop":
            if existing_message_id is None:
                await self.bot.discord.send(
                    f"{self.display_name} is not currently running in this channel.",
                    response=True,
                )
                return

            await self.tracker.remove(channel_id)
            await self._delete_message(channel_id, int(existing_message_id))
            await self.bot.discord.send(
                f"{self.display_name} stopped in this channel.",
                response=True,
            )
            return

        if action == "resend":
            if existing_message_id is None:
                await self.bot.discord.send(
                    f"{self.display_name} is not currently running in this channel.",
                    response=True,
                )
                return

            existing = await self._ensure_message(channel_id, int(existing_message_id))
            if existing is None:
                await self.bot.discord.send(
                    f"{self.display_name} message is not accessible.",
                    response=True,
                )
                return

            message = await self._send_initial_message()
            if message is None:
                await self.bot.discord.send(
                    "Failed to send replacement message to this channel.",
                    response=True,
                )
                return

            self.bot.edits.register(message)
            await self.tracker.set(channel_id, message.id)
            await self._delete_message(channel_id, int(existing_message_id))
            await self.bot.discord.send(
                f"{self.display_name} resent in this channel.",
                response=True,
            )
            return

        if existing_message_id is not None:
            await self.bot.discord.send(
                f"{self.display_name} is already running in this channel.",
                response=True,
            )
            return

        message = await self._send_initial_message()
        if message is None:
            await self.bot.discord.send(
                "Failed to send message to this channel.",
                response=True,
            )
            return

        self.bot.edits.register(message)
        await self.tracker.set(channel_id, message.id)
        await self.bot.discord.send(
            f"{self.display_name} online in this channel.",
            response=True,
        )

    async def update(self, payload: UpdatePayload) -> None:
        await self.tracker.prune_stale()
        stored_messages = await self.tracker.items()

        if not stored_messages:
            return

        for channel_id, message_id in list(stored_messages.items()):
            coalescer = await self._ensure_message(channel_id, message_id)
            if coalescer is None:
                continue
            await coalescer.edit(wait=False, **dict(payload))

    async def _load_messages(self) -> dict[str, Any]:
        messages = self.bot.config.get(self.storage_key)
        return messages if isinstance(messages, dict) else {}

    async def _save_messages(self, messages: dict[str, int]) -> None:
        self.bot.config[self.storage_key] = messages
        await self.bot.save_config()

    async def _normalize_message(
        self,
        channel_id_str: str,
        message_id: Any,
    ) -> tuple[int, int] | None:
        try:
            return int(channel_id_str), int(message_id)
        except (TypeError, ValueError):
            return None

    async def _validate_message(self, channel_id: int, message_id: int) -> bool:
        coalescer = await self._ensure_message(channel_id, message_id)
        if coalescer is None:
            return False

        if coalescer.NotFound_or_Forbidden:
            await self.bot.edits.delete(message_id)
            return False

        return True

    async def _send_initial_message(self) -> discord.Message | None:
        payload = await _resolve(self.initial_payload())

        try:
            message = await self.bot.discord.send(
                response=False,
                **payload,
            )
        except (discord.NotFound, discord.Forbidden):
            return None

        if message is None or message.message is None:
            return None

        return message.message

    async def _ensure_message(
        self,
        channel_id: int,
        message_id: int,
    ) -> "MessageEditCoalescer | None":
        existing = self.bot.edits.get(message_id)
        if existing is not None:
            return existing

        message = self._partial_message(channel_id, message_id)

        return self.bot.edits.register(message)

    def _partial_message(
        self,
        channel_id: int,
        message_id: int,
    ) -> discord.PartialMessage:
        channel = self.bot.get_partial_messageable(channel_id)

        return channel.get_partial_message(message_id)

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        if await self.bot.edits.delete(message_id):
            return

        message = self._partial_message(channel_id, message_id)
        if message is None:
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
