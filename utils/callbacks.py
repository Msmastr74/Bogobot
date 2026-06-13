import asyncio
import inspect
from logging import Logger
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar

from utils.type import P

class CallbackRegistry:
    def __init__(self, logger: Logger | None = None):
        self._callbacks: dict[str, list[Callable]] = {}
        def _log_exc(text: str, exc: Exception):
            if logger:
                logger.warning(text, exc_info=True)
            else:
                print(f"{text}: {exc}")
        self._log_exc = _log_exc
    
    def register(self, event: str, callback: Callable):
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)
    
    def execute(self, event: str, *args, **kwargs):
        if event not in self._callbacks:
            return
        for callback in self._callbacks[event]:
            try:
                callback(*args, **kwargs)
            except Exception as e:
                self._log_exc(f"Error in {event} callback {callback}", e)
    
    async def execute_async(self, event: str, *args, **kwargs):
        if event not in self._callbacks:
            return
        coros: list[Awaitable[None]] = []
        coro_info: list[Callable] = []
        for callback in self._callbacks[event]:
            try:
                result = callback(*args, **kwargs)
                if inspect.isawaitable(result):
                    async def coro_exec(coro: Awaitable[Any]) -> None:
                        await coro
                    coros.append(coro_exec(result))
                    coro_info.append(callback)
            except Exception as e:
                self._log_exc(f"Error in {event} callback {callback}", e)
        results = await asyncio.gather(*coros, return_exceptions=True)
        for result, callback in zip(results, coro_info):
            if isinstance(result, Exception):
                self._log_exc(f"Error in {event} callback {callback}", result)

MaybeAwaitableT = TypeVar("MaybeAwaitableT", Awaitable[None], None)
AsyncCallback: TypeAlias = Callable[P, MaybeAwaitableT]
SyncCallback: TypeAlias = Callable[P, None]
