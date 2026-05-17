import asyncio
import inspect
from typing import Any, Awaitable, Callable, TypeAlias

from utils.type import P

class CallbackRegistry:
    def __init__(self):
        self._callbacks: dict[str, list[Callable]] = {}
    
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
                print(f"Error in {event} callback {callback}: {e}")
    
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
                print(f"Error in {event} callback {callback}: {e}")
        results = await asyncio.gather(*coros, return_exceptions=True)
        for result, callback in zip(results, coro_info):
            if isinstance(result, Exception):
                print(f"Error in {event} callback {callback}: {result}")

AsyncCallback: TypeAlias = Callable[P, Awaitable[None] | None]
SyncCallback: TypeAlias = Callable[P, None]
