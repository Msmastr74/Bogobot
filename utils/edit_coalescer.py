import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING
import time

import discord

if TYPE_CHECKING:
    from logging import Logger

# Maximum number of seconds to wait based on message send duration
# This solution adapts to the hidden rate limiting handling behind the scenes of discord.py
ADAPTIVE_MAX_DELAY_SECONDS = 10

@dataclass(slots=True)
class PendingEdit:
    kwargs: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future[discord.Message | None] | None = None


class MessageEditCoalescer:
    def __init__(
        self,
        message: discord.PartialMessage,
        *,
        logger: "Logger | None" = None
    ):
        self.message = message
        self.logger = logger
        self._pending: PendingEdit | None = None
        self._changed = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False
        self.NotFound_or_Forbidden = False

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    async def close(self) -> None:
        self._closed = True
        self._changed.set()
        if self._pending is not None and self._pending.future is not None:
            if not self._pending.future.done():
                self._pending.future.set_result(None)
        self._pending = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

    async def edit(
        self,
        *,
        wait: bool = False,
        **kwargs: Any,
    ) -> discord.Message | None:
        if self._closed:
            raise RuntimeError(f"Edit coalescer for message {self.message.id} is closed")
        self.start()
        old_pending = self._pending
        if old_pending is not None and old_pending.future is not None:
            if not old_pending.future.done():
                old_pending.future.set_result(None)
        future = asyncio.get_running_loop().create_future() if wait else None
        self._pending = PendingEdit(kwargs=kwargs, future=future)
        self._changed.set()
        if future is not None:
            return await future
        return None

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closed:
            await self._changed.wait()
            self._changed.clear()
            while self._pending is not None and not self._closed:
                pending = self._pending
                self._pending = None
                edit_start_time = loop.time()
                try:
                    result = await self.message.edit(**pending.kwargs)
                except (discord.NotFound, discord.Forbidden):
                    self.NotFound_or_Forbidden = True
                    result = None
                except Exception as exc:
                    if pending.future is not None and not pending.future.done():
                        pending.future.set_exception(exc)
                    elif self.logger is not None:
                        self.logger.warning(
                            f"Coalesced edit failed for message {self.message.id}",
                            exc_info=exc,
                        )
                    continue
                edit_duration = loop.time() - edit_start_time

                if pending.future is not None and not pending.future.done():
                    pending.future.set_result(result)
                
                await asyncio.sleep(min(edit_duration, ADAPTIVE_MAX_DELAY_SECONDS))


class EditCoalescer:
    def __init__(
        self,
        *,
        logger: "Logger | None" = None,
    ):
        self.logger = logger
        self._coalescers: dict[int, MessageEditCoalescer] = {}

    def register(
        self,
        message: discord.Message | discord.PartialMessage
    ) -> MessageEditCoalescer:
        coalescer = self._coalescers.get(message.id)
        if coalescer is not None:
            return coalescer
        coalescer = MessageEditCoalescer(
            message,
            logger=self.logger
        )
        self._coalescers[message.id] = coalescer
        return coalescer

    def get(self, message_id: int) -> MessageEditCoalescer | None:
        return self._coalescers.get(message_id)

    async def edit(
        self,
        message_id: int,
        *,
        wait: bool = False,
        **kwargs: Any,
    ) -> discord.Message | None:
        coalescer = self.get(message_id)
        if coalescer is None:
            return None
        return await coalescer.edit(wait=wait, **kwargs)

    async def delete(
        self,
        message_id: int,
    ) -> bool:
        coalescer = self._coalescers.pop(message_id, None)
        if coalescer is None:
            return False
        try:
            await coalescer.message.delete()
        except (discord.NotFound, discord.Forbidden):
            return False
        finally:
            await coalescer.close()

        return True

    async def close(self) -> None:
        coalescers = list(self._coalescers.values())
        self._coalescers.clear()

        for coalescer in coalescers:
            await coalescer.close()
