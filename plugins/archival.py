import asyncio
from collections.abc import Sequence
import contextlib
from dataclasses import dataclass, replace
import io
import json
import math
from pathlib import Path
import re
import tempfile
import time
from typing import Literal

import discord
from PIL import Image

from bogobot_core import BotCore
from utils import groups
from utils.pagination import PageSection, PaginatedView, SectionRead
from ai import AIParam, action
from utils.video_archival import ScanProgress, VideoArchiveFrame, VideoArchiver, VideoScanMatch, VideoScanResult


DEFAULT_ARCHIVE_PATH = "archive/monitor.bga"
DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0
DEFAULT_CHUNK_EVENT_LIMIT = 200
DEFAULT_VIDEO_ARCHIVE_DIR = "archive/video"
ARCHIVE_CLOSE_CUSTOM_ID = "bogobot:archive:close"
ARCHIVE_BUFFER_EVENT_LIMIT = 200
ARCHIVE_HEADER_SCAN_BLOCK_SIZE = 64 * 1024
ARCHIVE_SCAN_COOLDOWN_SECONDS = 30.0
ARCHIVE_IMAGES = 3
ARCHIVE_SCAN_MAX_RANGE_SECONDS = 24 * 60 * 60
DISCORD_TIMESTAMP_RE = re.compile(r"^<t:(?P<timestamp>-?\d+)(?::[tTdDfFRsS])?>$")
ARCHIVE_SCAN_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.IGNORECASE,
)
EPOCH_MILLISECONDS_THRESHOLD = 10_000_000_000


@dataclass(frozen=True)
class ArchiveEvent:
    timestamp: float
    dt_centiseconds: int
    display_dt_centiseconds: int
    value: int
    section_count: int
    chunk_start: int
    start: int
    end: int


@dataclass(frozen=True)
class ArchiveState:
    cursor: int
    snapshot_end: int


@dataclass(frozen=True)
class ArchiveRetrieveFrame:
    timestamp: int
    offset_frames: int
    media: str | discord.File


class ArchiveRetrieveView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        before_frames: Sequence[ArchiveRetrieveFrame],
        target_frame: ArchiveRetrieveFrame,
        after_frames: Sequence[ArchiveRetrieveFrame],
    ) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(
            discord.ui.TextDisplay("## Archive Frames"),
            discord.ui.Separator(),
        )

        for frame in before_frames:
            container.add_item(self._frame_section(frame))

        container.add_item(discord.ui.TextDisplay(
            self._active_frame_text(target_frame)
        ))
        container.add_item(discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                target_frame.media,
                description="Target archive frame",
            )
        ))

        for frame in after_frames:
            container.add_item(self._frame_section(frame))

        self.add_item(container)

    def _active_frame_text(self, frame: ArchiveRetrieveFrame) -> str:
        return f"### Archive Frame\n<t:{frame.timestamp}:S> `{frame.timestamp}`"

    def _frame_section(self, frame: ArchiveRetrieveFrame) -> discord.ui.Section:
        offset = f"{frame.offset_frames:+d} frame"
        if abs(frame.offset_frames) != 1:
            offset += "s"
        return discord.ui.Section(
            discord.ui.TextDisplay(
                f"`{offset}` <t:{frame.timestamp}:S> `{frame.timestamp}`"
            ),
            accessory=discord.ui.Thumbnail(
                frame.media,
                description=f"Archive frame {offset}",
            ),
        )


