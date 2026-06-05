import contextlib
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import heapq
import math
import logging
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from PIL import Image
from cv2.typing import MatLike

from utils.logger_pipe import PipeLogger, log_subprocess_pipe


SHOWINFO_RE = re.compile(r"n:\s*(?P<index>\d+)\s+pts:\s*\S+\s+pts_time:(?P<time>-?\d+(?:\.\d+)?)")
SCAN_THREADS = 3
SCAN_BATCH_SIZE = 64
SCAN_RESULT_LIMIT = 8


@dataclass(frozen=True)
class VideoArchiveStatus:
    enabled: bool
    running: bool
    current_path: Path | None
    recorded_frames: int
    dropped_frames: int
    last_frame_at: float | None


@dataclass(frozen=True)
class VideoScanMatch:
    timestamp: float
    score: float
    relative_seconds: float
    frame: bytes | None


@dataclass(frozen=True)
class VideoScanResult:
    scanned_frames: int
    matches: tuple[VideoScanMatch, ...]

    @property
    def timestamp(self) -> float:
        return self.matches[0].timestamp

    @property
    def score(self) -> float:
        return self.matches[0].score

    @property
    def relative_seconds(self) -> float:
        return self.matches[0].relative_seconds

    @property
    def frame(self) -> bytes | None:
        return self.matches[0].frame


@dataclass
class ScanProgress:
    stage: str
    started_at: float
    stage_started_at: float
    window_start_seconds: float
    window_end_seconds: float
    current_seconds: float | None = None
    scanned_frames: int = 0
    best_score: float | None = None
    done: bool = False
    cancel_requested: bool = False
    cancelled: bool = False
    completed_at: float | None = None
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    @property
    def stage_elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.stage_started_at)

    @property
    def progress_ratio(self) -> float | None:
        if self.done and not self.cancelled:
            return 1.0
        if self.current_seconds is None:
            return None
        span = self.window_end_seconds - self.window_start_seconds
        if span <= 0:
            return None
        return max(0.0, min(1.0, (self.current_seconds - self.window_start_seconds) / span))

    @property
    def estimated_remaining_seconds(self) -> float | None:
        ratio = self.progress_ratio
        if ratio is None or ratio <= 0 or self.done:
            return 0.0 if self.done else None
        current_seconds = self.current_seconds
        if current_seconds is None:
            return None
        current_units = current_seconds - self.window_start_seconds
        total_units = self.window_end_seconds - self.window_start_seconds
        if current_units <= 0 or total_units <= 0:
            return None
        estimated_total = self.stage_elapsed_seconds / current_units * total_units
        return max(0.0, estimated_total - self.stage_elapsed_seconds)

    def set_stage(
        self,
        stage: str,
        *,
        window_start_seconds: float | None = None,
        window_end_seconds: float | None = None,
    ) -> None:
        self.stage = stage
        self.stage_started_at = time.monotonic()
        self.current_seconds = None
        self.scanned_frames = 0
        self.best_score = None
        self.done = False
        self.completed_at = None
        if window_start_seconds is not None:
            self.window_start_seconds = window_start_seconds
        if window_end_seconds is not None:
            self.window_end_seconds = window_end_seconds

    def request_cancel(self) -> None:
        self.cancel_requested = True
        process = self._process
        self.stage = "Cancelling scan"
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()

    def is_cancel_requested(self) -> bool:
        return self.cancel_requested

    def set_process(self, process: subprocess.Popen[bytes] | None) -> None:
        self._process = process
        should_cancel = self.cancel_requested and process is not None
        if should_cancel and process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()

    def mark_cancelled(self) -> None:
        self.stage = "Cancelled"
        self.done = True
        self.cancelled = True
        self.completed_at = time.time()
        self.set_process(None)


@dataclass(frozen=True)
class _ScanCandidate:
    x: int
    y: int
    width: int
    height: int
    sample_step: int
    template: MatLike
    locator_score: float


