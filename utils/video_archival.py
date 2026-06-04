import contextlib
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class VideoArchiveStatus:
    enabled: bool
    running: bool
    current_path: Path | None
    recorded_frames: int
    dropped_frames: int
    last_frame_at: float | None


@dataclass(frozen=True)
class VideoScanResult:
    timestamp: float
    score: float
    relative_seconds: float
    scanned_frames: int
    frame: bytes | None


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
        preset: str = "fast",
        tune: str | None = "animation",
        keyint: int = 30,
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
        frame_pts = self._read_frame_pts(video_path)
        if not frame_pts:
            return False
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
        min_score: float = 0.86,
        locator_interval_seconds: float = 30.0,
        max_candidates: int = 12,
        requested_start_timestamp: float | None = None,
        requested_end_timestamp: float | None = None,
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
        templates = self._scan_templates(image)
        self.logger.debug(
            "Video archive scan templates: %s templates in %.3fs",
            len(templates),
            time.perf_counter() - templates_started_at,
        )
        if not templates:
            return None

        duration_started_at = time.perf_counter()
        duration = self._video_duration(video_path)
        self.logger.debug(
            "Video archive scan duration probe: %s in %.3fs",
            f"{duration:.3f}s" if duration is not None else "unavailable",
            time.perf_counter() - duration_started_at,
        )
        if duration is None:
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
        candidates = self._scan_candidates(
            video_path,
            templates,
            locator_interval_seconds=locator_interval_seconds,
            max_candidates=max_candidates,
            start_seconds=scan_start_seconds,
            duration_seconds=scan_end_seconds - scan_start_seconds,
        )
        self.logger.debug(
            "Video archive scan locator: %s candidates in %.3fs over %.3fs..%.3fs",
            len(candidates),
            time.perf_counter() - locator_started_at,
            scan_start_seconds,
            scan_end_seconds,
        )
        if not candidates:
            return None

        dense_started_at = time.perf_counter()
        result = self._scan_video_range(
            video_path,
            candidates,
            scan_start_seconds,
            scan_end_seconds - scan_start_seconds,
            frame_size,
        )
        best_score = result.score
        best_relative_seconds = result.relative_seconds
        best_frame = result.frame
        scanned_frames = result.scanned_frames
        dense_seconds = time.perf_counter() - dense_started_at
        self.logger.debug(
            "Video archive scan dense pass: %s frames in %.3fs; best %.3f at +%.3fs",
            scanned_frames,
            dense_seconds,
            best_score,
            best_relative_seconds,
        )

        if scanned_frames <= 0 or best_score < min_score:
            self.logger.debug(
                "Video archive scan rejected in %.3fs: frames=%s best=%.3f min=%.3f",
                time.perf_counter() - total_started_at,
                scanned_frames,
                best_score,
                min_score,
            )
            return None
        self.logger.debug(
            "Video archive scan matched in %.3fs: score=%.3f relative=%.3fs frames=%s",
            time.perf_counter() - total_started_at,
            best_score,
            best_relative_seconds,
            scanned_frames,
        )
        return VideoScanResult(
            timestamp=archive_start_timestamp + best_relative_seconds,
            score=best_score,
            relative_seconds=best_relative_seconds,
            scanned_frames=scanned_frames,
            frame=best_frame,
        )

    def _extract_frame_command(
        self,
        video_path: Path,
        output_path: Path,
        relative_seconds: float,
        quality: int,
    ) -> list[str]:
        preroll_seconds = min(max(1.0, float(self.keyint)), relative_seconds)
        seek_seconds = max(0.0, relative_seconds - preroll_seconds)
        decode_seconds = relative_seconds - seek_seconds
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{seek_seconds:.3f}",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-ss", f"{decode_seconds:.3f}",
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

        scales = [1.0, 0.75, 2 / 3, 0.5, 0.4, 1 / 3, 0.25]
        templates: list[MatLike] = []
        seen_sizes: set[tuple[int, int]] = set()
        for scale in scales:
            width = int(round(source_w * scale))
            height = int(round(source_h * scale))
            if width < 8 or height < 8 or width > self.width or height > self.height:
                continue
            if (width, height) in seen_sizes:
                continue
            seen_sizes.add((width, height))
            resized = cv2.resize(source, (width, height), interpolation=cv2.INTER_AREA)
            if math.isclose(float(resized.std()), 0.0, abs_tol=0.01):
                continue
            templates.append(resized)
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
    ) -> list[_ScanCandidate]:
        import cv2
        import numpy as np

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
        assert process.stdout is not None

        candidates: dict[tuple[int, int, int, int], _ScanCandidate] = {}
        try:
            while True:
                data = process.stdout.read(frame_size)
                if not data:
                    break
                if len(data) != frame_size:
                    break
                frame = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))
                for template in templates:
                    result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
                    _min_value, max_value, _min_loc, max_loc = cv2.minMaxLoc(result)
                    x, y = max_loc
                    height, width = template.shape[:2]
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
                        template=template[::sample_step, ::sample_step].astype(np.int16),
                        locator_score=score,
                    )
        finally:
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()

        if process.stderr is not None:
            stderr = process.stderr.read()
            process.stderr.close()
        else:
            stderr = b""
        process.wait(timeout=5)
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

    def _scan_video_range(
        self,
        video_path: Path,
        candidates: list[_ScanCandidate],
        start_seconds: float,
        duration_seconds: float | None,
        frame_size: int,
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
        assert process.stdout is not None
        assert process.stderr is not None

        best_score = -1.0
        best_relative_seconds = start_seconds
        best_frame: bytes | None = None
        best_frame_index = 0
        scanned_frames = 0
        stderr_chunks: list[bytes] = []

        def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = process.stderr.read(65536)
                if not chunk:
                    return
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(
            target=read_stderr,
            name="BogobotVideoScanStderr",
            daemon=True,
        )
        stderr_thread.start()

        try:
            while True:
                data = process.stdout.read(frame_size)
                if not data:
                    break
                if len(data) != frame_size:
                    break
                score = self._scan_frame_score(data, candidates)
                scanned_frames += 1
                if score > best_score:
                    best_score = score
                    best_frame = bytes(data)
                    best_frame_index = scanned_frames - 1
        finally:
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()

        process.wait(timeout=5)
        stderr_thread.join(timeout=5)
        stderr = b"".join(stderr_chunks)
        best_pts_time = self._scan_showinfo_time(stderr, best_frame_index)
        if best_pts_time is not None:
            best_relative_seconds = best_pts_time

        if process.returncode not in (0, None):
            message = stderr.decode(errors="replace").strip()
            if message:
                self.logger.warning(f"Video archive scan failed: {message}")
            return _ScanRangeResult(-1.0, start_seconds, scanned_frames, None)
        self.logger.debug(
            "Video archive scan pass: +%.3fs duration=%s frames=%s time=%.3fs best=%.3f at +%.3fs",
            start_seconds,
            f"{duration_seconds:.3f}s" if duration_seconds is not None else "end",
            scanned_frames,
            time.perf_counter() - started_at,
            best_score,
            best_relative_seconds,
        )
        return _ScanRangeResult(best_score, best_relative_seconds, scanned_frames, best_frame)

    def _scan_frame_score(self, data: bytes, candidates: list[_ScanCandidate]) -> float:
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
            mean_abs_diff = float(np.abs(roi.astype(np.int16) - candidate.template).mean())
            score = 1.0 - math.sqrt(mean_abs_diff / 255.0)
            best = max(best, score)
        return best

    def _scan_sample_step(self, width: int, height: int) -> int:
        return max(1, min(width, height) // 96)

    def _scan_showinfo_time(self, stderr: bytes, frame_index: int) -> float | None:
        text = stderr.decode(errors="replace")
        matches = list(SHOWINFO_RE.finditer(text))
        if 0 <= frame_index < len(matches):
            with contextlib.suppress(ValueError):
                return float(matches[frame_index].group("time"))
        return None

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
