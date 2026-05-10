import asyncio
from collections.abc import Awaitable, Callable, Hashable, Mapping
from typing import Any, Generic, TypeVar


K = TypeVar("K", bound=Hashable)
T = TypeVar("T")
RawItems = Mapping[str, Any]


class Tracker(Generic[K, T]):
    def __init__(
        self,
        *,
        load: Callable[[], Awaitable[RawItems]],
        save: Callable[[dict[str, T]], Awaitable[None]],
        normalize: Callable[[str, Any], Awaitable[tuple[K, T] | None]],
        validate: Callable[[K, T], Awaitable[bool]] | None = None,
    ):
        self._load = load
        self._save = save
        self._normalize = normalize
        self._validate = validate
        self._items: dict[K, T] | None = None
        self._save_lock = asyncio.Lock()
        self._saving = False
        self._pending_save: dict[str, T] | None = None
        self._pending_waiters: list[asyncio.Future[None]] = []

    async def load(self) -> dict[K, T]:
        raw_items = await self._load()
        normalized: dict[K, T] = {}
        for key, value in raw_items.items():
            item = await self._normalize(key, value)
            if item is None:
                continue
            normalized_key, normalized_value = item
            normalized[normalized_key] = normalized_value
        if dict(raw_items) != self._raw_items(normalized):
            await self._save_items(normalized)
        self._items = normalized
        return dict(normalized)

    async def items(self) -> dict[K, T]:
        if self._items is None:
            return await self.load()
        return dict(self._items)

    async def get(self, key: K) -> T | None:
        items = await self.items()
        return items.get(key)

    async def set(self, key: K, value: Any) -> bool:
        item = await self._normalize(str(key), value)
        if item is None:
            return False
        normalized_key, normalized_value = item
        items = await self.items()
        items[normalized_key] = normalized_value
        await self._save_items(items)
        return True

    async def remove(self, key: K) -> bool:
        items = await self.items()
        if key not in items:
            return False
        items.pop(key, None)
        await self._save_items(items)
        return True

    async def remove_many(self, keys: list[K]) -> bool:
        items = await self.items()
        changed = False
        for key in keys:
            if key in items:
                items.pop(key, None)
                changed = True
        if changed:
            await self._save_items(items)
        return changed

    async def prune_stale(self) -> list[K]:
        items = await self.items()
        if self._validate is None:
            return []
        stale_keys: list[K] = []
        for key, value in items.items():
            if not await self._validate(key, value):
                stale_keys.append(key)
        await self.remove_many(stale_keys)
        return stale_keys

    async def _save_items(self, items: dict[K, T]) -> None:
        self._items = dict(items)
        raw_items = self._raw_items(items)
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        should_save = False
        async with self._save_lock:
            if self._saving:
                self._pending_save = raw_items
                self._pending_waiters.append(waiter)
            else:
                self._saving = True
                should_save = True
        if should_save:
            await self._drain_save_queue(raw_items, [waiter])
        await waiter

    async def _drain_save_queue(
        self,
        raw_items: dict[str, T],
        waiters: list[asyncio.Future[None]],
    ) -> None:
        while True:
            try:
                await self._save(raw_items)
            except Exception as exc:
                self._finish_waiters(waiters, exc)
                async with self._save_lock:
                    pending_waiters = self._pending_waiters
                    self._pending_save = None
                    self._pending_waiters = []
                    self._saving = False
                self._finish_waiters(pending_waiters, exc)
                raise
            self._finish_waiters(waiters)
            async with self._save_lock:
                if self._pending_save is None:
                    self._saving = False
                    return
                raw_items = self._pending_save
                waiters = self._pending_waiters
                self._pending_save = None
                self._pending_waiters = []

    def _finish_waiters(
        self,
        waiters: list[asyncio.Future[None]],
        exc: Exception | None = None,
    ) -> None:
        for waiter in waiters:
            if waiter.done():
                continue
            if exc is None:
                waiter.set_result(None)
            else:
                waiter.set_exception(exc)

    def _raw_items(self, items: dict[K, T]) -> dict[str, T]:
        return {
            str(key): value
            for key, value in items.items()
        }
