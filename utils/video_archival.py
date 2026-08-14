import contextlib
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import heapq
import math
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from cv2.typing import MatLike

from utils.logger_pipe import PipeLogger, log_subprocess_pipe


SCAN_MAX_THREAD_CAP = 8
SCAN_THREAD_HEADROOM = 1.0
SCAN_THREAD_SAMPLE_SECONDS = 2.0
SCAN_THREAD_FALLBACK_CPU_FRACTION = 0.5
SCAN_THREAD_TARGET_PROCESS_CPU_FRACTION = 0.75
SCAN_LOCATOR_PROGRESS_WEIGHT = 2 / 3
SCAN_DENSE_PROGRESS_WEIGHT = 1 / 3
SCAN_TEMPLATE_SCALE_MAX = 2.0
SCAN_TEMPLATE_SCALE_MIN = 0.2
SCAN_TEMPLATE_SCALE_STEP = 0.01
SCAN_LOCATOR_SIZE = 60
SCAN_SHORT_WINDOW_SECONDS = 30 * 60
SCAN_SHORT_WINDOW_LOCATOR_INTERVAL_SECONDS = 5.0
SCAN_LOCATOR_SCALE_EVALUATIONS = 2
SCAN_FRAME_STREAM_PREROLL_SECONDS = 180.0
SCAN_BATCH_SIZE = 64
SCAN_RESULT_LIMIT = 8
SCAN_MAX_CANDIDATES = 4
FRAME_TIMELINE_CACHE_SIZE = 4
FrameTimeline = NDArray[np.float64]
FrameIndices = NDArray[np.int64]


class _AdaptiveScanThreadBudget:
    def __init__(self, *, logger: logging.Logger, stage: str):
        self.logger = logger
        self.stage = stage
        self.cpu_count = max(1, os.cpu_count() or 1)
        self.max_workers = max(
            1,
            min(
                SCAN_MAX_THREAD_CAP,
                self.cpu_count - 1 if self.cpu_count > 1 else 1,
            ),
        )
        self._last_process_time: float | None = None
        self._last_wall_time: float | None = None
        self._active_workers = self._sample_active_workers()
        self._sampled_at = time.monotonic()
        self.logger.debug(
            "Video archive scan %s active thread budget: %s/%s workers",
            self.stage,
            self._active_workers,
            self.max_workers,
        )

    @property
    def active_workers(self) -> int:
        now = time.monotonic()
        if now - self._sampled_at < SCAN_THREAD_SAMPLE_SECONDS:
            return self._active_workers

        self._sampled_at = now
        sampled_workers = self._sample_active_workers()
        if sampled_workers != self._active_workers:
            self.logger.debug(
                "Video archive scan %s active thread budget changed: %s -> %s workers",
                self.stage,
                self._active_workers,
                sampled_workers,
            )
            self._active_workers = sampled_workers
        return self._active_workers

    @property
    def pending_limit(self) -> int:
        return self.active_workers

    def _sample_active_workers(self) -> int:
        load_average = self._load_average()
        if load_average is None:
            return self._sample_active_workers_from_process_cpu()

        available_cpu = math.floor(self.cpu_count - load_average - SCAN_THREAD_HEADROOM)
        return max(1, min(self.max_workers, available_cpu))

    def _sample_active_workers_from_process_cpu(self) -> int:
        current_process_time = time.process_time()
        current_wall_time = time.monotonic()

        if self._last_process_time is None or self._last_wall_time is None:
            self._last_process_time = current_process_time
            self._last_wall_time = current_wall_time
            return self._fallback_workers()

        process_delta = current_process_time - self._last_process_time
        wall_delta = current_wall_time - self._last_wall_time
        self._last_process_time = current_process_time
        self._last_wall_time = current_wall_time
        if wall_delta <= 0:
            return self._fallback_workers()

        process_cpu = max(0.0, process_delta / wall_delta)
        target_cpu = max(
            1.0,
            (self.cpu_count - SCAN_THREAD_HEADROOM)
            * SCAN_THREAD_TARGET_PROCESS_CPU_FRACTION,
        )

        if process_cpu > target_cpu and self._active_workers > 1:
            return self._active_workers - 1
        if process_cpu < target_cpu * 0.6 and self._active_workers < self.max_workers:
            return self._active_workers + 1
        return self._active_workers

    def _fallback_workers(self) -> int:
        return max(
            1,
            min(
                self.max_workers,
                math.ceil(self.cpu_count * SCAN_THREAD_FALLBACK_CPU_FRACTION),
            ),
        )

    def _load_average(self) -> float | None:
        with contextlib.suppress(AttributeError, OSError):
            return os.getloadavg()[0]
        return None


@dataclass(frozen=True)
class VideoArchiveStatus:
    enabled: bool
    running: bool
    current_path: Path | None
    recorded_frames: int
    dropped_frames: int
    last_frame_at: float | None


@dataclass(frozen=True)
class VideoArchiveRange:
    day: str
    start_timestamp: float
    end_timestamp: float


