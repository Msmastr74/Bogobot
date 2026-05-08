import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, cast, TYPE_CHECKING
if TYPE_CHECKING:
    from logging import Logger

import discord


OperationKind = Literal["send", "edit", "delete"]


@dataclass(slots=True)
class QueuedOperation:
    kind: OperationKind
    message_id: int | None = None

    content: str | None = None
    embed: discord.Embed | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)

    future: asyncio.Future[Any] | None = None
    cancelled: bool = False


class ChannelProxy:
    """
    One proxy per Discord channel.

    This keeps operations for a channel serialized, which matches Discord's
    route-specific rate limiting much better than a global message queue.

    Also coalesces queued edits:
    - edit message 123 to A
    - edit message 123 to B
    - edit message 123 to C

    Only C is sent to Discord if A/B have not been processed yet.
    """

    def __init__(
        self,
        channel: 'discord.abc.PartialMessageableChannel',
        *,
        logger: Any | None = None,
        min_interval: float = 0.15,
    ):
        self.channel = channel
        self.channel_id = channel.id  # pyright: ignore[reportAttributeAccessIssue]
        self.logger = logger
        self.min_interval = min_interval

        self._queue: asyncio.Queue[QueuedOperation] = asyncio.Queue()
        self._pending_edits: dict[int, QueuedOperation] = {}
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    async def close(self) -> None:
        self._closed = True

        if self._worker_task is not None:
            self._worker_task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task

    async def send(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        wait: bool = True,
        **kwargs: Any,
    ) -> discord.Message | None:
        op = QueuedOperation(
            kind="send",
            content=content,
            embed=embed,
            kwargs=kwargs,
        )

        return await self._enqueue(op, wait=wait)

    async def edit(
        self,
        message_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        wait: bool = False,
        **kwargs: Any,
    ) -> discord.Message | None:
        old_op = self._pending_edits.get(message_id)

        if old_op is not None:
            old_op.cancelled = True

            if old_op.future is not None and not old_op.future.done():
                old_op.future.set_result(None)

        op = QueuedOperation(
            kind="edit",
            message_id=message_id,
            content=content,
            embed=embed,
            kwargs=kwargs,
        )

        self._pending_edits[message_id] = op

        return await self._enqueue(op, wait=wait)

    async def delete(
        self,
        message_id: int,
        *,
        wait: bool = False,
    ) -> None:
        old_edit = self._pending_edits.pop(message_id, None)

        if old_edit is not None:
            old_edit.cancelled = True

            if old_edit.future is not None and not old_edit.future.done():
                old_edit.future.set_result(None)

        op = QueuedOperation(
            kind="delete",
            message_id=message_id,
        )

        await self._enqueue(op, wait=wait)

    async def _enqueue(self, op: QueuedOperation, *, wait: bool) -> Any:
        if self._closed:
            raise RuntimeError(f"ChannelProxy for {self.channel_id} is closed")

        self.start()

        if wait:
            op.future = asyncio.get_running_loop().create_future()

        await self._queue.put(op)

        if wait and op.future is not None:
            return await op.future

        return None

    async def _worker(self) -> None:
        while not self._closed:
            op = await self._queue.get()

            try:
                if op.cancelled:
                    continue

                result = await self._run_operation(op)

                if op.future is not None and not op.future.done():
                    op.future.set_result(result)

            except discord.NotFound:
                if op.future is not None and not op.future.done():
                    op.future.set_result(None)

            except discord.Forbidden:
                if op.future is not None and not op.future.done():
                    op.future.set_result(None)

                if self.logger is not None:
                    self.logger.warning(
                        f"Missing permissions for channel proxy {self.channel_id}"
                    )

            except Exception as exc:
                if op.future is not None and not op.future.done():
                    op.future.set_exception(exc)
                elif self.logger is not None:
                    self.logger.warning(
                        f"ChannelProxy operation failed for {self.channel_id}: {op.kind}",
                        exc_info=exc,
                    )

            finally:
                if op.kind == "edit" and op.message_id is not None:
                    if self._pending_edits.get(op.message_id) is op:
                        self._pending_edits.pop(op.message_id, None)

                self._queue.task_done()

                if self.min_interval > 0:
                    await asyncio.sleep(self.min_interval)

    async def _run_operation(self, op: QueuedOperation) -> Any:
        if op.kind == "send":
            if not hasattr(self.channel, "send"):
                raise TypeError(f"Channel {self.channel_id} does not support send()")
            
            if op.embed is not None:
                return await self.channel.send(
                    content=op.content,
                    embed=op.embed,
                    **op.kwargs
                )
            return await self.channel.send(
                content=op.content,
                **op.kwargs
            )

        if op.message_id is None:
            raise ValueError(f"{op.kind} requires message_id")

        if not hasattr(self.channel, "get_partial_message"):
            raise TypeError(
                f"Channel {self.channel_id} does not support partial messages"
            )

        message = self.channel.get_partial_message(op.message_id)

        if op.kind == "edit":
            return await message.edit(
                content=op.content,
                embed=op.embed,
                **op.kwargs,
            )

        if op.kind == "delete":
            await message.delete()
            return None

        raise ValueError(f"Unknown operation kind: {op.kind}")

