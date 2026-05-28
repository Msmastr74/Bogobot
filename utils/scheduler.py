import asyncio
from collections.abc import Awaitable, Callable
from logging import Logger
from typing import Any, Generic, TypedDict
from uuid import uuid4

import discord
from utils.tracker import Tracker
from utils.type import T

class NewSchedule(TypedDict, Generic[T]):
    payload: T


class Schedule(NewSchedule[T], Generic[T]):
    id: str

class ChannelScheduler(Generic[T]):
    def __init__(
        self,
        bot: discord.Client,
        *,
        schedules: dict[str, Any],
        save_schedules: Callable[[dict[str, list[Schedule[T]]]], Awaitable[None]],
        logger: Logger | None = None,
    ):
        self.bot = bot
        self.schedules = schedules
        self.save_schedules = save_schedules
        self.logger = logger
        self._ready = asyncio.Event()

        self.tracker = Tracker[int, list[Schedule[T]]](
            load=self._load_store,
            save=self._save_store,
            normalize=self._normalize_schedules,
            validate=self._validate_schedules,
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
        return self.schedules

    async def _save_store(self, schedules: dict[str, list[Schedule[T]]]) -> None:
        self.schedules = schedules
        await self.save_schedules(self.schedules)

    async def _normalize_schedules(
        self,
        channel_id_str: str,
        schedules: Any,
    ) -> tuple[int, list[Schedule[T]]] | None:
        try:
            channel_id = int(channel_id_str)
        except ValueError:
            return None

        if not isinstance(schedules, list):
            return None

        normalized: list[Schedule[T]] = []
        seen_ids: set[str] = set()

        for schedule in schedules:
            if not self._is_new_schedule_like(schedule):
                continue

            schedule: Schedule[T] | NewSchedule[T] = schedule
            schedule_id = schedule.get("id")
            if not isinstance(schedule_id, str) or not schedule_id:
                schedule_id = self._new_schedule_id()

            if schedule_id in seen_ids:
                continue

            seen_ids.add(schedule_id)

            normalized.append({
                "id": schedule_id,
                "payload": schedule["payload"]
            })

        if not normalized:
            return None

        return channel_id, normalized

    async def _validate_schedules(
        self,
        channel_id: int,
        schedules: list[Schedule[T]],
    ) -> bool:
        return bool(schedules) and self._can_send_to(channel_id)

    def _can_send_to(self, channel_id: int) -> bool:
        channel = self.bot.get_channel(channel_id)
        return channel is not None and hasattr(channel, "send")

    def _is_new_schedule_like(self, value: Any) -> bool:
        return (
            isinstance(value, dict)
            and "payload" in value
        )

    def _new_schedule_id(self) -> str:
        return uuid4().hex

    async def add_schedule(
        self,
        channel_id: int,
        schedule: NewSchedule[T],
    ) -> Schedule[T] | None:
        if not self._can_send_to(channel_id):
            return None

        if not self._is_new_schedule_like(schedule):
            return None

        created_schedule: Schedule[T] = {
            **schedule,
            "id": self._new_schedule_id(),
        }

        store = await self.tracker.items()
        schedules = list(store.get(channel_id, []))
        schedules.append(created_schedule)

        await self.tracker.set(channel_id, schedules)
        return created_schedule

    async def remove_schedule(self, channel_id: int, schedule_id: str) -> bool:
        store = await self.tracker.items()
        existing_schedules = store.get(channel_id)

        if existing_schedules is None:
            return False

        schedules = [
            schedule
            for schedule in existing_schedules
            if schedule["id"] != schedule_id
        ]

        if len(schedules) == len(existing_schedules):
            return False

        if schedules:
            await self.tracker.set(channel_id, schedules)
        else:
            await self.tracker.remove(channel_id)

        return True

    async def get_schedule(
        self,
        channel_id: int,
        schedule_id: str,
    ) -> Schedule[T] | None:
        store = await self.tracker.items()

        for schedule in store.get(channel_id, []):
            if schedule["id"] == schedule_id:
                return schedule

        return None

    async def has_schedule(self, channel_id: int, schedule_id: str) -> bool:
        return await self.get_schedule(channel_id, schedule_id) is not None

    async def get_channel_schedules(self, channel_id: int) -> list[Schedule[T]]:
        store = await self.tracker.items()
        return list(store.get(channel_id, []))

    async def get_channels(self) -> dict[int, list[Schedule[T]]]:
        store = await self.tracker.items()
        return {
            channel_id: list(schedules)
            for channel_id, schedules in store.items()
        }

    async def channel_ids(self) -> list[int]:
        store = await self.tracker.items()
        return list(store.keys())

    async def clear_channel(self, channel_id: int) -> bool:
        store = await self.tracker.items()

        if channel_id not in store:
            return False

        await self.tracker.remove(channel_id)
        return True

    async def prune_stale(self) -> None:
        await self.tracker.prune_stale()

    async def close(self) -> None:
        pass