@dataclass(frozen=True)
class _ScanRangeResult:
    score: float
    relative_seconds: float
    scanned_frames: int
    frame: bytes | None
    matches: tuple["_ScanRangeMatch", ...]


@dataclass(frozen=True)
class _ScanRangeMatch:
    score: float
    frame_index: int
    relative_seconds: float
    frame: bytes | None


class _TopScanMatches:
    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._next_index = 0
        self._heap: list[tuple[float, int, _ScanRangeMatch]] = []

    def insert(self, match: _ScanRangeMatch) -> None:
        item = (match.score, self._next_index, match)
        self._next_index += 1
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, item)
        elif match.score > self._heap[0][0]:
            heapq.heappushpop(self._heap, item)

    def get_top_n(self) -> tuple[_ScanRangeMatch, ...]:
        return tuple(
            item[2]
            for item in sorted(self._heap, key=lambda item: item[0], reverse=True)
        )


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
        crf: int = 22,
        preset: str = "fast",
        tune: str | None = "animation",
        keyint: int = 60,
        final_format: str = "mkv",
        queue_size: int = 2,
        logger: logging.Logger | None = None,
    ):
        self.directory = directory
        self.width = max(1, int(width))
        self.height = max(1, int(height))
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

    def recorded_bounds_for_day(self, day: str) -> tuple[float, float] | None:
        video_path = self.video_path_for_day(day)
        if not video_path.exists():
            return None
        start_timestamp = self._read_start_timestamp(day)
        if start_timestamp is None:
            start_timestamp = self._read_metadata_start_timestamp(video_path)
        if start_timestamp is None:
            return None
        duration = self._video_duration(video_path)
        if duration is None:
            return None
        return start_timestamp, start_timestamp + duration

    def video_path_for_timestamp(self, timestamp: float) -> Path:
        day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        return self.video_path_for_day(day)

    def video_path_for_day(self, day: str) -> Path:
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

        self.repair_recording_timestamps(ts_path)
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
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.suppress(OSError):
            output_path.unlink()
        command = self._extract_frame_command(
            video_path,
            output_path,
            relative_seconds,
            quality,
        )
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0 and output_path.exists():
            return True

        stderr = result.stderr.decode(errors="replace").strip()
        if stderr:
            self.logger.warning(f"Could not extract video archive frame: {stderr}")
        else:
            self.logger.warning("Could not extract video archive frame: no frame was written")
        return False

    def scan_for_image(
        self,
        day: str,
        image: Image.Image,
        *,
        locator_interval_seconds: float = 30.0,
        max_candidates: int = 12,
        requested_start_timestamp: float | None = None,
        requested_end_timestamp: float | None = None,
        progress: ScanProgress | None = None,
    ) -> VideoScanResult | None:
        total_started_at = time.perf_counter()

        video_path = self.video_path_for_day(day)
        if not video_path.exists():
            return None

        archive_start_timestamp = self._read_start_timestamp(day)
        if archive_start_timestamp is None:
            archive_start_timestamp = self._read_metadata_start_timestamp(video_path)
        if archive_start_timestamp is None:
            archive_start_timestamp = datetime.strptime(day, "%Y-%m-%d").timestamp()

        locator_interval_seconds = max(0.25, float(locator_interval_seconds))
        frame_size = self.width * self.height * 3
        templates_started_at = time.perf_counter()
        if progress is not None:
            progress.set_stage("Preparing templates")
        templates = self._scan_templates(image)
        self.logger.debug(
            "Video archive scan templates: %s templates in %.3fs",
            len(templates),
            time.perf_counter() - templates_started_at,
        )
        if not templates:
            return None
        if progress is not None and progress.is_cancel_requested():
            progress.mark_cancelled()
            return None

        duration_started_at = time.perf_counter()
        if progress is not None:
            progress.set_stage("Reading archive duration")
        duration = self._video_duration(video_path)
        self.logger.debug(
            "Video archive scan duration probe: %s in %.3fs",
            f"{duration:.3f}s" if duration is not None else "unavailable",
            time.perf_counter() - duration_started_at,
        )
        if duration is None:
            return None
        if progress is not None and progress.is_cancel_requested():
            progress.mark_cancelled()
            return None
        scan_start_seconds = 0.0 if requested_start_timestamp is None else max(
            0.0,
            float(requested_start_timestamp) - archive_start_timestamp,
        )
        scan_end_seconds = duration if requested_end_timestamp is None else min(
            duration,
            max(0.0, float(requested_end_timestamp) - archive_start_timestamp),
        )
        if scan_end_seconds <= scan_start_seconds:
            return None

        locator_started_at = time.perf_counter()
        if progress is not None:
            progress.set_stage(
                "Locating image",
                window_start_seconds=scan_start_seconds,
                window_end_seconds=scan_end_seconds,
            )
        candidates = self._scan_candidates(
            video_path,
            templates,
            locator_interval_seconds=locator_interval_seconds,
            max_candidates=max_candidates,
            start_seconds=scan_start_seconds,
            duration_seconds=scan_end_seconds - scan_start_seconds,
            progress=progress,
        )
        self.logger.debug(
            "Video archive scan locator: %s candidates in %.3fs over %.3fs..%.3fs",
            len(candidates),
            time.perf_counter() - locator_started_at,
            scan_start_seconds,
            scan_end_seconds,
        )
        if not candidates:
            if progress is not None and progress.is_cancel_requested():
                progress.mark_cancelled()
            return None

        dense_started_at = time.perf_counter()
        if progress is not None:
            progress.set_stage(
                "Scanning archive frames",
                window_start_seconds=scan_start_seconds,
                window_end_seconds=scan_end_seconds,
            )
        result = self._scan_video_range(
            video_path,
            candidates,
            scan_start_seconds,
            scan_end_seconds - scan_start_seconds,
            frame_size,
            progress=progress,
        )
        if progress is not None and progress.is_cancel_requested():
            progress.mark_cancelled()
            return None
        best_score = result.score
        best_relative_seconds = result.relative_seconds
        _best_frame = result.frame
        scanned_frames = result.scanned_frames
        dense_seconds = time.perf_counter() - dense_started_at
        self.logger.debug(
            "Video archive scan dense pass: %s frames in %.3fs; best %.3f at +%.3fs",
            scanned_frames,
            dense_seconds,
            best_score,
            best_relative_seconds,
        )

        if scanned_frames <= 0:
            self.logger.debug(
                "Video archive scan rejected in %.3fs: frames=%s best=%.3f",
                time.perf_counter() - total_started_at,
                scanned_frames,
                best_score,
            )
            return None
        if progress is not None:
            progress.stage = "Finished"
            progress.current_seconds = best_relative_seconds
            progress.scanned_frames = scanned_frames
            progress.best_score = best_score
            progress.done = True
            progress.completed_at = time.time()
        self.logger.debug(
            "Video archive scan matched in %.3fs: score=%.3f relative=%.3fs frames=%s",
            time.perf_counter() - total_started_at,
            best_score,
            best_relative_seconds,
            scanned_frames,
        )
        matches = tuple(
            VideoScanMatch(
                timestamp=archive_start_timestamp + match.relative_seconds,
                score=match.score,
                relative_seconds=match.relative_seconds,
                frame=match.frame,
            )
            for match in result.matches
        )
        if not matches:
            return None
        return VideoScanResult(
            scanned_frames=scanned_frames,
            matches=matches,
        )

    def _extract_frame_command(
        self,
        video_path: Path,
        output_path: Path,
        relative_seconds: float,
        quality: int,
    ) -> list[str]:
        preroll_seconds = min(max(10.0, float(self.keyint) * 3.0), relative_seconds)
        seek_seconds = max(0.0, relative_seconds - preroll_seconds)
        decode_seconds = relative_seconds - seek_seconds
        select_filter = f"select=gte(t\\,{decode_seconds:.6f})"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{seek_seconds:.6f}",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-vf", select_filter,
            "-frames:v", "1",
            "-q:v", str(max(1, int(quality))),
        ]
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            command.extend(["-pix_fmt", "yuvj420p"])
        command.append(str(output_path))
        return command

    def _scan_frames_command(
        self,
        video_path: Path,
        *,
        start_seconds: float = 0.0,
        duration_seconds: float | None = None,
        select_interval_seconds: float | None = None,
        show_frame_info: bool = False,
    ) -> list[str]:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info" if show_frame_info else "error",
        ]
        command.extend([
            "-i", str(video_path),
            "-map", "0:v:0",
        ])
        filters: list[str] = []
        range_filter: str | None = None
        if start_seconds > 0 or duration_seconds is not None:
            if duration_seconds is None:
                range_filter = f"gte(t\\,{start_seconds:.6f})"
            else:
                end_seconds = start_seconds + duration_seconds
                range_filter = f"between(t\\,{start_seconds:.6f}\\,{end_seconds:.6f})"
        if select_interval_seconds is not None:
            interval = max(0.25, select_interval_seconds)
            interval_filter = f"isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval:.6f})"
            if range_filter is not None:
                filters.append(f"select={range_filter}*({interval_filter})")
            else:
                filters.append(f"select={interval_filter}")
        elif range_filter is not None:
            filters.append(f"select={range_filter}")
        filters.append(f"scale={self.width}:{self.height}")
        if show_frame_info:
            filters.append("showinfo")
        command.extend([
            "-vf", ",".join(filters),
            "-fps_mode", "passthrough",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ])
        return command

    def _scan_templates(self, image: Image.Image) -> list[MatLike]:
        import cv2
        import numpy as np

        if image.mode != "RGB":
            image = image.convert("RGB")
        source = np.array(image)
        source_h, source_w = source.shape[:2]
        if source_h < 8 or source_w < 8:
            return []

        templates: list[MatLike] = []
        seen_sizes: set[tuple[int, int]] = set()
        frame_pixels = self.width * self.height
        reverse_template_count = 0
        reverse_template_limit = 3

        def add_template(width: int, height: int) -> None:
            nonlocal reverse_template_count

            if width < 8 or height < 8:
                return
            if width * height > frame_pixels * 4:
                return
            can_match_frame = width <= self.width and height <= self.height
            can_contain_frame = width >= self.width and height >= self.height
            if not can_match_frame and not can_contain_frame:
                return
            if can_contain_frame and not can_match_frame:
                if reverse_template_count >= reverse_template_limit:
                    return
                reverse_template_count += 1
            if (width, height) in seen_sizes:
                return
            seen_sizes.add((width, height))
            interpolation = cv2.INTER_AREA if width <= source_w and height <= source_h else cv2.INTER_LINEAR
            resized = cv2.resize(source, (width, height), interpolation=interpolation)
            if math.isclose(float(resized.std()), 0.0, abs_tol=0.01):
                return
            templates.append(resized)

        dynamic_scales: list[float] = []
        source_aspect = source_w / source_h
        archive_aspect = self.width / self.height
        full_frame_like = (
            source_w >= self.width
            and source_h >= self.height
            and abs(source_aspect - archive_aspect) / archive_aspect <= 0.08
        )
        if source_w > self.width or source_h > self.height:
            fit_scale = min(self.width / source_w, self.height / source_h)
            width_fit_scale = self.width / source_w
            height_fit_scale = self.height / source_h
            dynamic_scales.extend([
                fit_scale,
                width_fit_scale,
                height_fit_scale,
                fit_scale * 0.98,
                fit_scale * 1.02,
            ])
            add_template(self.width, self.height)
            if full_frame_like:
                return templates

        if source_w < self.width and source_h < self.height:
            scales = [
                2.0,
                1.75,
                1.5,
                1.25,
                1.0,
                0.9,
                0.8,
                0.75,
                2 / 3,
                0.6,
                0.55,
                0.5,
                0.45,
                0.4,
                1 / 3,
                0.25,
                0.2,
            ]
        else:
            scales = [
                1.0,
                0.75,
                0.5,
                *dynamic_scales,
                0.45,
                0.4,
                1 / 3,
                0.25,
                0.2,
            ]
        for scale in scales:
            width = int(round(source_w * scale))
            height = int(round(source_h * scale))
            add_template(width, height)
        return templates

    def _scan_candidates(
        self,
        video_path: Path,
        templates: list[MatLike],
        *,
        locator_interval_seconds: float,
        max_candidates: int,
        start_seconds: float = 0.0,
        duration_seconds: float | None = None,
        progress: ScanProgress | None = None,
    ) -> list[_ScanCandidate]:
        frame_size = self.width * self.height * 3
        process = subprocess.Popen(
            self._scan_frames_command(
                video_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
                select_interval_seconds=locator_interval_seconds,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if progress is not None:
            progress.set_process(process)
        assert process.stdout is not None

        candidates: dict[tuple[int, int, int, int], _ScanCandidate] = {}
        pending: dict[Future[dict[tuple[int, int, int, int], _ScanCandidate]], None] = {}
        executor = ThreadPoolExecutor(
            max_workers=SCAN_THREADS,
            thread_name_prefix="BogobotVideoScanLocate",
        )

        def merge_candidates(
            new_candidates: dict[tuple[int, int, int, int], _ScanCandidate],
        ) -> None:
            for key, candidate in new_candidates.items():
                existing = candidates.get(key)
                if existing is not None and existing.locator_score >= candidate.locator_score:
                    continue
                candidates[key] = candidate

        def collect_finished(done: set[Future[dict[tuple[int, int, int, int], _ScanCandidate]]]) -> None:
            for future in done:
                pending.pop(future)
                merge_candidates(future.result())

        def collect_one_finished() -> None:
            if not pending:
                return
            done, _pending = wait(pending.keys(), return_when=FIRST_COMPLETED)
            collect_finished(done)

        try:
            eof = False
            while True:
                if progress is not None and progress.is_cancel_requested():
                    with contextlib.suppress(OSError):
                        process.terminate()
                    break
                batch: list[bytes] = []
                for _ in range(SCAN_BATCH_SIZE):
                    data = process.stdout.read(frame_size)
                    if not data:
                        eof = True
                        break
                    if len(data) != frame_size:
                        eof = True
                        break
                    batch.append(bytes(data))
                if not batch:
                    break
                future = executor.submit(self._scan_candidate_batch, batch, templates)
                pending[future] = None
                while len(pending) >= SCAN_THREADS * 2:
                    collect_one_finished()
                if eof:
                    break
            while pending and not (progress is not None and progress.is_cancel_requested()):
                collect_one_finished()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            if progress is not None:
                progress.set_process(None)
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()

        if process.stderr is not None:
            stderr = process.stderr.read()
            process.stderr.close()
        else:
            stderr = b""
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if progress is not None and progress.is_cancel_requested():
            return []
        if process.returncode not in (0, None):
            message = stderr.decode(errors="replace").strip()
            if message:
                self.logger.warning(f"Video archive scan locator failed: {message}")
            return []

        return sorted(
            candidates.values(),
            key=lambda candidate: candidate.locator_score,
            reverse=True,
        )[:max(1, int(max_candidates))]

    def _scan_candidate_batch(
        self,
        batch: list[bytes],
        templates: list[MatLike],
    ) -> dict[tuple[int, int, int, int], _ScanCandidate]:
        import cv2
        import numpy as np

        candidates: dict[tuple[int, int, int, int], _ScanCandidate] = {}
        for data in batch:
            frame = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
            for template in templates:
                height, width = template.shape[:2]
                if width <= self.width and height <= self.height:
                    result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
                    _min_value, max_value, _min_loc, max_loc = cv2.minMaxLoc(result)
                    x, y = max_loc
                    candidate_template = template
                elif width >= self.width and height >= self.height:
                    result = cv2.matchTemplate(template, frame, cv2.TM_CCOEFF_NORMED)
                    _min_value, max_value, _min_loc, max_loc = cv2.minMaxLoc(result)
                    template_x, template_y = max_loc
                    x = 0
                    y = 0
                    width = self.width
                    height = self.height
                    candidate_template = template[
                        template_y:template_y + self.height,
                        template_x:template_x + self.width,
                    ]
                else:
                    continue
                key = (x // 2, y // 2, width, height)
                score = float(max_value)
                existing = candidates.get(key)
                if existing is not None and existing.locator_score >= score:
                    continue
                sample_step = self._scan_sample_step(width, height)
                candidates[key] = _ScanCandidate(
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    sample_step=sample_step,
                    template=candidate_template[::sample_step, ::sample_step].copy(),
                    locator_score=score,
                )
        return candidates

    def _scan_video_range(
        self,
        video_path: Path,
        candidates: list[_ScanCandidate],
        start_seconds: float,
        duration_seconds: float | None,
        frame_size: int,
        *,
        progress: ScanProgress | None = None,
    ) -> _ScanRangeResult:
        started_at = time.perf_counter()
        process = subprocess.Popen(
            self._scan_frames_command(
                video_path,
                start_seconds=start_seconds,
                duration_seconds=duration_seconds,
                show_frame_info=True,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if progress is not None:
            progress.set_process(process)
        assert process.stdout is not None
        assert process.stderr is not None

        best_score = -1.0
        best_relative_seconds = start_seconds
        best_frame: bytes | None = None
        best_frame_index = 0
        top_matches = _TopScanMatches(SCAN_RESULT_LIMIT)
        scanned_frames = 0
        stderr_chunks: list[bytes] = []
        stderr_lock = threading.Lock()
        last_progress_at = 0.0
        submitted_frames = 0
        pending: dict[Future[list[float]], tuple[int, list[bytes]]] = {}
        executor = ThreadPoolExecutor(
            max_workers=SCAN_THREADS,
            thread_name_prefix="BogobotVideoScanScore",
        )

        def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = process.stderr.read(65536)
                if not chunk:
                    return
                with stderr_lock:
                    stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(
            target=read_stderr,
            name="BogobotVideoScanStderr",
            daemon=True,
        )
        stderr_thread.start()

        def update_progress() -> None:
            nonlocal last_progress_at

            now = time.monotonic()
            if now - last_progress_at < 1.0:
                return
            last_progress_at = now
            with stderr_lock:
                stderr = b"".join(stderr_chunks)
            current_seconds = self._scan_latest_showinfo_time(stderr)
            if progress is not None:
                progress.current_seconds = current_seconds
                progress.scanned_frames = scanned_frames
                progress.best_score = best_score if best_score >= 0 else None

        def collect_finished(done: set[Future[list[float]]]) -> None:
            nonlocal best_score, best_frame, best_frame_index, scanned_frames

            for future in done:
                start_index, batch = pending.pop(future)
                scores = future.result()
                for offset, score in enumerate(scores):
                    frame_index = start_index + offset
                    if score > best_score:
                        best_score = score
                        best_frame = batch[offset]
                        best_frame_index = frame_index
                    top_matches.insert(_ScanRangeMatch(
                        score=score,
                        frame_index=frame_index,
                        relative_seconds=start_seconds,
                        frame=batch[offset],
                    ))
                scanned_frames += len(scores)
            update_progress()

        def collect_one_finished() -> None:
            if not pending:
                return
            done, _pending = wait(pending.keys(), return_when=FIRST_COMPLETED)
            collect_finished(done)

        try:
            eof = False
            while True:
                if progress is not None and progress.is_cancel_requested():
                    with contextlib.suppress(OSError):
                        process.terminate()
                    break
                batch: list[bytes] = []
                for _ in range(SCAN_BATCH_SIZE):
                    data = process.stdout.read(frame_size)
                    if not data:
                        eof = True
                        break
                    if len(data) != frame_size:
                        eof = True
                        break
                    batch.append(bytes(data))
                if not batch:
                    break
                future = executor.submit(self._scan_frame_scores, batch, candidates)
                pending[future] = (submitted_frames, batch)
                submitted_frames += len(batch)
                while len(pending) >= SCAN_THREADS * 2:
                    collect_one_finished()
                if eof:
                    break
            while pending and not (progress is not None and progress.is_cancel_requested()):
                collect_one_finished()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            if progress is not None:
                progress.set_process(None)
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()

        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        stderr_thread.join(timeout=5)
        with stderr_lock:
            stderr = b"".join(stderr_chunks)
        if progress is not None and progress.is_cancel_requested():
            return _ScanRangeResult(-1.0, start_seconds, scanned_frames, None, ())
        best_pts_time = self._scan_showinfo_time(stderr, best_frame_index)
        if best_pts_time is not None:
            best_relative_seconds = best_pts_time
        matches = tuple(
            _ScanRangeMatch(
                score=match.score,
                frame_index=match.frame_index,
                relative_seconds=self._scan_showinfo_time(stderr, match.frame_index) or start_seconds,
                frame=match.frame,
            )
            for match in top_matches.get_top_n()
        )

        if process.returncode not in (0, None):
            message = stderr.decode(errors="replace").strip()
            if message:
                self.logger.warning(f"Video archive scan failed: {message}")
            return _ScanRangeResult(-1.0, start_seconds, scanned_frames, None, ())
        self.logger.debug(
            "Video archive scan pass: +%.3fs duration=%s frames=%s time=%.3fs best=%.3f at +%.3fs",
            start_seconds,
            f"{duration_seconds:.3f}s" if duration_seconds is not None else "end",
            scanned_frames,
            time.perf_counter() - started_at,
            best_score,
            best_relative_seconds,
        )
        return _ScanRangeResult(
            best_score,
            best_relative_seconds,
            scanned_frames,
            best_frame,
            matches,
        )

    def _scan_frame_score(self, data: bytes, candidates: list[_ScanCandidate]) -> float:
        import cv2
        import numpy as np

        frame = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
        best = -1.0
        for candidate in candidates:
            roi = frame[
                candidate.y:candidate.y + candidate.height:candidate.sample_step,
                candidate.x:candidate.x + candidate.width:candidate.sample_step,
            ]
            if roi.shape[:2] != candidate.template.shape[:2]:
                continue
            if math.isclose(float(roi.std()), 0.0, abs_tol=0.01):
                continue
            correlation_score = float(cv2.matchTemplate(
                roi,
                candidate.template,
                cv2.TM_CCOEFF_NORMED,
            )[0][0])
            mean_abs_diff = float(np.abs(
                roi.astype(np.int16) - candidate.template.astype(np.int16)
            ).mean())
            difference_score = 1.0 - math.sqrt(mean_abs_diff / 255.0)
            score = (correlation_score + difference_score) / 2
            best = max(best, score)
        return best

    def _scan_frame_scores(
        self,
        batch: list[bytes],
        candidates: list[_ScanCandidate],
    ) -> list[float]:
        return [
            self._scan_frame_score(data, candidates)
            for data in batch
        ]

    def _scan_sample_step(self, width: int, height: int) -> int:
        if width == self.width and height == self.height:
            return 1
        return max(1, min(width, height) // 96)

    def _scan_showinfo_time(self, stderr: bytes, frame_index: int) -> float | None:
        matches = self._scan_showinfo_matches(stderr)
        if 0 <= frame_index < len(matches):
            with contextlib.suppress(ValueError):
                return float(matches[frame_index].group("time"))
        return None

    def _scan_latest_showinfo_time(self, stderr: bytes) -> float | None:
        matches = self._scan_showinfo_matches(stderr)
        if not matches:
            return None
        with contextlib.suppress(ValueError):
            return float(matches[-1].group("time"))
        return None

    def _scan_showinfo_matches(self, stderr: bytes) -> list[re.Match[str]]:
        text = stderr.decode(errors="replace")
        return list(SHOWINFO_RE.finditer(text))

    def _video_duration(self, video_path: Path) -> float | None:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            with contextlib.suppress(ValueError):
                duration = float(result.stdout.decode(errors="replace").strip())
                if math.isfinite(duration) and duration > 0:
                    return duration

        frame_pts = self._read_frame_pts(video_path)
        timeline_pts = self._frame_timeline_pts(frame_pts)
        if not timeline_pts:
            return None
        return timeline_pts[-1]

    def _frame_timeline_pts(self, frame_pts: list[tuple[int, float]]) -> list[float]:
        segment_base = 0.0
        segment_first_pts = frame_pts[0][1]
        previous_pts = segment_first_pts
        previous_timeline_pts = 0.0
        timeline_pts: list[float] = []
        for _, pts in frame_pts:
            if pts < previous_pts:
                segment_base = previous_timeline_pts
                segment_first_pts = pts
            timeline_pt = segment_base + max(0.0, pts - segment_first_pts)
            previous_pts = pts
            previous_timeline_pts = timeline_pt
            timeline_pts.append(timeline_pt)
        return timeline_pts

    def repair_recording_timestamps(self, ts_path: Path) -> bool:
        if ts_path.suffix != ".ts" or not ts_path.exists():
            return False
        frame_pts = self._read_frame_pts(ts_path)
        if not self._has_pts_reset(frame_pts):
            return False

        tmp_path = ts_path.with_name(f"{ts_path.stem}.repairing.ts")
        # Keep each original in-segment frame delta, but collapse timestamp resets.
        setpts = (
            "settb=AVTB,"
            "setpts=if(isnan(PREV_INPTS)\\,0\\,"
            "if(lt(PTS\\,PREV_INPTS)\\,PREV_OUTPTS\\,PREV_OUTPTS+PTS-PREV_INPTS))"
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(ts_path),
            "-an",
            "-vf", setpts,
            "-fps_mode", "passthrough",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx265",
            "-preset", self.preset,
            "-crf", str(self.crf),
        ]
        if self.tune is not None:
            command.extend(["-tune", self.tune])
        command.extend([
            "-x265-params", f"keyint={self.keyint}:min-keyint={self.keyint}",
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-f", "mpegts",
            str(tmp_path),
        ])
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self.logger.warning(f"Could not repair video archive timestamps {ts_path}: {stderr}")
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            return False

        repaired_pts = self._read_frame_pts(tmp_path)
        if not repaired_pts or self._has_pts_reset(repaired_pts):
            self.logger.warning(f"Repaired video archive still has invalid timestamps: {ts_path}")
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            return False

        tmp_path.replace(ts_path)
        self.logger.info(f"Repaired video archive timestamps in {ts_path}")
        return True

    def _read_frame_pts(self, video_path: Path) -> list[tuple[int, float]]:
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
            return []

        frame_pts: list[tuple[int, float]] = []
        for line in result.stdout.decode(errors="replace").splitlines():
            value = line.strip().rstrip(",")
            if not value:
                continue
            try:
                frame_pts.append((len(frame_pts), float(value)))
            except ValueError:
                continue
        return frame_pts

    def _has_pts_reset(self, frame_pts: list[tuple[int, float]]) -> bool:
        previous_pts: float | None = None
        for _, pts in frame_pts:
            if previous_pts is not None and pts < previous_pts:
                return True
            previous_pts = pts
        return False

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
            self._ensure_start_timestamp(day, frame.timestamp)
            self._start_process(day, frame.timestamp)

        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Video archive ffmpeg process is not writable")

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

    def _start_process(self, day: str, timestamp: float) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        output_path = self.directory / f"{day}.ts"
        self.repair_recording_timestamps(output_path)
        output_file = output_path.open("ab")
        start_timestamp = self._read_start_timestamp(day)
        if start_timestamp is None:
            start_timestamp = timestamp
        timestamp_offset = max(0.0, timestamp - start_timestamp)

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
            "-vf", f"setpts=PTS-STARTPTS+{timestamp_offset:.6f}/TB",
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
            "-muxpreload", "0",
            "-muxdelay", "0",
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