class ChannelProxyManager:
    """
    Owns all ChannelProxy instances.

    Stored config shape:

        channels: {
            "123456789": ["monitor", "alerts"],
            "987654321": ["monitor"]
        }

    One ChannelProxy exists per channel ID, regardless of how many usage types
    reference that channel.
    """

    def __init__(
        self,
        bot: discord.Client,
        *,
        channel_data: dict[str, Any],
        save_channels: Callable[[dict[str, Any]], None],
        logger: 'Logger | None' = None,
    ):
        self.bot = bot
        self.channel_data = channel_data
        self.save_channels = save_channels
        self.logger = logger

        self.proxies: dict[int, ChannelProxy] = {}
        self._wait_event = asyncio.Event()

    def _get_channel_store(self) -> dict[str, list[str]]:
        channels = self.channel_data

        normalized: dict[str, list[str]] = {}

        for channel_id_str, usages in channels.items():
            if not isinstance(channel_id_str, str):
                continue

            if isinstance(usages, list):
                normalized[channel_id_str] = [
                    usage for usage in usages if isinstance(usage, str)
                ]
            elif isinstance(usages, str):
                normalized[channel_id_str] = [usages]

        if normalized != channels:
            self.channel_data = normalized
            self.save_channels(self.channel_data)

        return normalized

    async def wait_until_ready(self) -> None:
        await self._wait_event.wait()

    async def initialize_channels(self) -> None:
        """
        Reads channel config and creates ChannelProxy instances for available
        channels.

        Unavailable channels are removed from tracking.
        """

        store = self._get_channel_store()
        stale_channel_ids: list[str] = []

        for channel_id_str, usages in list(store.items()):
            try:
                channel_id = int(channel_id_str)
            except ValueError:
                stale_channel_ids.append(channel_id_str)
                continue

            if not usages:
                stale_channel_ids.append(channel_id_str)
                continue

            channel = self.bot.get_channel(channel_id)

            if channel is None:
                stale_channel_ids.append(channel_id_str)
                continue

            if not hasattr(channel, "send"):
                stale_channel_ids.append(channel_id_str)
                continue
            
            channel = cast('discord.abc.PartialMessageableChannel', channel)

            proxy = ChannelProxy(
                channel,
                logger=self.logger,
            )
            proxy.start()

            self.proxies[channel_id] = proxy

        if stale_channel_ids:
            for channel_id_str in stale_channel_ids:
                store.pop(channel_id_str, None)

            self.channel_data = store
            self.save_channels(self.channel_data)
        
        self._wait_event.set()

    async def add_channel(self, usage_type: str, channel_id: int) -> ChannelProxy | None:
        """
        Adds a usage type to a channel.

        Returns None if the channel is not currently available in cache.
        """

        channel = self.bot.get_channel(channel_id)

        if channel is None:
            return None

        if not hasattr(channel, "send"):
            return None
        
        channel = cast('discord.abc.PartialMessageableChannel', channel)

        store = self._get_channel_store()
        channel_id_str = str(channel_id)

        usages = store.setdefault(channel_id_str, [])

        if usage_type not in usages:
            usages.append(usage_type)
            usages.sort()

            self.channel_data = store
            self.save_channels(self.channel_data)

        proxy = self.proxies.get(channel_id)

        if proxy is None:
            proxy = ChannelProxy(
                channel,
                logger=self.logger,
            )
            proxy.start()

            self.proxies[channel_id] = proxy

        return proxy

    async def remove_channel(self, usage_type: str, channel_id: int) -> bool:
        """
        Removes a usage type from a channel.

        If the channel has no more usages, the ChannelProxy is closed and removed.

        Returns True if something was removed.
        """

        store = self._get_channel_store()
        channel_id_str = str(channel_id)

        usages = store.get(channel_id_str)

        if usages is None:
            return False

        if usage_type not in usages:
            return False

        usages.remove(usage_type)

        if usages:
            store[channel_id_str] = usages
        else:
            store.pop(channel_id_str, None)

            proxy = self.proxies.pop(channel_id, None)
            if proxy is not None:
                await proxy.close()

        self.channel_data = store
        self.save_channels(self.channel_data)

        return True

    def get(self, channel_id: int) -> ChannelProxy | None:
        return self.proxies.get(channel_id)

    def has_usage(self, usage_type: str, channel_id: int) -> bool:
        store = self._get_channel_store()
        return usage_type in store.get(str(channel_id), [])

    def get_channels_for_usage(self, usage_type: str) -> list[int]:
        store = self._get_channel_store()

        channel_ids: list[int] = []

        for channel_id_str, usages in store.items():
            if usage_type not in usages:
                continue

            try:
                channel_ids.append(int(channel_id_str))
            except ValueError:
                continue

        return channel_ids

    async def close(self) -> None:
        for proxy in list(self.proxies.values()):
            await proxy.close()

        self.proxies.clear()
