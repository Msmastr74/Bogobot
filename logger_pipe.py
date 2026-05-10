import contextlib
import logging
import threading
from collections import deque
from typing import IO


class PipeLogger:
    def __init__(
        self,
        pipe: IO[bytes],
        logger: logging.Logger,
        *,
        prefix: str = "",
        level: int = logging.INFO,
        capture_lines: int = 50,
    ):
        self.pipe = pipe
        self.logger = logger
        self.prefix = prefix
        self.level = level
        self._lines: deque[str] = deque(maxlen=capture_lines)
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    def close(self, timeout: float | None = 1) -> None:
        self._thread.join(timeout)

        with contextlib.suppress(OSError, ValueError):
            self.pipe.close()

    def _drain(self) -> None:
        for raw_line in iter(self.pipe.readline, b""):
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue

            self._lines.append(line)
            message = f"{self.prefix}: {line}" if self.prefix else line
            self.logger.log(self.level, message)


def log_subprocess_pipe(
    pipe: IO[bytes] | None,
    logger: logging.Logger,
    *,
    prefix: str = "",
    level: int = logging.INFO,
    capture_lines: int = 50,
) -> PipeLogger | None:
    if pipe is None:
        return None

    return PipeLogger(
        pipe,
        logger,
        prefix=prefix,
        level=level,
        capture_lines=capture_lines,
    )
