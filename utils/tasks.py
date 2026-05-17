import asyncio
import datetime as dt
import inspect
import logging
from typing import Any, Awaitable, Callable, Optional

_log = logging.getLogger(__name__)

class RelativeLoop:
    def __init__(
        self,
        coro: Callable[..., Awaitable[Any]],
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        count: Optional[int] = None,
    ) -> None:
        if not inspect.iscoroutinefunction(coro):
            raise TypeError("Expected an async function")

        interval = seconds + minutes * 60 + hours * 3600
        if interval <= 0:
            raise ValueError("Loop interval must be greater than 0 seconds")

        self.coro = coro
        self.seconds = interval
        self.count = count

        self._task: Optional[asyncio.Task[None]] = None
        self._stop_requested = False
        self._current_loop = 0
        self._last_iteration: Optional[dt.datetime] = None
        self._next_iteration: Optional[dt.datetime] = None

    def start(self, *args: Any, **kwargs: Any) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("Loop is already running")

        self._stop_requested = False
        self._current_loop = 0
        self._task = asyncio.create_task(self._run(*args, **kwargs))
        return self._task

    def stop(self) -> None:
        """
        Gracefully stop after the current iteration finishes.

        If the loop is currently sleeping, it will still wake naturally unless
        you call cancel().
        """
        self._stop_requested = True

    def cancel(self) -> None:
        """
        Immediately cancel the loop task.
        """
        self._stop_requested = True

        if self._task is not None and not self._task.done():
            self._task.cancel()

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_task(self) -> Optional[asyncio.Task[None]]:
        return self._task

    @property
    def current_loop(self) -> int:
        return self._current_loop

    @property
    def last_iteration(self) -> Optional[dt.datetime]:
        return self._last_iteration

    @property
    def next_iteration(self) -> Optional[dt.datetime]:
        return self._next_iteration

    async def _run(self, *args: Any, **kwargs: Any) -> None:
        try:
            while not self._stop_requested:
                started_at = dt.datetime.now(dt.timezone.utc)
                self._last_iteration = started_at

                await self.coro(*args, **kwargs)

                self._current_loop += 1

                if self.count is not None and self._current_loop >= self.count:
                    break

                if self._stop_requested:
                    break

                # This is the key behavior:
                #
                # The target is based on the timestamp from immediately before
                # this execution, not on a previously incremented scheduled time.
                target_time = started_at + dt.timedelta(seconds=self.seconds)
                self._next_iteration = target_time

                now = dt.datetime.now(dt.timezone.utc)
                sleep_for = (target_time - now).total_seconds()

                if sleep_for < 0:
                    _log.warning(
                        f"Task exceeded interval {self.seconds}s by {abs(sleep_for)}s. Executing next iteration immediately."
                    )
                await asyncio.sleep(max(sleep_for, 0))
                # If sleep_for <= 0, the loop catches up immediately.
                # The next iteration will take a fresh started_at timestamp.

        except asyncio.CancelledError:
            raise
        finally:
            self._next_iteration = None


def loop(
    *,
    seconds: float = 0,
    minutes: float = 0,
    hours: float = 0,
    count: Optional[int] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], RelativeLoop]:
    def decorator(coro: Callable[..., Awaitable[Any]]) -> RelativeLoop:
        return RelativeLoop(
            coro,
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            count=count,
        )

    return decorator
