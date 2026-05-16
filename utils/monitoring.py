import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TYPE_CHECKING, cast

import discord
from discord.ext import tasks

from utils.tracker import Tracker

if TYPE_CHECKING:
    from bogobot_core import BotCore
    from utils.edit_coalescer import MessageEditCoalescer


MessagePayload = Mapping[str, Any]
PayloadFactory = Callable[[], MessagePayload | Awaitable[MessagePayload]]
UpdateFactory = Callable[[], MessagePayload | None | Awaitable[MessagePayload | None]]


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class PersistentChannelMonitor:
    def __init__(
        self,
        bot: "BotCore",
        *,
        storage_key: str,
        display_name: str,
        initial_payload: PayloadFactory,
        update_payload: UpdateFactory,
        interval_seconds: float = 1,
    ):
        self.bot = bot
        self.storage_key = storage_key
        self.display_name = display_name
        self.initial_payload = initial_payload
        self.update_payload = update_payload
        self.tracker = Tracker[int, int](
            load=self._load_messages,
            save=self._save_messages,
            normalize=self._normalize_message,
            validate=self._validate_message,
        )
        self._loop = tasks.loop(seconds=interval_seconds)(self._tick)

    async def initialize(self) -> None:
        await self.tracker.load()
        await self.tracker.prune_stale()

    def start(self) -> None:
        if not self._loop.is_running():
            self._loop.start()

    def command(
        self,
        group: Any,
        *,
        name: str,
        description: str,
    ) -> None:
        @group.command(name=name, description=description)
        async def monitor_command(
            interaction: discord.Interaction,
            action: Literal["start", "stop"],
        ):
            await self.handle_command(interaction, action)

    async def handle_command(
        self,
        interaction: discord.Interaction,
        action: Literal["start", "stop"],
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

        if existing_message_id is not None:
            await self.bot.discord.send(
                f"{self.display_name} is already running in this channel.",
                response=True,
            )
            return

        payload: MessagePayload = dict(await _resolve(self.initial_payload()))

        try:
            message = await self.bot.discord.send(
                response=False,
                **payload,
            )
        except (discord.NotFound, discord.Forbidden):
            message = None

        if message is None or message.message is None:
            await self.bot.discord.send(
                "Failed to send message to this channel.",
                response=True,
            )
            return

        self.bot.edits.register(message.message)
        await self.tracker.set(channel_id, message.message.id)
        await self.bot.discord.send(
            f"{self.display_name} online in this channel.",
            response=True,
        )

    async def _tick(self) -> None:
        await self.tracker.prune_stale()
        stored_messages = await self.tracker.items()

        if not stored_messages:
            return

        payload: MessagePayload | None = await _resolve(self.update_payload())
        if payload is None:
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