class ScanView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        scan_window: tuple[float, float] | None = None,
        input_media: str | discord.File,
        progress: ScanProgress,
        result: VideoScanResult | None = None,
        result_media: str | discord.File | None = None,
        preview_media: Sequence[tuple[VideoScanMatch, str | discord.File]] | None = None,
    ) -> None:
        super().__init__(timeout=None)

        progress_items: list[discord.ui.Item] = []
        if progress.done:
            progress_items.extend([
                discord.ui.TextDisplay("### Scan Progress"),
                discord.ui.TextDisplay(self._progress_text(progress)),
            ])
        else:
            cancel_button = discord.ui.Button(
                label="Cancel Scan" if not progress.cancel_requested else "Cancelling...",
                style=discord.ButtonStyle.secondary,
                disabled=progress.cancel_requested,
            )

            async def cancel_scan(interaction: discord.Interaction) -> None:
                progress.request_cancel()
                cancel_button.label = "Cancelling..."
                cancel_button.disabled = True
                await interaction.response.edit_message(view=self)

            cancel_button.callback = cancel_scan
            progress_items.append(discord.ui.Section(
                discord.ui.TextDisplay("### Scan Progress"),
                discord.ui.TextDisplay(self._progress_text(progress)),
                accessory=cancel_button,
            ))

        container = discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay("## Archive Scan"),
                *(
                    [discord.ui.TextDisplay(self._window_text(scan_window))]
                    if scan_window is not None else
                    []
                ),
                accessory=discord.ui.Thumbnail(
                    input_media,
                    description="Input image",
                ),
            ),
            discord.ui.Separator(),
            *progress_items,
        )

        if progress.done and result is not None:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(self._result_text(result)))
            if result_media is not None:
                container.add_item(discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        result_media,
                        description="Matched archive frame",
                    )
                ))
            for match, media in preview_media or []:
                container.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(self._match_text(match)),
                    accessory=discord.ui.Thumbnail(
                        media,
                        description="Archive match preview",
                    ),
                ))
        elif progress.done and progress.cancelled:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("Scan cancelled."))
        elif progress.done:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                "No matching archive frame found.\n"
                "-# Try a narrower `start`/`end` window."
            ))
        if progress.done:
            container.add_item(discord.ui.TextDisplay(self._completed_text(progress)))

        self.add_item(container)

    def _window_text(self, scan_window: tuple[float, float]) -> str:
        start_timestamp, end_timestamp = scan_window
        return f"-# From <t:{int(start_timestamp)}:S> to <t:{int(end_timestamp)}:S>"

    def _progress_text(self, progress: ScanProgress) -> str:
        lines = [
            f"Stage: `{progress.stage}`",
            f"Elapsed: `{self._duration(progress.elapsed_seconds)}`",
        ]
        bar = self._progress_bar(progress)
        if bar is not None:
            lines.append(f"`{bar}`")
        if progress.scanned_frames > 0:
            lines.append(f"Archive frames: `{progress.scanned_frames}`")
        if progress.best_score is not None:
            lines.append(f"Best score: `{progress.best_score:.3f}`")
        remaining = progress.estimated_remaining_seconds
        if remaining is not None and not progress.done:
            lines.append(f"Estimated time remaining: `{self._duration(remaining)}`")
        elif progress.done:
            lines.append("Estimated time remaining: `0s`")
        return "\n".join(lines)

    def _progress_bar(self, progress: ScanProgress) -> str | None:
        ratio = progress.progress_ratio
        if ratio is None:
            return None
        width = 16
        filled = min(width, max(0, round(ratio * width)))
        bar = "█" * filled + "░" * (width - filled)
        return f"[{bar}] {ratio * 100:.1f}%"

    def _result_text(self, result: VideoScanResult) -> str:
        epoch_ts = int(result.timestamp)
        return (
            f"`{epoch_ts}` <t:{epoch_ts}:S>\n"
            f"-# Matched archive frame with score `{result.score:.3f}`"
        )

    def _match_text(self, match: VideoScanMatch) -> str:
        epoch_ts = int(match.timestamp)
        return (
            f"`{epoch_ts}` <t:{epoch_ts}:S>\n"
            f"-# Archive frame with score `{match.score:.3f}`"
        )

    def _completed_text(self, progress: ScanProgress) -> str:
        completed_at = progress.completed_at if progress.completed_at is not None else time.time()
        return f"-# Scan completed at <t:{int(completed_at)}:S>"

    def _duration(self, seconds: float) -> str:
        seconds = max(0, int(seconds))
        minutes, second = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minute}m {second}s"
        if minutes:
            return f"{minutes}m {second}s"
        return f"{second}s"


def parse_archive_frame_time(value: str) -> float | None:
    value = value.strip()
    match = DISCORD_TIMESTAMP_RE.fullmatch(value)
    if match is not None:
        return float(match.group("timestamp"))

    try:
        timestamp = float(value)
    except ValueError:
        return None

    if abs(timestamp) >= EPOCH_MILLISECONDS_THRESHOLD:
        timestamp /= 1000
    return timestamp


def save_archive_retrieve_frame(
    frame: VideoArchiveFrame,
    output_path: Path,
    width: int,
    height: int,
) -> None:
    image = Image.frombytes("RGB", (width, height), frame.data)
    image.save(output_path, format="JPEG", quality=90)


def parse_archive_scan_duration(value: str) -> float | None:
    match = ARCHIVE_SCAN_DURATION_RE.fullmatch(value)
    if match is None:
        return None
    amount = float(match.group("value"))
    unit = match.group("unit").lower()
    if unit.startswith("s"):
        return amount
    if unit.startswith("m"):
        return amount * 60
    if unit.startswith("h"):
        return amount * 60 * 60
    if unit.startswith("d"):
        return amount * 24 * 60 * 60
    return None


