import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING, cast

import discord
from utils.tracker import Tracker

if TYPE_CHECKING:
    from logging import Logger


class NotificationBroadcaster:
    def __init__(
        self,
        bot: discord.Client,
        *,
        subscriptions: dict[str, Any],
        save_subscriptions: Callable[[dict[str, Any]], Awaitable[None]],
        logger: "Logger | None" = None,
    ):
        self.bot = bot
        self.subscriptions = subscriptions
        self.save_subscriptions = save_subscriptions
        self.logger = logger
        self._ready = asyncio.Event()
        self.tracker = Tracker[int, list[str]](
            load=self._load_store,
            save=self._save_store,
            normalize=self._normalize_subscription,
            validate=self._validate_subscription,
        )

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    async def initialize(self) -> None:
        try:
            await self.tracker.load()
            await self.tracker.prune_stale()
        finally:
            self._ready.set()

    async def _load_store(self) -> dict[str, Any]:
        return self.subscriptions

    async def _save_store(self, subscriptions: dict[str, list[str]]) -> None:
        self.subscriptions = subscriptions
        await self.save_subscriptions(self.subscriptions)

    async def _normalize_subscription(self, channel_id_str: str, topics: Any) -> tuple[int, list[str]] | None:
        try:
            channel_id = int(channel_id_str)
        except ValueError:
            return None

        if isinstance(topics, list):
            normalized_topics = sorted({
                topic for topic in topics if isinstance(topic, str)
            })
        elif isinstance(topics, str):
            normalized_topics = [topics]
        else:
            return None

        if not normalized_topics:
            return None

        return channel_id, normalized_topics

    async def _validate_subscription(self, channel_id: int, topics: list[str]) -> bool:
        return bool(topics) and self._can_send_to(channel_id)

    def _can_send_to(self, channel_id: int) -> bool:
        channel = self.bot.get_channel(channel_id)
        return channel is not None and hasattr(channel, "send")

    async def subscribe(self, topic: str, channel_id: int) -> bool:
        if not self._can_send_to(channel_id):
            return False

        store = await self.tracker.items()
        topics = store.setdefault(channel_id, [])

        if topic not in topics:
            topics.append(topic)
            topics.sort()
            await self.tracker.set(channel_id, topics)

        return True

    async def unsubscribe(self, topic: str, channel_id: int) -> bool:
        store = await self.tracker.items()
        topics = store.get(channel_id)

        if topics is None or topic not in topics:
            return False

        topics.remove(topic)

        if topics:
            await self.tracker.set(channel_id, topics)
        else:
            await self.tracker.remove(channel_id)

        return True

    async def has_subscription(self, topic: str, channel_id: int) -> bool:
        store = await self.tracker.items()
        return topic in store.get(channel_id, [])

    async def channel_ids(self, topic: str) -> list[int]:
        store = await self.tracker.items()
        channel_ids: list[int] = []

        for channel_id, topics in store.items():
            if topic not in topics:
                continue

            channel_ids.append(channel_id)

        return channel_ids

    async def notify(
        self,
        topic: str,
        **kwargs: Any,
    ) -> int:
        sent = 0
        stale_channel_ids: list[int] = []

        for channel_id in await self.channel_ids(topic):
            channel = self.bot.get_channel(channel_id)

            if channel is None or not hasattr(channel, "send"):
                stale_channel_ids.append(channel_id)
                continue

            try:
                await cast(Any, channel).send(**kwargs)
            except (discord.NotFound, discord.Forbidden):
                stale_channel_ids.append(channel_id)
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(
                        f"Notification failed for topic {topic!r} in channel {channel_id}",
                        exc_info=exc,
                    )
            else:
                sent += 1

        for channel_id in stale_channel_ids:
            await self.unsubscribe(topic, channel_id)

        return sent

    async def close(self) -> None:
        pass