@dataclass(frozen=True)
class VideoArchiveFrame:
    timestamp: float
    data: bytes


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
    progress_base: float = 0.0
    progress_weight: float = 1.0
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
        stage_ratio = max(0.0, min(1.0, (self.current_seconds - self.window_start_seconds) / span))
        return max(0.0, min(1.0, self.progress_base + stage_ratio * self.progress_weight))

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
        stage_ratio = max(0.0, min(1.0, current_units / total_units))
        current_overall = self.progress_base + stage_ratio * self.progress_weight
        if current_overall <= 0:
            return None
        estimated_total = self.elapsed_seconds / current_overall
        return max(0.0, estimated_total - self.elapsed_seconds)

    def set_stage(
        self,
        stage: str,
        *,
        window_start_seconds: float | None = None,
        window_end_seconds: float | None = None,
        progress_base: float | None = None,
        progress_weight: float | None = None,
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
        if progress_base is not None:
            self.progress_base = progress_base
        if progress_weight is not None:
            self.progress_weight = progress_weight

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
    locator_key: tuple[int, ...]
    template_id: int
    x: int
    y: int
    width: int
    height: int
    sample_step: int
    template: MatLike
    template_centered: MatLike
    template_int16: MatLike
    locator_score: float


@dataclass(frozen=True)
class _ScanRangeResult:
    score: float
    scanned_frames: int
    matches: tuple["_ScanRangeMatch", ...]


@dataclass(frozen=True)
class _PreparedScanRange:
    day: str
    video_path: Path
    archive_start_timestamp: float
    start_timestamp: float
    end_timestamp: float
    decode_start_timestamp: float | None = None
    timeline_pts: FrameTimeline = field(default_factory=lambda: np.array([], dtype=np.float64))

    @property
    def start_seconds(self) -> float:
        return self.start_timestamp - self.archive_start_timestamp

    @property
    def decode_start_seconds(self) -> float | None:
        if self.decode_start_timestamp is None:
            return None
        return self.decode_start_timestamp - self.archive_start_timestamp

    @property
    def duration_seconds(self) -> float:
        return self.end_timestamp - self.start_timestamp


@dataclass(frozen=True)
class _ScanRangeMatch:
    score: float
    timestamp: float
    relative_seconds: float
    frame: bytes | None


@dataclass(frozen=True)
class _ScanFrame:
    timestamp: float
    relative_seconds: float
    data: bytes


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


class _TopScanCandidates:
    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._next_index = 0
        self._heap: list[tuple[float, int, tuple[int, ...], _ScanCandidate]] = []
        self._by_key: dict[tuple[int, ...], _ScanCandidate] = {}

    def insert(self, key: tuple[int, ...], candidate: _ScanCandidate) -> None:
        existing = self._by_key.get(key)
        if existing is not None and existing.locator_score >= candidate.locator_score:
            return

        self._by_key[key] = candidate
        heapq.heappush(self._heap, (candidate.locator_score, self._next_index, key, candidate))
        self._next_index += 1

        if len(self._heap) > self.limit * 4:
            self._prune()

    def extend(self, candidates: Iterable[_ScanCandidate]) -> None:
        for candidate in candidates:
            self.insert(self._key(candidate), candidate)

    def get_top_n(self) -> tuple[_ScanCandidate, ...]:
        self._prune()
        return tuple(
            item[3]
            for item in sorted(self._heap, key=lambda item: item[0], reverse=True)
        )

    def _prune(self) -> None:
        candidates = sorted(
            self._by_key.values(),
            key=lambda candidate: candidate.locator_score,
            reverse=True,
        )[:self.limit]
        self._by_key = {
            self._key(candidate): candidate
            for candidate in candidates
        }
        self._heap = [
            (candidate.locator_score, index, self._key(candidate), candidate)
            for index, candidate in enumerate(candidates)
        ]
        heapq.heapify(self._heap)
        self._next_index = len(self._heap)

    def _key(self, candidate: _ScanCandidate) -> tuple[int, ...]:
        return candidate.locator_key


@dataclass(frozen=True)
class _QueuedFrame:
    image: Image.Image
    timestamp: float


class VideoScanner:
    def __init__(
        self,
        *,
        width: int,
        height: int,
        logger: logging.Logger,
    ):
        self.width = width
        self.height = height
        self.logger = logger

    def frame_window(
        self,
        ranges: list[_PreparedScanRange],
        *,
        target_timestamp: float,
        before: int,
        after: int,
    ) -> tuple[VideoArchiveFrame, ...]:
        frames = [
            VideoArchiveFrame(timestamp=frame.timestamp, data=frame.data)
            for frame in self._frame_stream(ranges)
        ]
        if not frames:
            return ()

        target_index = next(
            (
                index
                for index, frame in enumerate(frames)
                if frame.timestamp >= target_timestamp
            ),
            len(frames) - 1,
        )
        start_index = max(0, target_index - max(0, before))
        end_index = min(len(frames), target_index + max(0, after) + 1)
        return tuple(frames[start_index:end_index])

    def scan_for_image(
        self,
        ranges: list[_PreparedScanRange],
        image: Image.Image,
        *,
        start_timestamp: float,
        end_timestamp: float,
        locator_interval_seconds: float = 30.0,
        progress: ScanProgress | None = None,
    ) -> VideoScanResult | None:
        total_started_at = time.perf_counter()
        if not ranges:
            return None

        effective_locator_interval_seconds = self._locator_interval_seconds(
            start_timestamp,
            end_timestamp,
            locator_interval_seconds,
        )
        locator_started_at = time.perf_counter()
        if progress is not None:
            progress.set_stage(
                "Locating image",
                window_start_seconds=float(start_timestamp),
                window_end_seconds=float(end_timestamp),
                progress_base=0.0,
                progress_weight=SCAN_LOCATOR_PROGRESS_WEIGHT,
            )
        candidates = self._candidates_from_frames(
            self._frame_stream(
                ranges,
                select_interval_seconds=effective_locator_interval_seconds,
                progress=progress,
            ),
            image,
            max_candidates=SCAN_MAX_CANDIDATES,
            progress=progress,
        )
        self.logger.debug(
            "Video archive scan locator: %s candidates in %.3fs over %.3fs..%.3fs interval=%.3fs",
            len(candidates),
            time.perf_counter() - locator_started_at,
            start_timestamp,
            end_timestamp,
            effective_locator_interval_seconds,
        )
        if not candidates:
            if progress is not None and progress.is_cancel_requested():
                progress.mark_cancelled()
            return None

        dense_started_at = time.perf_counter()
        if progress is not None:
            progress.set_stage(
                "Scanning archive frames",
                window_start_seconds=float(start_timestamp),
                window_end_seconds=float(end_timestamp),
                progress_base=SCAN_LOCATOR_PROGRESS_WEIGHT,
                progress_weight=SCAN_DENSE_PROGRESS_WEIGHT,
            )
        result = self._scan_frame_stream(
            self._frame_stream(ranges, progress=progress),
            candidates,
            progress=progress,
        )
        if progress is not None and progress.is_cancel_requested():
            progress.mark_cancelled()
            return None
        matches = tuple(
            VideoScanMatch(
                timestamp=match.timestamp,
                score=match.score,
                relative_seconds=match.relative_seconds,
                frame=match.frame,
            )
            for match in result.matches
        )
        if result.scanned_frames <= 0 or not matches:
            self.logger.debug(
                "Video archive scan rejected in %.3fs: frames=%s best=%.3f",
                time.perf_counter() - total_started_at,
                result.scanned_frames,
                result.score,
            )
            return None
        if progress is not None:
            progress.stage = "Finished"
            progress.current_seconds = float(end_timestamp)
            progress.scanned_frames = result.scanned_frames
            progress.best_score = matches[0].score
            progress.done = True
            progress.completed_at = time.time()
        self.logger.debug(
            "Video archive scan matched in %.3fs: dense %.3fs score=%.3f frames=%s",
            time.perf_counter() - total_started_at,
            time.perf_counter() - dense_started_at,
            matches[0].score,
            result.scanned_frames,
        )
        return VideoScanResult(scanned_frames=result.scanned_frames, matches=matches)

    def _frames_command(
        self,
        video_path: Path,
        *,
        selected_frame_indices: FrameIndices,
        input_duration_seconds: float | None = None,
    ) -> list[str]:
        filters = [
            self._frame_index_select_filter(selected_frame_indices),
            f"scale={self.width}:{self.height}",
        ]
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
        ]
        if input_duration_seconds is not None:
            command.extend(["-t", f"{input_duration_seconds:.6f}"])
        command.extend([
            "-i", str(video_path),
        ])
        command.extend([
            "-map", "0:v:0",
            "-vf", ",".join(filters),
            "-fps_mode", "passthrough",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ])
        return command

    def _frame_index_select_filter(self, selected_frame_indices: FrameIndices) -> str:
        if selected_frame_indices.size == 0:
            return "select=0"

        first_index = int(selected_frame_indices[0])
        last_index = int(selected_frame_indices[-1])
        if np.all(np.diff(selected_frame_indices) == 1):
            return f"select=between(n\\,{first_index}\\,{last_index})"

        return "select=" + self._frame_index_select_tree([
            int(index)
            for index in selected_frame_indices
        ])

    def _frame_index_select_tree(self, indices: list[int]) -> str:
        if not indices:
            return "0"
        if len(indices) == 1:
            return f"eq(n\\,{indices[0]})"

        mid = len(indices) // 2
        pivot = indices[mid]
        left_side = self._frame_index_select_tree(indices[:mid])
        right_side = self._frame_index_select_tree(indices[mid:])
        return f"if(lt(n\\,{pivot})\\,{left_side}\\,{right_side})"

    def _selected_frame_indices(
        self,
        scan_range: _PreparedScanRange,
        select_interval_seconds: float | None,
    ) -> FrameIndices:
        timeline_pts = scan_range.timeline_pts
        if timeline_pts.size == 0:
            return np.array([], dtype=np.int64)

        start_seconds = scan_range.start_seconds
        end_seconds = scan_range.start_seconds + scan_range.duration_seconds
        interval = (
            None
            if select_interval_seconds is None else
            max(0.25, select_interval_seconds)
        )
        start_index = int(np.searchsorted(timeline_pts, start_seconds - 0.000001, side="left"))
        end_index = int(np.searchsorted(timeline_pts, end_seconds + 0.000001, side="right"))
        if interval is None:
            return np.arange(start_index, end_index, dtype=np.int64)

        selected: list[int] = []
        previous_selected: float | None = None
        for index in range(start_index, end_index):
            pts = float(timeline_pts[index])
            if previous_selected is not None and pts - previous_selected < interval:
                continue
            previous_selected = pts
            selected.append(index)
        return np.asarray(selected, dtype=np.int64)

    def _seek_plan(
        self,
        start_seconds: float,
        duration_seconds: float | None,
        *,
        decode_start_seconds: float | None = None,
    ) -> tuple[float, float, float | None]:
        if decode_start_seconds is None:
            preroll_seconds = min(
                max(0.0, SCAN_FRAME_STREAM_PREROLL_SECONDS),
                max(0.0, start_seconds),
            )
            seek_seconds = max(0.0, start_seconds - preroll_seconds)
        else:
            seek_seconds = max(0.0, min(start_seconds, decode_start_seconds))
        decode_start_seconds = max(0.0, start_seconds - seek_seconds)
        decode_duration_seconds = (
            None
            if duration_seconds is None else
            decode_start_seconds + max(0.0, duration_seconds)
        )
        return seek_seconds, decode_start_seconds, decode_duration_seconds

    def _candidate_source(self, image: Image.Image) -> MatLike | None:
        import numpy as np

        if image.mode != "RGB":
            image = image.convert("RGB")
        source = np.array(image)
        source_h, source_w = source.shape[:2]
        if source_h < 8 or source_w < 8:
            return None
        if math.isclose(float(source.std()), 0.0, abs_tol=0.01):
            return None
        return source

    def _locator_interval_seconds(
        self,
        start_timestamp: float,
        end_timestamp: float,
        locator_interval_seconds: float,
    ) -> float:
        interval = max(0.25, float(locator_interval_seconds))
        window_seconds = max(0.0, end_timestamp - start_timestamp)
        if window_seconds <= SCAN_SHORT_WINDOW_SECONDS:
            return min(interval, SCAN_SHORT_WINDOW_LOCATOR_INTERVAL_SECONDS)
        return interval

    def _template_scales(self, source_w: int, source_h: int) -> tuple[float, ...]:
        scale_count = int(round(
            (SCAN_TEMPLATE_SCALE_MAX - SCAN_TEMPLATE_SCALE_MIN)
            / SCAN_TEMPLATE_SCALE_STEP
        ))
        scales = [
            round(SCAN_TEMPLATE_SCALE_MAX - index * SCAN_TEMPLATE_SCALE_STEP, 2)
            for index in range(scale_count + 1)
        ]
        if source_w > self.width or source_h > self.height:
            fit_scale = min(self.width / source_w, self.height / source_h)
            scales.extend([
                fit_scale,
                self.width / source_w,
                self.height / source_h,
                fit_scale * 0.98,
                fit_scale * 1.02,
            ])
        return tuple(dict.fromkeys(
            scale
            for scale in scales
            if scale > 0
        ))

    def _first_locator_batch_scale_indices(
        self,
        sample_count: int,
        scale_count: int,
    ) -> list[tuple[int, ...]]:
        if sample_count <= 0 or scale_count <= 0:
            return []

        scale_evaluations = scale_count * SCAN_LOCATOR_SCALE_EVALUATIONS
        scales_per_sample = min(
            scale_count,
            max(1, math.ceil(scale_evaluations / sample_count)),
        )
        scheduled: list[tuple[int, ...]] = []
        scale_index = 0
        for sample_index in range(sample_count):
            sample_scales: list[int] = []
            seen_scales: set[int] = set()
            while (
                len(sample_scales) < scales_per_sample
                and scale_index < scale_evaluations
                and len(seen_scales) < scale_count
            ):
                candidate_scale_index = scale_index % scale_count
                scale_index += 1
                if candidate_scale_index in seen_scales:
                    continue
                seen_scales.add(candidate_scale_index)
                sample_scales.append(candidate_scale_index)
            scheduled.append(tuple(sample_scales))
            if scale_index >= scale_evaluations:
                scheduled.extend(() for _ in range(sample_count - sample_index - 1))
                break
        return scheduled

    def _candidate_scale_indices(
        self,
        candidates: tuple[_ScanCandidate, ...],
        scale_count: int,
    ) -> tuple[int, ...]:
        if scale_count <= 0:
            return ()
        scale_indices = tuple(dict.fromkeys(
            candidate.template_id
            for candidate in candidates
            if 0 <= candidate.template_id < scale_count
        ))
        if scale_indices:
            return scale_indices
        return tuple(range(min(scale_count, SCAN_MAX_CANDIDATES)))

    def _frame_stream(
        self,
        ranges: list[_PreparedScanRange],
        *,
        select_interval_seconds: float | None = None,
        progress: ScanProgress | None = None,
    ) -> Iterator[_ScanFrame]:
        frame_size = self.width * self.height * 3
        for scan_range in ranges:
            if progress is not None and progress.is_cancel_requested():
                return
            selected_frame_indices = self._selected_frame_indices(scan_range, select_interval_seconds)
            if selected_frame_indices.size == 0:
                continue
            selected_timestamps = (
                scan_range.archive_start_timestamp
                + scan_range.timeline_pts[selected_frame_indices]
            )
            timestamp_index = 0
            process = subprocess.Popen(
                self._frames_command(
                    scan_range.video_path,
                    selected_frame_indices=selected_frame_indices,
                    input_duration_seconds=float(scan_range.timeline_pts[int(selected_frame_indices[-1])]) + 1.0,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if progress is not None:
                progress.set_process(process)
            assert process.stdout is not None
            assert process.stderr is not None

            terminated = False
            try:
                while True:
                    if progress is not None and progress.is_cancel_requested():
                        terminated = True
                        with contextlib.suppress(OSError):
                            process.terminate()
                        break
                    data = process.stdout.read(frame_size)
                    if not data or len(data) != frame_size:
                        break
                    if timestamp_index >= selected_timestamps.size:
                        terminated = True
                        with contextlib.suppress(OSError):
                            process.terminate()
                        break
                    timestamp = float(selected_timestamps[timestamp_index])
                    timestamp_index += 1
                    if timestamp < scan_range.start_timestamp:
                        continue
                    if timestamp > scan_range.end_timestamp:
                        terminated = True
                        with contextlib.suppress(OSError):
                            process.terminate()
                        break
                    yield _ScanFrame(
                        timestamp=timestamp,
                        relative_seconds=timestamp - scan_range.archive_start_timestamp,
                        data=bytes(data),
                    )
            finally:
                if progress is not None:
                    progress.set_process(None)
                if process.poll() is None:
                    terminated = True
                    with contextlib.suppress(OSError):
                        process.terminate()
                if process.stdout is not None:
                    with contextlib.suppress(OSError):
                        process.stdout.close()
                stderr = b""
                if process.stderr is not None:
                    with contextlib.suppress(OSError):
                        stderr = process.stderr.read()
                    with contextlib.suppress(OSError):
                        process.stderr.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if not terminated and process.returncode not in (0, None):
                    message = stderr.decode(errors="replace").strip()
                    if message:
                        self.logger.warning(f"Video archive frame stream failed: {message}")

    def _candidates_from_frames(
        self,
        frames: Iterator[_ScanFrame],
        image: Image.Image,
        *,
        max_candidates: int,
        progress: ScanProgress | None = None,
    ) -> list[_ScanCandidate]:
        source = self._candidate_source(image)
        if source is None:
            return []
        source_h, source_w = source.shape[:2]
        scales = self._template_scales(source_w, source_h)
        self.logger.debug(
            "Video archive scan locator input: source=%sx%s scales=%s",
            source_w,
            source_h,
            len(scales),
        )
        if not scales:
            return []

        candidates = _TopScanCandidates(max_candidates)
        pending: dict[Future[tuple[_ScanCandidate, ...]], float] = {}
        thread_budget = _AdaptiveScanThreadBudget(
            logger=self.logger,
            stage="locator",
        )
        executor = ThreadPoolExecutor(max_workers=thread_budget.max_workers, thread_name_prefix="BogobotVideoScanLocate")

        def merge(new_candidates: tuple[_ScanCandidate, ...]) -> None:
            candidates.extend(new_candidates)

        def collect_one() -> None:
            if not pending:
                return
            done, _pending = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                timestamp = pending.pop(future)
                merge(future.result())
                if progress is not None:
                    progress.current_seconds = timestamp

        def submit_batch(
            batch: list[tuple[bytes, tuple[int, ...]]],
            batch_timestamp: float,
        ) -> None:
            while len(pending) >= thread_budget.pending_limit:
                collect_one()
            pending[executor.submit(self._candidate_batch, batch, source, scales)] = batch_timestamp

        def submit_discovery_batches(first_frames: list[_ScanFrame]) -> None:
            if not first_frames:
                return
            first_schedule = self._first_locator_batch_scale_indices(
                len(first_frames),
                len(scales),
            )
            first_batch = [
                (frame.data, scale_indices)
                for frame, scale_indices in zip(first_frames, first_schedule, strict=True)
                if scale_indices
            ]
            if not first_batch:
                return

            worker_count = max(1, min(thread_budget.active_workers, len(first_batch)))
            chunk_size = max(1, math.ceil(len(first_batch) / worker_count))
            for offset in range(0, len(first_batch), chunk_size):
                chunk = first_batch[offset:offset + chunk_size]
                timestamp_index = min(offset + len(chunk) - 1, len(first_frames) - 1)
                submit_batch(chunk, first_frames[timestamp_index].timestamp)

        try:
            frame_iter = iter(frames)
            first_frames: list[_ScanFrame] = []
            for frame in frame_iter:
                if progress is not None and progress.is_cancel_requested():
                    break
                first_frames.append(frame)
                if len(first_frames) >= SCAN_LOCATOR_SIZE:
                    break
            self.logger.debug(
                "Video archive scan locator discovery frames: %s",
                len(first_frames),
            )

            if first_frames and not (progress is not None and progress.is_cancel_requested()):
                submit_discovery_batches(first_frames)
                while pending and not (progress is not None and progress.is_cancel_requested()):
                    collect_one()
                self.logger.debug(
                    "Video archive scan locator candidates after discovery: %s",
                    len(candidates.get_top_n()),
                )

            batch: list[tuple[bytes, tuple[int, ...]]] = []
            batch_timestamp = 0.0
            for frame in frame_iter:
                if progress is not None and progress.is_cancel_requested():
                    break
                scale_indices = self._candidate_scale_indices(
                    candidates.get_top_n(),
                    len(scales),
                )
                batch.append((
                    frame.data,
                    scale_indices,
                ))
                batch_timestamp = frame.timestamp
                if len(batch) < SCAN_BATCH_SIZE:
                    continue
                submit_batch(batch, batch_timestamp)
                batch = []
            if batch and not (progress is not None and progress.is_cancel_requested()):
                submit_batch(batch, batch_timestamp)
            while pending and not (progress is not None and progress.is_cancel_requested()):
                collect_one()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if progress is not None and progress.is_cancel_requested():
            return []
        return list(candidates.get_top_n())

    def _scan_frame_stream(
        self,
        frames: Iterator[_ScanFrame],
        candidates: list[_ScanCandidate],
        *,
        progress: ScanProgress | None = None,
    ) -> _ScanRangeResult:
        started_at = time.perf_counter()
        best_score = -1.0
        top_matches = _TopScanMatches(SCAN_RESULT_LIMIT)
        scanned_frames = 0
        pending: dict[Future[list[float]], list[_ScanFrame]] = {}
        thread_budget = _AdaptiveScanThreadBudget(
            logger=self.logger,
            stage="scorer",
        )
        executor = ThreadPoolExecutor(max_workers=thread_budget.max_workers, thread_name_prefix="BogobotVideoScanScore")

        def collect_one() -> None:
            nonlocal best_score, scanned_frames

            if not pending:
                return
            done, _pending = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                batch = pending.pop(future)
                scores = future.result()
                for frame, score in zip(batch, scores, strict=True):
                    best_score = max(best_score, score)
                    top_matches.insert(_ScanRangeMatch(
                        score=score,
                        timestamp=frame.timestamp,
                        relative_seconds=frame.relative_seconds,
                        frame=frame.data,
                    ))
                scanned_frames += len(scores)
                if progress is not None and batch:
                    progress.current_seconds = batch[-1].timestamp
                    progress.scanned_frames = scanned_frames
                    progress.best_score = best_score if best_score >= 0 else None

        try:
            batch: list[_ScanFrame] = []
            for frame in frames:
                if progress is not None and progress.is_cancel_requested():
                    break
                batch.append(frame)
                if len(batch) < SCAN_BATCH_SIZE:
                    continue
                while len(pending) >= thread_budget.pending_limit:
                    collect_one()
                pending[executor.submit(self._frame_scores, [scan_frame.data for scan_frame in batch], candidates)] = batch
                batch = []
            if batch and not (progress is not None and progress.is_cancel_requested()):
                while len(pending) >= thread_budget.pending_limit:
                    collect_one()
                pending[executor.submit(self._frame_scores, [scan_frame.data for scan_frame in batch], candidates)] = batch
            while pending and not (progress is not None and progress.is_cancel_requested()):
                collect_one()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if progress is not None and progress.is_cancel_requested():
            return _ScanRangeResult(-1.0, scanned_frames, ())
        self.logger.debug(
            "Video archive scan pass: frames=%s time=%.3fs best=%.3f",
            scanned_frames,
            time.perf_counter() - started_at,
            best_score,
        )
        return _ScanRangeResult(best_score, scanned_frames, top_matches.get_top_n())

    def _candidate_batch(
        self,
        batch: list[tuple[bytes, tuple[int, ...]]],
        source: MatLike,
        scales: tuple[float, ...],
    ) -> tuple[_ScanCandidate, ...]:
        import cv2
        import numpy as np

        candidates = _TopScanCandidates(SCAN_MAX_CANDIDATES)
        source_h, source_w = source.shape[:2]
        frame_pixels = self.width * self.height
        template_cache: dict[int, MatLike | None] = {}
        rejected_too_small = 0
        rejected_too_large = 0
        rejected_geometry = 0
        rejected_flat = 0
        attempted_matches = 0

        def template_for_scale(scale_index: int) -> MatLike | None:
            nonlocal rejected_too_small, rejected_too_large, rejected_geometry, rejected_flat

            if scale_index in template_cache:
                return template_cache[scale_index]
            scale = scales[scale_index]
            width = int(round(source_w * scale))
            height = int(round(source_h * scale))
            if width < 8 or height < 8:
                rejected_too_small += 1
                template_cache[scale_index] = None
                return None
            if width * height > frame_pixels * 4:
                rejected_too_large += 1
                template_cache[scale_index] = None
                return None
            can_match_frame = width <= self.width and height <= self.height
            can_contain_frame = width >= self.width and height >= self.height
            if not can_match_frame and not can_contain_frame:
                rejected_geometry += 1
                template_cache[scale_index] = None
                return None
            interpolation = cv2.INTER_AREA if width <= source_w and height <= source_h else cv2.INTER_LINEAR
            resized = cv2.resize(source, (width, height), interpolation=interpolation)
            if math.isclose(float(resized.std()), 0.0, abs_tol=0.01):
                rejected_flat += 1
                template_cache[scale_index] = None
                return None
            template_cache[scale_index] = resized
            return resized

        for data, scale_indices in batch:
            frame = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
            for scale_index in scale_indices:
                template = template_for_scale(scale_index)
                if template is None:
                    continue
                attempted_matches += 1
                template_height, template_width = template.shape[:2]
                if template_width <= self.width and template_height <= self.height:
                    result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
                    _min_value, max_value, _min_loc, max_loc = cv2.minMaxLoc(result)
                    x, y = max_loc
                    width = template_width
                    height = template_height
                    key = (x // 2, y // 2, width, height)
                    candidate_template = template
                elif template_width >= self.width and template_height >= self.height:
                    result = cv2.matchTemplate(template, frame, cv2.TM_CCOEFF_NORMED)
                    _min_value, max_value, _min_loc, max_loc = cv2.minMaxLoc(result)
                    template_x, template_y = max_loc
                    x = 0
                    y = 0
                    width = self.width
                    height = self.height
                    key = (
                        template_x // 2,
                        template_y // 2,
                        template_width,
                        template_height,
                        self.width,
                        self.height,
                    )
                    candidate_template = template[
                        template_y:template_y + self.height,
                        template_x:template_x + self.width,
                    ]
                else:
                    continue
                score = float(max_value)
                sample_step = self._sample_step(width, height)
                sampled_template = candidate_template[::sample_step, ::sample_step].copy()
                template_float = sampled_template.astype(np.float32)
                template_centered = (template_float - template_float.mean()).ravel()
                candidates.insert(
                    key,
                    _ScanCandidate(
                        locator_key=key,
                        template_id=scale_index,
                        x=x,
                        y=y,
                        width=width,
                        height=height,
                        sample_step=sample_step,
                        template=sampled_template,
                        template_centered=template_centered,
                        template_int16=sampled_template.astype(np.int16),
                        locator_score=score,
                    )
                )
        result = candidates.get_top_n()
        if not result:
            self.logger.debug(
                "Video archive scan locator batch produced no candidates: "
                "frames=%s scale_refs=%s unique_scales=%s attempted=%s "
                "rejected_small=%s rejected_large=%s rejected_geometry=%s rejected_flat=%s",
                len(batch),
                sum(len(scale_indices) for _data, scale_indices in batch),
                len({scale_index for _data, scale_indices in batch for scale_index in scale_indices}),
                attempted_matches,
                rejected_too_small,
                rejected_too_large,
                rejected_geometry,
                rejected_flat,
            )
        return result

    def _frame_scores(
        self,
        batch: list[bytes],
        candidates: list[_ScanCandidate],
    ) -> list[float]:
        import numpy as np

        if not batch:
            return []

        frames = np.frombuffer(b"".join(batch), dtype=np.uint8).reshape((
            len(batch),
            self.height,
            self.width,
            3,
        ))
        best_scores = np.full((len(batch),), -1.0, dtype=np.float32)

        for candidate in candidates:
            roi = frames[
                :,
                candidate.y:candidate.y + candidate.height:candidate.sample_step,
                candidate.x:candidate.x + candidate.width:candidate.sample_step,
                :,
            ]
            if roi.shape[1:3] != candidate.template.shape[:2]:
                continue

            roi_float = roi.astype(np.float32)
            flattened_roi = roi_float.reshape((len(batch), -1))
            roi_means = flattened_roi.mean(axis=1, keepdims=True)
            roi_centered = flattened_roi - roi_means
            roi_std = flattened_roi.std(axis=1)
            valid = roi_std > 0.01
            if not np.any(valid):
                continue

            offset_adjusted_diff = np.abs(
                roi_centered - candidate.template_centered
            ).mean(axis=1)
            offset_adjusted_score = 1.0 - np.sqrt(np.minimum(1.0, offset_adjusted_diff / 255.0))
            raw_diff = np.abs(
                roi.astype(np.int16) - candidate.template_int16
            ).reshape((len(batch), -1)).mean(axis=1)
            raw_score = 1.0 - np.sqrt(np.minimum(1.0, raw_diff / 255.0))
            scores = offset_adjusted_score * 0.8 + raw_score * 0.2
            best_scores[valid] = np.maximum(best_scores[valid], scores[valid])

        return [float(score) for score in best_scores]

    def _sample_step(self, width: int, height: int) -> int:
        if width == self.width and height == self.height:
            return 1
        return max(1, min(width, height) // 96)


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
        self._frame_timeline_cache: OrderedDict[Path, tuple[int, int, FrameTimeline]] = OrderedDict()

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

    def recorded_ranges_for_interval(
        self,
        start_timestamp: float,
        end_timestamp: float,
    ) -> list[VideoArchiveRange]:
        ranges: list[VideoArchiveRange] = []
        for day in self._days_in_interval(start_timestamp, end_timestamp):
            bounds = self.recorded_bounds_for_day(day)
            if bounds is None:
                continue
            video_start_timestamp, video_end_timestamp = bounds
            range_start_timestamp = max(float(start_timestamp), video_start_timestamp)
            range_end_timestamp = min(float(end_timestamp), video_end_timestamp)
            if range_end_timestamp > range_start_timestamp:
                ranges.append(VideoArchiveRange(
                    day=day,
                    start_timestamp=range_start_timestamp,
                    end_timestamp=range_end_timestamp,
                ))
        return ranges

    def _days_in_interval(self, start_timestamp: float, end_timestamp: float) -> Iterator[str]:
        start_day = datetime.fromtimestamp(start_timestamp).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        end_day = datetime.fromtimestamp(end_timestamp).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        current_day = start_day
        while current_day <= end_day:
            yield current_day.strftime("%Y-%m-%d")
            current_day += timedelta(days=1)

    def recorded_days(self) -> list[str]:
        days: set[str] = set()
        for suffix in ("ts", self.final_format, "start"):
            for path in self.directory.glob(f"*.{suffix}"):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
                    days.add(path.stem)
        return sorted(days)

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
        decode_start_timestamp = self._decode_start_timestamp_for_target(
            video_path,
            start_timestamp,
            float(timestamp),
        )
        decode_start_seconds = (
            None
            if decode_start_timestamp is None else
            max(0.0, decode_start_timestamp - start_timestamp)
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with contextlib.suppress(OSError):
            output_path.unlink()
        command = self._extract_frame_command(
            video_path,
            output_path,
            relative_seconds,
            quality,
            decode_start_seconds=decode_start_seconds,
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

    def scan_for_image_interval(
        self,
        image: Image.Image,
        *,
        start_timestamp: float,
        end_timestamp: float,
        locator_interval_seconds: float = 30.0,
        progress: ScanProgress | None = None,
    ) -> VideoScanResult | None:
        if progress is not None:
            progress.set_stage("Preparing archive timeline")
        ranges = self._prepared_scan_ranges(start_timestamp, end_timestamp)
        return VideoScanner(
            width=self.width,
            height=self.height,
            logger=self.logger,
        ).scan_for_image(
            ranges,
            image,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            locator_interval_seconds=locator_interval_seconds,
            progress=progress,
        )

    def extract_frame_window(
        self,
        timestamp: float,
        *,
        before: int = 4,
        after: int = 4,
    ) -> tuple[VideoArchiveFrame, ...]:
        before = max(0, int(before))
        after = max(0, int(after))
        target_timestamp = float(timestamp)
        day = datetime.fromtimestamp(target_timestamp).strftime("%Y-%m-%d")
        video_path = self.video_path_for_day(day)
        if not video_path.exists():
            return ()

        bounds = self.recorded_bounds_for_day(day)
        if bounds is None:
            return ()
        archive_start_timestamp, _archive_end_timestamp = bounds
        timeline_pts = self._cached_frame_timeline_pts(video_path)
        if timeline_pts.size == 0:
            return ()

        target_relative_seconds = max(0.0, target_timestamp - archive_start_timestamp)
        target_frame_index = max(
            0,
            int(np.searchsorted(timeline_pts, target_relative_seconds, side="right")) - 1,
        )
        start_frame_index = max(0, target_frame_index - before)
        end_frame_index = min(len(timeline_pts) - 1, target_frame_index + after + 1)
        decode_frame_index = max(0, start_frame_index - self.keyint * 2)

        scan_range = _PreparedScanRange(
            day=day,
            video_path=video_path,
            archive_start_timestamp=archive_start_timestamp,
            start_timestamp=archive_start_timestamp + float(timeline_pts[start_frame_index]),
            end_timestamp=archive_start_timestamp + float(timeline_pts[end_frame_index]) + 0.001,
            decode_start_timestamp=archive_start_timestamp + float(timeline_pts[decode_frame_index]),
            timeline_pts=timeline_pts,
        )
        return VideoScanner(
            width=self.width,
            height=self.height,
            logger=self.logger,
        ).frame_window(
            [scan_range],
            target_timestamp=target_timestamp,
            before=before,
            after=after,
        )

    def _prepared_scan_ranges(
        self,
        start_timestamp: float,
        end_timestamp: float,
    ) -> list[_PreparedScanRange]:
        ranges: list[_PreparedScanRange] = []
        for archive_range in self.recorded_ranges_for_interval(start_timestamp, end_timestamp):
            video_path = self.video_path_for_day(archive_range.day)
            bounds = self.recorded_bounds_for_day(archive_range.day)
            if bounds is None:
                continue
            timeline_pts = self._cached_frame_timeline_pts(video_path)
            if timeline_pts.size == 0:
                continue
            decode_start_timestamp = self._decode_start_timestamp_for_target(
                video_path,
                bounds[0],
                archive_range.start_timestamp,
            )
            ranges.append(_PreparedScanRange(
                day=archive_range.day,
                video_path=video_path,
                archive_start_timestamp=bounds[0],
                start_timestamp=archive_range.start_timestamp,
                end_timestamp=archive_range.end_timestamp,
                decode_start_timestamp=decode_start_timestamp,
                timeline_pts=timeline_pts,
            ))
        return ranges

    def _decode_start_timestamp_for_target(
        self,
        video_path: Path,
        archive_start_timestamp: float,
        target_timestamp: float,
    ) -> float | None:
        timeline_pts = self._cached_frame_timeline_pts(video_path)
        if timeline_pts.size == 0:
            return None

        target_relative_seconds = max(0.0, target_timestamp - archive_start_timestamp)
        target_frame_index = max(
            0,
            int(np.searchsorted(timeline_pts, target_relative_seconds, side="right")) - 1,
        )
        decode_frame_index = max(0, target_frame_index - self.keyint * 2)
        return archive_start_timestamp + float(timeline_pts[decode_frame_index])

    def _cached_frame_timeline_pts(self, video_path: Path) -> FrameTimeline:
        try:
            stat = video_path.stat()
        except OSError:
            return np.array([], dtype=np.float64)

        cache_key = video_path.resolve()
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
        cache_value = self._frame_timeline_cache.get(cache_key)
        if cache_value is not None and cache_value[0] == size and cache_value[1] == mtime_ns:
            self._frame_timeline_cache.move_to_end(cache_key)
            return cache_value[2]

        timeline_pts = self._frame_timeline_pts(self._read_frame_pts(video_path))
        self._frame_timeline_cache[cache_key] = (size, mtime_ns, timeline_pts)
        self._frame_timeline_cache.move_to_end(cache_key)
        while len(self._frame_timeline_cache) > FRAME_TIMELINE_CACHE_SIZE:
            self._frame_timeline_cache.popitem(last=False)
        return timeline_pts

    def _extract_frame_command(
        self,
        video_path: Path,
        output_path: Path,
        relative_seconds: float,
        quality: int,
        *,
        decode_start_seconds: float | None = None,
    ) -> list[str]:
        if decode_start_seconds is None:
            preroll_seconds = min(max(10.0, float(self.keyint) * 3.0), relative_seconds)
            seek_seconds = max(0.0, relative_seconds - preroll_seconds)
        else:
            seek_seconds = max(0.0, min(relative_seconds, decode_start_seconds))
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
        if timeline_pts.size == 0:
            return None
        return float(timeline_pts[-1])

    def _frame_timeline_pts(self, frame_pts: list[tuple[int, float]]) -> FrameTimeline:
        if not frame_pts:
            return np.array([], dtype=np.float64)
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
        return np.asarray(timeline_pts, dtype=np.float64)

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
            "-fps_mode", "vfr",
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