def parse_archive_scan_absolute_time(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    if value.lower() == "now":
        return time.time()

    timestamp = parse_archive_frame_time(value)
    if timestamp is not None:
        return timestamp
    return None


def resolve_archive_scan_window(
    *,
    start: str | None,
    end: str | None,
    window: str,
) -> tuple[float, float] | str:
    window_seconds = parse_archive_scan_duration(window)
    if window_seconds is None or window_seconds <= 0:
        return "`window` must look like `30s`, `15min`, `12h`, or `1day`."
    if window_seconds > ARCHIVE_SCAN_MAX_RANGE_SECONDS:
        return "`window` cannot be longer than `24h`."

    start_timestamp = (
        parse_archive_scan_absolute_time(start)
        if start is not None else
        None
    )
    end_timestamp = (
        parse_archive_scan_absolute_time(end)
        if end is not None else
        None
    )
    if start is not None and start_timestamp is None:
        return "`start` must be `now`, epoch seconds, epoch milliseconds, `<t:...>`, or `<t:...:*>`."
    if end is not None and end_timestamp is None:
        return "`end` must be `now`, epoch seconds, epoch milliseconds, `<t:...>`, or `<t:...:*>`."

    if start_timestamp is None and end_timestamp is None:
        end_timestamp = time.time()
        start_timestamp = end_timestamp - window_seconds
    elif start_timestamp is None:
        assert end_timestamp is not None
        start_timestamp = end_timestamp - window_seconds
    elif end_timestamp is None:
        end_timestamp = start_timestamp + window_seconds

    if end_timestamp <= start_timestamp:
        return "`end` must be after `start`."
    if end_timestamp - start_timestamp > ARCHIVE_SCAN_MAX_RANGE_SECONDS:
        return "Archive scans cannot cover more than `24h`."
    return start_timestamp, end_timestamp

async def setup(bot: BotCore):
    manage = groups.manage(bot)
    archive_group = groups.archive(bot)
    archive_config_raw = bot.config.get("archive", {})
    archive_config = archive_config_raw if isinstance(archive_config_raw, dict) else {}

    def archive_setting(name: str, default):
        legacy_name = f"archive_{name}"
        if name in archive_config:
            return archive_config[name]
        if legacy_name in bot.config:
            return bot.config[legacy_name]
        return default

    archive_path = Path(archive_setting("path", DEFAULT_ARCHIVE_PATH))
    flush_interval = max(
        1.0,
        float(archive_setting(
            "flush_interval",
            DEFAULT_FLUSH_INTERVAL_SECONDS,
        )),
    )
    chunk_event_limit = max(
        1,
        int(archive_setting(
            "chunk_event_limit",
            DEFAULT_CHUNK_EVENT_LIMIT,
        )),
    )
    video_config_raw = archive_config.get("video", {})
    video_config = video_config_raw if isinstance(video_config_raw, dict) else {}

    def video_setting(name: str, default):
        legacy_name = f"archive_video_{name}"
        if name in video_config:
            return video_config[name]
        if legacy_name in bot.config:
            return bot.config[legacy_name]
        return default

    async def set_video_archive_enabled(enabled: bool) -> None:
        nonlocal archive_config, video_config

        archive_config_raw = bot.config.get("archive")
        if not isinstance(archive_config_raw, dict):
            archive_config_raw = {}
            bot.config["archive"] = archive_config_raw
        archive_config = archive_config_raw

        video_config_raw = archive_config.get("video")
        if not isinstance(video_config_raw, dict):
            video_config_raw = {}
            archive_config["video"] = video_config_raw
        video_config = video_config_raw

        video_config["enabled"] = enabled
        await bot.save_config()

    video_archiver = VideoArchiver(
        directory=Path(video_setting("dir", DEFAULT_VIDEO_ARCHIVE_DIR)),
        width=int(video_setting("width", 640)),
        height=int(video_setting("height", 360)),
        crf=int(video_setting("crf", 22)),
        preset=str(video_setting("preset", "fast")),
        tune=video_setting("tune", "animation"),
        keyint=int(video_setting("keyint", 60)),
        final_format=str(video_setting("final_format", "mkv")),
        logger=bot.logger.getChild("VideoArchive"),
    )

    pending_parts: list[str] = []
    archive_lock = asyncio.Lock()
    archive_scan_running = False
    archive_scan_last_finished_at = 0.0
    flush_task: asyncio.Task[None] | None = None
    last_event_time: float | None = None
    chunk_base_time: float | None = None
    chunk_elapsed_centiseconds = 0
    chunk_started = False
    chunk_event_count = 0

    async def delete_archive_message(interaction: discord.Interaction) -> None:
        if interaction.message is not None:
            try:
                await interaction.message.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "I cannot delete that archive message.",
                        ephemeral=True,
                    )
            return

        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Deleted archive message.",
                ephemeral=True,
            )

    class PersistentArchiveCloseView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(
            label="Close",
            style=discord.ButtonStyle.danger,
            custom_id=ARCHIVE_CLOSE_CUSTOM_ID,
        )
        async def close(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ) -> None:
            await delete_archive_message(interaction)

    bot.add_view(PersistentArchiveCloseView())

    def append_text(text: str) -> None:
        if archive_path.parent != Path("."):
            archive_path.parent.mkdir(parents=True, exist_ok=True)

        with archive_path.open("a", encoding="utf-8") as f:
            f.write(text)

    def file_size() -> int:
        if not archive_path.exists():
            return 0
        return archive_path.stat().st_size

    async def archive_snapshot_end() -> int:
        await flush_pending()
        try:
            return await asyncio.to_thread(file_size)
        except OSError as e:
            bot.logger.warning(f"Could not read monitor archive: {e}")
            return 0

    def find_chunk_start(end: int) -> int | None:
        if end <= 0 or not archive_path.exists():
            return None

        with archive_path.open("rb") as f:
            cursor = end
            carry = b""
            while cursor > 0:
                start = max(0, cursor - ARCHIVE_HEADER_SCAN_BLOCK_SIZE)
                f.seek(start)
                data = f.read(cursor - start) + carry

                candidates: list[int] = []
                if data.startswith(b"{"):
                    candidates.append(start)
                search_from = 0
                while True:
                    index = data.find(b"\n{", search_from)
                    if index < 0:
                        break
                    candidates.append(start + index + 1)
                    search_from = index + 2

                candidates = [candidate for candidate in candidates if candidate < end]
                if candidates:
                    return candidates[-1]

                carry = data[:1]
                cursor = start

        return None

    def find_next_chunk_start(start: int, snapshot_end: int) -> int | None:
        if start >= snapshot_end or not archive_path.exists():
            return None

        with archive_path.open("rb") as f:
            f.seek(start)
            offset = start
            carry = b""
            while offset < snapshot_end:
                raw = f.read(min(ARCHIVE_HEADER_SCAN_BLOCK_SIZE, snapshot_end - offset))
                if not raw:
                    return None
                data = carry + raw
                data_start = offset - len(carry)
                if offset == start and raw.startswith(b"{"):
                    return start
                index = data.find(b"\n{")
                if index >= 0:
                    return data_start + index + 1
                carry = raw[-1:]
                offset += len(raw)

        return None

    def read_chunk_before(end: int) -> tuple[int, int]:
        chunk_start = find_chunk_start(end)
        if chunk_start is None:
            return 0, 0

        next_chunk_start = find_next_chunk_start(chunk_start + 1, end)
        chunk_end = min(end, next_chunk_start) if next_chunk_start is not None else end
        return chunk_start, chunk_end

    def chunk_bounds_containing_or_after(start: int, snapshot_end: int) -> tuple[int, int]:
        if snapshot_end <= 0:
            return 0, 0

        search_end = min(max(start + 1, 1), snapshot_end)
        chunk_start = find_chunk_start(search_end)
        if chunk_start is None:
            chunk_start = find_next_chunk_start(0, snapshot_end)
        if chunk_start is None:
            return snapshot_end, snapshot_end

        next_chunk_start = find_next_chunk_start(chunk_start + 1, snapshot_end)
        chunk_end = next_chunk_start if next_chunk_start is not None else snapshot_end
        if chunk_end <= start:
            chunk_start = find_next_chunk_start(chunk_end, snapshot_end)
            if chunk_start is None:
                return snapshot_end, snapshot_end
            next_chunk_start = find_next_chunk_start(chunk_start + 1, snapshot_end)
            chunk_end = next_chunk_start if next_chunk_start is not None else snapshot_end

        return chunk_start, chunk_end

    def parse_chunk(start: int, end: int) -> list[ArchiveEvent]:
        if end <= start or not archive_path.exists():
            return []

        with archive_path.open("rb") as f:
            f.seek(start)
            raw = f.read(end - start)

        text = raw.decode("utf-8", errors="ignore")
        lines = text.splitlines(keepends=True)
        line_start = start
        current_time: float | None = None
        section_count = bot.SORT_SECTION_COUNT
        events: list[ArchiveEvent] = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    header = json.loads(stripped)
                    if int(header.get("version", 0)) != 1:
                        current_time = None
                    else:
                        current_time = float(header["base_epoch_time"])
                        section_count = int(header["section_count"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    current_time = None
                line_start += len(line)
                continue

            if current_time is None:
                line_start += len(line)
                continue

            token_start = 0
            while True:
                token_end = line.find(";", token_start)
                if token_end < 0:
                    break

                token = line[token_start:token_end]
                absolute_start = line_start + token_start
                absolute_end = line_start + token_end + 1
                token_start = token_end + 1
                if not token:
                    continue

                try:
                    raw_dt, raw_value = token.split(",", 1)
                    dt_centiseconds = int(raw_dt)
                    value = int(raw_value)
                except ValueError:
                    continue

                current_time += dt_centiseconds / 100
                events.append(ArchiveEvent(
                    timestamp=current_time,
                    dt_centiseconds=dt_centiseconds,
                    display_dt_centiseconds=dt_centiseconds,
                    value=value,
                    section_count=section_count,
                    chunk_start=start,
                    start=absolute_start,
                    end=absolute_end,
                ))

            line_start += len(line)

        return events

    def previous_event_before_chunk(chunk_start: int) -> ArchiveEvent | None:
        previous_chunk_start, previous_chunk_end = read_chunk_before(chunk_start)
        if previous_chunk_start == previous_chunk_end:
            return None

        previous_events = parse_chunk(previous_chunk_start, previous_chunk_end)
        if not previous_events:
            return None
        return previous_events[-1]

    def read_chunk_events(start: int, end: int) -> list[ArchiveEvent]:
        events = parse_chunk(start, end)
        if not events:
            return []

        previous_event = previous_event_before_chunk(start)
        if previous_event is None:
            return events

        first_event = events[0]
        display_dt_centiseconds = max(
            first_event.dt_centiseconds,
            int((first_event.timestamp - previous_event.timestamp) * 100),
        )
        return [
            replace(
                first_event,
                display_dt_centiseconds=display_dt_centiseconds,
            ),
            *events[1:],
        ]

    def read_events_before(
        end: int,
        snapshot_end: int,
        value_filter: int | None = None,
        limit: int = ARCHIVE_BUFFER_EVENT_LIMIT,
    ) -> list[ArchiveEvent]:
        events_descending: list[ArchiveEvent] = []
        cursor = min(end, snapshot_end)

        while (
            cursor > 0
            and len(events_descending) < limit
        ):
            chunk_start, chunk_end = read_chunk_before(cursor)
            if chunk_start == chunk_end:
                break

            chunk_events = read_chunk_events(chunk_start, chunk_end)
            eligible_events = [
                event for event in chunk_events
                if event.start < cursor
                and (value_filter is None or event.value == value_filter)
            ]
            for event in reversed(eligible_events):
                events_descending.append(event)
                if len(events_descending) >= limit:
                    break

            cursor = chunk_start

        return list(reversed(events_descending))

    def read_events_after(
        start: int,
        snapshot_end: int,
        value_filter: int | None = None,
        limit: int = ARCHIVE_BUFFER_EVENT_LIMIT,
    ) -> list[ArchiveEvent]:
        events: list[ArchiveEvent] = []
        cursor = max(0, start)

        while (
            cursor < snapshot_end
            and len(events) < limit
        ):
            chunk_start, chunk_end = chunk_bounds_containing_or_after(
                cursor,
                snapshot_end,
            )
            if chunk_start == chunk_end:
                break

            chunk_events = read_chunk_events(chunk_start, chunk_end)
            eligible_events = [
                event for event in chunk_events
                if event.start > cursor
                and (value_filter is None or event.value == value_filter)
            ]
            for event in eligible_events:
                events.append(event)
                if len(events) >= limit:
                    break

            cursor = chunk_end

        return events

    async def flush_pending() -> None:
        nonlocal pending_parts

        async with archive_lock:
            if not pending_parts:
                return

            text = "".join(pending_parts)
            try:
                await asyncio.to_thread(append_text, text)
            except OSError as e:
                bot.logger.warning(f"Could not save monitor archive: {e}")
                return

            pending_parts = []

    async def flush_loop() -> None:
        while True:
            await asyncio.sleep(flush_interval)
            await flush_pending()

    def archive_needs_separator() -> bool:
        if pending_parts:
            return True

        try:
            return archive_path.exists() and archive_path.stat().st_size > 0
        except OSError:
            return False

    def chunk_header(base_epoch_time: float) -> str:
        prefix = "\n" if archive_needs_separator() else ""
        return prefix + json.dumps(
            {
                "version": 1,
                "base_epoch_time": base_epoch_time,
                "section_count": bot.SORT_SECTION_COUNT,
            },
            separators=(",", ":"),
        ) + "\n"

    @bot.init_callback
    async def init():
        nonlocal flush_task

        if flush_task is None or flush_task.done():
            flush_task = asyncio.create_task(flush_loop())
        if bool(video_setting("enabled", False)):
            video_archiver.start()

    @bot.close_callback
    async def close():
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

        await flush_pending()
        video_archiver.close()

    @bot.new_frame_callback
    def archive_video_frame(img):
        video_archiver.record_frame(img)

    @manage.command(
        name="video_archive",
        description="Manage visual stream archive recording",
        capabilities=["archive.manage"],
        defer=False,
    )
    async def video_archive(
        interaction: discord.Interaction,
        action: Literal["start", "stop", "restart", "status"],
    ):
        if action == "start":
            video_archiver.start()
            await set_video_archive_enabled(True)
            status = video_archiver.status()
            await bot.discord.send(
                f"Video archive recording started. Current file: `{status.current_path or 'pending first frame'}`",
                response=True,
                ephemeral=True,
            )
            return

        if action == "stop":
            video_archiver.stop()
            await set_video_archive_enabled(False)
            await bot.discord.send(
                "Video archive recording stopped.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "restart":
            video_archiver.stop()
            video_archiver.start()
            await set_video_archive_enabled(True)
            status = video_archiver.status()
            await bot.discord.send(
                f"Video archive recording restarted. Current file: `{status.current_path or 'pending first frame'}`",
                response=True,
                ephemeral=True,
            )
            return

        status = video_archiver.status()
        last_frame = (
            f"<t:{int(status.last_frame_at)}:R>"
            if status.last_frame_at is not None else
            "never"
        )
        current_path = str(status.current_path) if status.current_path is not None else "pending first frame"
        await bot.discord.send(
            "\n".join((
                f"Enabled: `{status.enabled}`",
                f"FFmpeg running: `{status.running}`",
                f"Current file: `{current_path}`",
                f"Recorded frames: `{status.recorded_frames}`",
                f"Dropped frames: `{status.dropped_frames}`",
                f"Last frame: {last_frame}",
            )),
            response=True,
            ephemeral=True,
        )

    @bot.new_value_callback
    async def archive_value(
        new_values: list[tuple[bool, int]],
        value: int,
        timestamp: float,
    ):
        nonlocal chunk_base_time, chunk_elapsed_centiseconds
        nonlocal chunk_event_count, chunk_started, last_event_time

        if value < 0 or value > bot.SORT_SECTION_COUNT:
            return

        async with archive_lock:
            if (
                not chunk_started
                or chunk_event_count >= chunk_event_limit
                or (
                    last_event_time is not None
                    and timestamp < last_event_time
                )
            ):
                pending_parts.append(chunk_header(timestamp))
                chunk_base_time = timestamp
                chunk_elapsed_centiseconds = 0
                last_event_time = timestamp
                chunk_started = True
                chunk_event_count = 0

            if chunk_base_time is None:
                chunk_base_time = timestamp

            event_elapsed_centiseconds = max(
                chunk_elapsed_centiseconds,
                int((timestamp - chunk_base_time) * 100),
            )
            dt_centiseconds = event_elapsed_centiseconds - chunk_elapsed_centiseconds
            chunk_elapsed_centiseconds = event_elapsed_centiseconds
            last_event_time = timestamp
            pending_parts.append(f"{dt_centiseconds},{value};")
            chunk_event_count += 1

    class ArchiveView(PaginatedView[ArchiveState]):
        def __init__(
            self,
            *,
            initial_state: ArchiveState,
            value_filter: int | None = None,
        ):
            super().__init__(initial_state=initial_state, timeout=300)
            self.value_filter = value_filter
            self.cached_events: list[ArchiveEvent] = []
            self.message: BotCore._Discord.MessageHandle | None = None
            self.newer = discord.ui.Button(
                label="Newer",
                style=discord.ButtonStyle.secondary,
            )
            self.refresh = discord.ui.Button(
                label="Refresh",
                style=discord.ButtonStyle.primary,
            )
            self.older = discord.ui.Button(
                label="Older",
                style=discord.ButtonStyle.secondary,
            )
            self.close = discord.ui.Button(
                label="Close",
                style=discord.ButtonStyle.danger,
                custom_id=ARCHIVE_CLOSE_CUSTOM_ID,
            )
            self.newer.callback = self.newer_action
            self.refresh.callback = self.refresh_action
            self.older.callback = self.older_action
            self.close.callback = self.close_action
            self.controls = discord.ui.ActionRow(
                self.newer,
                self.refresh,
                self.older,
                self.close,
            )

        def page_allowed_mentions(self) -> discord.AllowedMentions | None:
            return discord.AllowedMentions.none()

        def empty_sections(self) -> list[PageSection]:
            body = "No archived monitor values yet."
            if self.value_filter is not None:
                body = f"No archived monitor values matching `{self.value_filter}`."
            return [
                PageSection(
                    title="Monitor Archive",
                    body=body,
                    accent_colour=discord.Color.dark_teal(),
                )
            ]

        def page_header(self, page) -> str | None:
            indexes = [
                section.index for section in page.sections
                if section.index is not None
            ]
            if not indexes:
                return "## Monitor Archive"
            return (
                "## Monitor Archive\n"
                f"Archive snapshot: `{self.state.snapshot_end}` bytes\n"
                f"{self.filter_line()}"
                f"Showing event starts `{min(indexes)}` to `{max(indexes)}`"
            )

        def filter_line(self) -> str:
            if self.value_filter is None:
                return ""
            return f"Value filter: `{self.value_filter}`\n"

        def add_controls(self) -> None:
            self.add_item(self.controls)

        def sync_controls(self) -> None:
            self.newer.disabled = self.previous_page_state is None
            self.older.disabled = self.next_page_state is None
            self.refresh.disabled = False

        def section_for(self, event: ArchiveEvent) -> PageSection:
            return PageSection(
                title="Monitor Archive",
                body=self.event_line(event),
                accent_colour=discord.Color.dark_teal(),
                index=event.start,
            )

        def event_line(self, event: ArchiveEvent) -> str:
            timestamp_ms = int(event.timestamp * 1000)
            timestamp = timestamp_ms // 1000
            seconds = timestamp % 60
            milliseconds = timestamp_ms % 1000
            dt_seconds = event.display_dt_centiseconds / 100
            v = str(event.value).rjust(len(str(event.section_count)))
            return (
                f"`@{event.start} "
                f"dt={f'{dt_seconds:.2f}s':<6} "
                f"value={v}/{event.section_count}` "
                f"<t:{timestamp}:s> `:{seconds:02d}.{milliseconds:03d}`"
            )

        def replace_cache(self, events: list[ArchiveEvent]) -> None:
            self.cached_events = sorted(events, key=lambda event: event.start)

        async def event_before(self, cursor: int, snapshot_end: int) -> ArchiveEvent | None:
            candidates = [
                event for event in self.cached_events
                if event.start < cursor
            ]
            if not candidates:
                self.replace_cache(await asyncio.to_thread(
                    read_events_before,
                    cursor,
                    snapshot_end,
                    self.value_filter,
                ))
                candidates = [
                    event for event in self.cached_events
                    if event.start < cursor
                ]
            if not candidates:
                return None
            return max(candidates, key=lambda event: event.start)

        async def event_after(self, cursor: int, snapshot_end: int) -> ArchiveEvent | None:
            candidates = [
                event for event in self.cached_events
                if event.start > cursor
            ]
            if not candidates:
                self.replace_cache(await asyncio.to_thread(
                    read_events_after,
                    cursor,
                    snapshot_end,
                    self.value_filter,
                ))
                candidates = [
                    event for event in self.cached_events
                    if event.start > cursor
                ]
            if not candidates:
                return None
            return min(candidates, key=lambda event: event.start)

        async def next_section(
            self,
            state: ArchiveState,
        ) -> SectionRead[ArchiveState] | None:
            event = await self.event_before(state.cursor, state.snapshot_end)
            if event is None:
                return None
            return SectionRead(
                section=self.section_for(event),
                state=ArchiveState(
                    cursor=event.start,
                    snapshot_end=state.snapshot_end,
                ),
            )

        async def previous_section(
            self,
            state: ArchiveState,
        ) -> SectionRead[ArchiveState] | None:
            event = await self.event_after(state.cursor, state.snapshot_end)
            if event is None:
                return None
            return SectionRead(
                section=self.section_for(event),
                state=ArchiveState(
                    cursor=event.start,
                    snapshot_end=state.snapshot_end,
                ),
            )

        async def newer_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.show_previous_page(interaction)

        async def refresh_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            snapshot_end = await archive_snapshot_end()
            self.replace_cache([])
            await self.set_state(
                interaction,
                ArchiveState(cursor=snapshot_end, snapshot_end=snapshot_end),
            )

        async def older_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.show_next_page(interaction)

        async def on_timeout(self) -> None:
            if self.current_page is None or self.message is None:
                return

            frozen_view = discord.ui.LayoutView(timeout=None)
            frozen_view.add_item(self.generate_container(self.current_page))
            await self.message.edit(view=frozen_view, **self.current_page.as_edit_kwargs())

        async def close_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await delete_archive_message(interaction)

    @archive_group.command(
        name="view",
        description="View archived monitor values",
        eph=False,
    )
    @action(
        "archive_view",
        "View the monitor archive, optionally filtered by correct-section value.",
        params={
            "value": AIParam("Only show archive entries with this correct-section value.", type=int | None, required=False),
        },
    )
    async def archive_view(
        interaction: discord.Interaction,
        value: int | None = None,
    ):
        if value is not None and (value < 0 or value > bot.SORT_SECTION_COUNT):
            await bot.discord.send(
                f"Archive value must be between 0 and {bot.SORT_SECTION_COUNT}.",
                response=True,
                ephemeral=True,
            )
            return

        snapshot_end = await archive_snapshot_end()
        view = ArchiveView(
            initial_state=ArchiveState(
                cursor=snapshot_end,
                snapshot_end=snapshot_end,
            ),
            value_filter=value,
        )
        page = await view.load()
        view.message = await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True,
            ephemeral=False,
        )

    @archive_group.command(
        name="retrieve",
        description="View a visual archive frame by timestamp",
        eph=False,
    )
    @action(
        "archive_retrieve",
        "Show a visual archive frame by epoch seconds, epoch milliseconds, or Discord timestamp.",
        params={
            "time": AIParam("Epoch seconds, epoch milliseconds, <t:...>, or <t:...:*>.", type=str),
        },
    )
    async def archive_retrieve(
        interaction: discord.Interaction,
        time: str,
    ):
        timestamp = parse_archive_frame_time(time)
        if timestamp is None:
            await bot.discord.send(
                "Time must be epoch seconds, epoch milliseconds, `<t:...>`, or `<t:...:*>`.",
                response=True,
                ephemeral=True,
            )
            return

        timestamp_seconds = int(timestamp)
        with tempfile.TemporaryDirectory(prefix="bogobot_archive_frame_") as temp_dir:
            temp_path = Path(temp_dir)
            archive_frames = await asyncio.to_thread(
                video_archiver.extract_frame_window,
                timestamp,
                before=4,
                after=4,
            )
            if not archive_frames:
                await bot.discord.send(
                    f"No archived frame found for <t:{timestamp_seconds}:S>.",
                    response=True,
                    ephemeral=True,
                )
                return

            target_index = next(
                (
                    index
                    for index, frame in enumerate(archive_frames)
                    if frame.timestamp >= timestamp
                ),
                len(archive_frames) - 1,
            )
            files: list[discord.File] = []
            retrieve_frames: list[ArchiveRetrieveFrame] = []
            for index, frame in enumerate(archive_frames):
                offset_frames = index - target_index
                frame_timestamp = int(frame.timestamp)
                output_path = temp_path / f"archive_frame_{timestamp_seconds}_{offset_frames:+d}f.jpg"
                await asyncio.to_thread(
                    save_archive_retrieve_frame,
                    frame,
                    output_path,
                    video_archiver.width,
                    video_archiver.height,
                )
                filename = f"archive_frame_{timestamp_seconds}_{offset_frames:+d}f.jpg"
                file = discord.File(output_path, filename=filename)
                files.append(file)
                retrieve_frames.append(ArchiveRetrieveFrame(
                    timestamp=frame_timestamp,
                    offset_frames=offset_frames,
                    media=file,
                ))

            target_frame = retrieve_frames[target_index]
            before_frames = [
                frame
                for frame in retrieve_frames
                if frame.offset_frames < 0
            ]
            after_frames = [
                frame
                for frame in retrieve_frames
                if frame.offset_frames > 0
            ]
            try:
                await bot.discord.send(
                    view=ArchiveRetrieveView(
                        before_frames=before_frames,
                        target_frame=target_frame,
                        after_frames=after_frames,
                    ),
                    files=files,
                    response=True,
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                for file in files:
                    file.close()

    @archive_group.command(
        name="scan",
        description="Find when an image appears in a visual archive recording",
        defer=False,
    )
    async def archive_scan(
        interaction: discord.Interaction,
        image: discord.Attachment,
        end: str | None = None,
        start: str | None = None,
        window: str = "12h",
    ):
        nonlocal archive_scan_last_finished_at, archive_scan_running

        if archive_scan_running:
            await bot.discord.send(
                "An archive scan is already running.",
                response=True,
                ephemeral=True,
            )
            return

        now = time.monotonic()
        cooldown_remaining = ARCHIVE_SCAN_COOLDOWN_SECONDS - (
            now - archive_scan_last_finished_at
        )
        if cooldown_remaining > 0:
            await bot.discord.send(
                f"Archive scanning is cooling down. Try again in `{math.ceil(cooldown_remaining)}` seconds.",
                response=True,
                ephemeral=True,
            )
            return

        resolved_window = resolve_archive_scan_window(
            start=start,
            end=end,
            window=window,
        )
        if isinstance(resolved_window, str):
            await bot.discord.send(
                resolved_window,
                response=True,
                ephemeral=True,
            )
            return

        requested_start_timestamp, requested_end_timestamp = resolved_window
        scan_ranges = video_archiver.recorded_ranges_for_interval(
            requested_start_timestamp,
            requested_end_timestamp,
        )
        if not scan_ranges:
            await bot.discord.send(
                "No visual archive recordings overlap that scan range.",
                response=True,
                ephemeral=True,
            )
            return

        scan_window: tuple[float, float] | None = (
            requested_start_timestamp,
            requested_end_timestamp,
        )
        archive_scan_running = True
        scan_started = False
        try:
            image_bytes = await image.read()
            target_image = Image.open(io.BytesIO(image_bytes))
            target_image.load()
        except Exception:
            archive_scan_running = False
            await bot.discord.send(
                "Could not read that image attachment.",
                response=True,
                ephemeral=True,
            )
            return

        progress = ScanProgress(
            stage="Queued",
            started_at=time.monotonic(),
            stage_started_at=time.monotonic(),
            window_start_seconds=0.0,
            window_end_seconds=0.0,
        )
        input_buffer = io.BytesIO(image_bytes)
        input_file = discord.File(
            input_buffer,
            filename=f"archive_scan_input_{image.filename or 'image'}",
        )
        scan_message = await bot.discord.send(
            view=ScanView(
                scan_window=scan_window,
                input_media=input_file,
                progress=progress,
            ),
            files=[input_file],
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        input_file.close()

        if scan_message is None or scan_message.message is None:
            archive_scan_running = False
            await bot.discord.send(
                "Could not create scan progress message.",
                response=True,
                ephemeral=True,
            )
            return

        input_attachment = (
            scan_message.message.attachments[0]
            if scan_message.message.attachments
            else None
        )
        input_media: str = input_attachment.url if input_attachment is not None else image.url
        scan_done = asyncio.Event()

        async def edit_progress_loop() -> None:
            while not scan_done.is_set():
                await asyncio.sleep(5)
                if scan_done.is_set():
                    break
                await scan_message.edit(
                    view=ScanView(
                        scan_window=scan_window,
                        input_media=input_media,
                        progress=progress,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        progress_task = asyncio.create_task(edit_progress_loop())

        async def delete_later(message: BotCore._Discord.MessageHandle) -> None:
            await asyncio.sleep(5)
            await message.delete()

        async def send_scan_completed_ping() -> None:
            message = await bot.discord.send(
                contents=f"{interaction.user.mention} Archive scan completed.",
                response=True,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
            if message is not None:
                await delete_later(message)

        try:
            scan_started = True
            result = await asyncio.to_thread(
                video_archiver.scan_for_image_interval,
                target_image,
                start_timestamp=requested_start_timestamp,
                end_timestamp=requested_end_timestamp,
                progress=progress,
            )
        finally:
            scan_done.set()
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
            archive_scan_running = False
            if scan_started:
                archive_scan_last_finished_at = time.monotonic()
        if result is None:
            if not progress.done:
                progress.done = True
                progress.completed_at = time.time()
            await scan_message.edit(
                view=ScanView(
                    scan_window=scan_window,
                    input_media=input_media,
                    progress=progress,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not progress.cancelled:
                await send_scan_completed_ping()
            return

        if progress.completed_at is None:
            progress.completed_at = time.time()
        result_files: list[tuple[VideoScanMatch, discord.File]] = []
        for index, match in enumerate(result.matches[:ARCHIVE_IMAGES]):
            if match.frame is None:
                continue
            epoch_ts = int(match.timestamp)
            frame_image = Image.frombytes(
                "RGB",
                (video_archiver.width, video_archiver.height),
                match.frame,
            )
            frame_buffer = io.BytesIO()
            frame_image.save(frame_buffer, format="JPEG", quality=90)
            frame_buffer.seek(0)
            result_files.append((match, discord.File(
                frame_buffer,
                filename=f"archive_scan_{index + 1}_{epoch_ts}.jpg",
            )))

        result_file = result_files[0][1] if result_files else None
        preview_media = result_files[1:]

        try:
            attachments: list[discord.Attachment | discord.File] = []
            if input_attachment is not None:
                attachments.append(input_attachment)
            attachments.extend(file for _match, file in result_files)
            await scan_message.edit(
                view=ScanView(
                    scan_window=scan_window,
                    input_media=input_media,
                    progress=progress,
                    result=result,
                    result_media=result_file,
                    preview_media=preview_media,
                ),
                attachments=attachments,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            for _match, result_file in result_files:
                result_file.close()
        await send_scan_completed_ping()
