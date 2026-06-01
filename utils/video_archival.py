import contextlib
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import queue
import subprocess
import threading
import time

from PIL import Image

from utils.logger_pipe import PipeLogger, log_subprocess_pipe


@dataclass(frozen=True)
class VideoArchiveStatus:
    enabled: bool
    running: bool
    current_path: Path | None
    recorded_frames: int
    dropped_frames: int
    last_frame_at: float | None


@dataclass(frozen=True)
class _QueuedFrame:
    image: Image.Image
    timestamp: float


class VideoArchiver:
    def __init__(
        self,
        *,
        directory: Path,
        width: int = 640,
        height: int = 360,
        fps: float = 1.0,
        crf: int = 36,
        preset: str = "superfast",
        tune: str | None = "animation",
        keyint: int = 10,
        final_format: str = "mkv",
        queue_size: int = 2,
        logger: logging.Logger | None = None,
    ):
        self.directory = directory
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.fps = max(0.1, float(fps))
        self.crf = max(0, int(crf))
        self.preset = preset
        self.tune = tune if tune else None
        self.keyint = max(1, int(keyint))
        self.final_format = self._normalize_final_format(final_format)
        self.logger = logger or logging.getLogger("Bogobot.VideoArchive")

        self._queue: queue.Queue[_QueuedFrame | None] = queue.Queue(maxsize=max(1, queue_size))
        self._lock = threading.Lock()
        self._enabled = False
        self._closed = False
        self._worker: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_logger: PipeLogger | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stdout_file = None
        self._current_path: Path | None = None
        self._current_day: str | None = None
        self._recorded_frames = 0
        self._dropped_frames = 0
        self._last_frame_at: float | None = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Video archive recorder is closed")
            self._enabled = True
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_main,
                    name="BogobotVideoArchive",
                    daemon=True,
                )
                self._worker.start()
        self.finalize_old_recordings()

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
        self._clear_queue()
        self._stop_process()
        self.finalize_old_recordings()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._enabled = False
        self._clear_queue()
        self._offer_sentinel()
        if self._worker is not None:
            self._worker.join(timeout=5)
        self._stop_process()
        self.finalize_old_recordings()

    def record_frame(self, image: Image.Image, timestamp: float | None = None) -> bool:
        with self._lock:
            if not self._enabled or self._closed:
                return False

        timestamp = time.time() if timestamp is None else float(timestamp)
        frame = _QueuedFrame(image.copy(), timestamp)
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            with self._lock:
                self._dropped_frames += 1
            return False

    def status(self) -> VideoArchiveStatus:
        with self._lock:
            return VideoArchiveStatus(
                enabled=self._enabled,
                running=self._process is not None and self._process.poll() is None,
                current_path=self._current_path,
                recorded_frames=self._recorded_frames,
                dropped_frames=self._dropped_frames,
                last_frame_at=self._last_frame_at,
            )

    def video_path_for_timestamp(self, timestamp: float) -> Path:
        day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        final_path = self.directory / f"{day}.{self.final_format}"
        if final_path.exists():
            return final_path
        return self.directory / f"{day}.ts"

    def finalize_old_recordings(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        current_path = self._current_path
        for path in sorted(self.directory.glob("*.ts")):
            if path == current_path:
                continue
            if path.stem == today:
                continue
            self.finalize_recording(path)

    def finalize_recording(self, ts_path: Path) -> Path | None:
        if self.final_format == "ts" or ts_path.suffix != ".ts" or not ts_path.exists():
            return ts_path if ts_path.exists() else None

        final_path = ts_path.with_suffix(f".{self.final_format}")
        tmp_path = ts_path.with_name(f"{ts_path.stem}.tmp.{self.final_format}")
        start_timestamp = self._read_start_timestamp(ts_path.stem)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(ts_path),
            "-map", "0",
            "-c", "copy",
        ]
        if start_timestamp is not None:
            command.extend(["-metadata", f"bogobot_start_timestamp={start_timestamp:.6f}"])
        command.append(str(tmp_path))
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self.logger.warning(f"Could not finalize video archive {ts_path}: {stderr}")
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            return None

        with contextlib.suppress(OSError):
            final_path.unlink()
        tmp_path.replace(final_path)
        ts_path.unlink()
        with contextlib.suppress(OSError):
            self._start_timestamp_path(ts_path.stem).unlink()
        self.logger.info(f"Finalized video archive {final_path}")
        return final_path

    def extract_frame(
        self,
        timestamp: float,
        output_path: Path,
        *,
        quality: int = 2,
    ) -> bool:
        video_path = self.video_path_for_timestamp(timestamp)
        if not video_path.exists():
            return False

        day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        start_timestamp = self._read_start_timestamp(day)
        if start_timestamp is None:
            start_timestamp = self._read_metadata_start_timestamp(video_path)
        if start_timestamp is None:
            start_timestamp = datetime.fromtimestamp(timestamp).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()
        relative_seconds = max(0.0, float(timestamp) - start_timestamp)
        relative_seconds = self._frame_pts_at_or_before(video_path, relative_seconds) or relative_seconds
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if video_path.suffix.lower() == ".ts":
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(video_path),
                "-ss", f"{relative_seconds:.3f}",
                "-frames:v", "1",
                "-q:v", str(max(1, int(quality))),
            ]
        else:
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-ss", f"{relative_seconds:.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", str(max(1, int(quality))),
            ]
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            command.extend(["-pix_fmt", "yuvj420p"])
        command.append(str(output_path))
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self.logger.warning(f"Could not extract video archive frame: {stderr}")
            return False
        return output_path.exists()

    def _frame_pts_at_or_before(self, video_path: Path, relative_seconds: float) -> float | None:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "frame=pts_time",
                "-of", "csv=p=0",
                str(video_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None

        frame_pts: list[float] = []
        for line in result.stdout.decode(errors="replace").splitlines():
            value = line.strip().rstrip(",")
            if not value:
                continue
            try:
                frame_pts.append(float(value))
            except ValueError:
                continue
        if not frame_pts:
            return None

        first_pts = frame_pts[0]
        selected = frame_pts[0]
        for pts in frame_pts:
            normalized_pts = pts - first_pts
            if normalized_pts > relative_seconds:
                break
            selected = pts
        return max(0.0, selected - first_pts)

    def _offer_sentinel(self) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _worker_main(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return

            with self._lock:
                enabled = self._enabled and not self._closed
            if not enabled:
                continue

            try:
                self._write_frame(item)
            except Exception:
                self.logger.exception("Video archive frame write failed")
                self._stop_process()

    def _write_frame(self, frame: _QueuedFrame) -> None:
        day = datetime.fromtimestamp(frame.timestamp).strftime("%Y-%m-%d")
        if self._process is None or self._process.poll() is not None or self._current_day != day:
            stopped_path = self._stop_process()
            if stopped_path is not None and stopped_path.stem != day:
                self.finalize_recording(stopped_path)
            self.finalize_old_recordings()
            self._start_process(day)

        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Video archive ffmpeg process is not writable")

        self._ensure_start_timestamp(day, frame.timestamp)
        image = frame.image
        if image.mode != "RGB":
            image = image.convert("RGB")
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height), Image.Resampling.BILINEAR)

        process.stdin.write(image.tobytes())
        process.stdin.flush()
        with self._lock:
            self._recorded_frames += 1
            self._last_frame_at = frame.timestamp

    def _start_process(self, day: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        output_path = self.directory / f"{day}.ts"
        output_file = output_path.open("ab")

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{self.width}x{self.height}",
            "-i", "pipe:0",
            "-an",
            "-vf", "setpts=PTS-STARTPTS",
            "-vsync", "vfr",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx265",
            "-preset", self.preset,
            "-crf", str(self.crf),
        ]
        if self.tune is not None:
            ffmpeg_cmd.extend(["-tune", self.tune])
        ffmpeg_cmd.extend([
            "-x265-params", f"keyint={self.keyint}:min-keyint={self.keyint}",
            "-f", "mpegts",
            "pipe:1",
        ])

        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception:
            output_file.close()
            raise

        self._process = process
        self._stdout_file = output_file
        self._current_day = day
        self._current_path = output_path
        self._stderr_logger = log_subprocess_pipe(
            process.stderr,
            self.logger,
            prefix="ffmpeg",
            level=logging.WARNING,
        )
        self._stdout_thread = threading.Thread(
            target=self._copy_stdout,
            args=(process,),
            name="BogobotVideoArchiveStdout",
            daemon=True,
        )
        self._stdout_thread.start()
        self.logger.info(f"Video archive recording to {output_path}")

    def _copy_stdout(self, process: subprocess.Popen[bytes]) -> None:
        stdout = process.stdout
        output_file = self._stdout_file
        if stdout is None or output_file is None:
            return

        try:
            for chunk in iter(lambda: stdout.read(64 * 1024), b""):
                if not chunk:
                    break
                output_file.write(chunk)
                output_file.flush()
        except Exception:
            self.logger.exception("Video archive output writer failed")

    def _stop_process(self) -> Path | None:
        process = self._process
        stopped_path = self._current_path
        self._process = None
        self._current_day = None
        self._current_path = None

        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(OSError, BrokenPipeError):
                    process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)

        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=2)
            self._stdout_thread = None

        if self._stderr_logger is not None:
            self._stderr_logger.close()
            self._stderr_logger = None

        if self._stdout_file is not None:
            with contextlib.suppress(OSError):
                self._stdout_file.close()
            self._stdout_file = None

        return stopped_path

    def _normalize_final_format(self, final_format: str) -> str:
        normalized = final_format.lower().lstrip(".")
        if normalized in {"mkv", "matroska"}:
            return "mkv"
        if normalized in {"mp4", "m4v"}:
            return "mp4"
        if normalized in {"ts", "mpegts"}:
            return "ts"
        raise ValueError(f"Unsupported video archive final format: {final_format}")

    def _start_timestamp_path(self, day: str) -> Path:
        return self.directory / f"{day}.start"

    def _read_start_timestamp(self, day: str) -> float | None:
        path = self._start_timestamp_path(day)
        try:
            return float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _ensure_start_timestamp(self, day: str, timestamp: float) -> None:
        path = self._start_timestamp_path(day)
        if path.exists():
            return
        path.write_text(f"{timestamp:.6f}\n", encoding="utf-8")

    def _read_metadata_start_timestamp(self, video_path: Path) -> float | None:
        if not video_path.exists():
            return None

        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format_tags=bogobot_start_timestamp",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None

        try:
            return float(result.stdout.decode(errors="replace").strip())
        except ValueError:
            return None
