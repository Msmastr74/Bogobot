import io
import asyncio
import inspect
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Coroutine

from PIL import Image

from utils.logger_pipe import PipeLogger, log_subprocess_pipe

FrameCallback = Callable[[Image.Image], None | Coroutine[Any, Any, None]]


class StreamHandler:
    PNG_SIG = b"\x89PNG\r\n\x1a\n"
    IEND = b"IEND"

    def __init__(
        self,
        *,
        url: str,
        quality: str,
        on_new_frame: FrameCallback,
        fps: float,
        quiet: bool = False,
        quick_fail_s: float = 120,
        backoff_min_s: float = 2,
        backoff_max_s: float = 120,
        logger: logging.Logger | None = None,
        loop: asyncio.AbstractEventLoop | None = None
    ):
        self.url = url
        self.quality = quality
        self.on_new_frame = on_new_frame
        self.fps = fps
        self.quiet = quiet
        self.quick_fail_s = quick_fail_s
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s
        self.logger = logger or logging.getLogger(__name__)
        self.async_loop: asyncio.AbstractEventLoop | None = loop
        self._frame_future = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._procs: list[subprocess.Popen[bytes]] = []
        self._stderr_loggers: list[PipeLogger] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 10) -> None:
        self._stop.set()
        self._kill_procs()
        if self._thread:
            self._thread.join(timeout)

    def _callback_is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.on_new_frame)

    def _loop(self) -> None:
        backoff = self.backoff_min_s
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._run_once()
            except Exception as e:
                self.logger.warning(f"Stream failed with error: {e}")
            lifetime = time.monotonic() - t0
            if lifetime < self.quick_fail_s:
                self._sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max_s)
            else:
                backoff = self.backoff_min_s
            self._kill_procs()

    def _run_once(self) -> None:
        streamlink = subprocess.Popen(
            ["streamlink", self.url, self.quality, "--stdout", "--loglevel", "error" if self.quiet else "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if streamlink_logger := log_subprocess_pipe(
            streamlink.stderr,
            self.logger,
            prefix="streamlink",
        ):
            self._stderr_loggers.append(streamlink_logger)
        if streamlink.stdout is None:
            raise RuntimeError("streamlink stdout was not piped")
        ffmpeg = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error" if self.quiet else "info",
                "-re",
                "-i", "pipe:0",
                "-vf", f"fps={self.fps}",
                "-f", "image2pipe",
                "-vcodec", "png",
                "pipe:1",
            ],
            stdin=streamlink.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if ffmpeg_logger := log_subprocess_pipe(
            ffmpeg.stderr,
            self.logger,
            prefix="ffmpeg",
        ):
            self._stderr_loggers.append(ffmpeg_logger)
        self._procs = [streamlink, ffmpeg]
        streamlink.stdout.close()
        if ffmpeg.stdout is None:
            raise RuntimeError("ffmpeg stdout was not piped")
        buf = bytearray()
        while not self._stop.is_set():
            chunk = os.read(ffmpeg.stdout.fileno(), 8192)
            if not chunk:
                break
            buf.extend(chunk)
            for png in self._pop_pngs(buf):
                img = Image.open(io.BytesIO(png))
                img.load()
                self._emit_frame(img)

    def _emit_frame(self, img: Image.Image) -> None:
        if self._callback_is_async():
            loop = self.async_loop
            if loop is None:
                raise RuntimeError("Async frame callback was used, but no async loop exists")
            if self._frame_future and not self._frame_future.done():
                self.logger.warning("Previous frame is still being processed, dropping frame")
                return
            async def runner() -> None:
                await self.on_new_frame(img)  # type: ignore[misc]
            self._frame_future = asyncio.run_coroutine_threadsafe(runner(), loop)
            self._frame_future.add_done_callback(self._log_frame_callback_error)
        else:
            self.on_new_frame(img)

    def _log_frame_callback_error(self, future: Future):
        if future.cancelled():
            return
        exception = future.exception()
        if exception is None:
            return
        self.logger.critical(
            "Stream frame callback failed",
            exc_info=(type(exception), exception, exception.__traceback__),
        )

    def _pop_pngs(self, buf: bytearray) -> list[bytes]:
        out = []
        while True:
            start = buf.find(self.PNG_SIG)
            if start < 0:
                buf.clear()
                return out
            del buf[:start]
            pos = len(self.PNG_SIG)
            while True:
                if len(buf) < pos + 8:
                    return out
                n = int.from_bytes(buf[pos:pos + 4], "big")
                typ = bytes(buf[pos + 4:pos + 8])
                pos += 12 + n
                if len(buf) < pos:
                    return out
                if typ == self.IEND:
                    out.append(bytes(buf[:pos]))
                    del buf[:pos]
                    break

    def _kill_procs(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    p.kill()
        self._procs = []
        for pipe_logger in self._stderr_loggers:
            pipe_logger.close()
        self._stderr_loggers = []

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))
