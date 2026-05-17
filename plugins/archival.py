import asyncio
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path

import discord

from bogobot_core import BotCore
from utils.pagination import Page, PageSection, PaginatedView, SectionRead


DEFAULT_ARCHIVE_PATH = "archive/monitor.bga"
DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0
DEFAULT_CHUNK_EVENT_LIMIT = 200
ARCHIVE_CLOSE_CUSTOM_ID = "bogobot:archive:close"
ARCHIVE_PAGE_EVENT_LIMIT = 40
ARCHIVE_HEADER_SCAN_BLOCK_SIZE = 64 * 1024
ARCHIVE_WINDOW_CHUNK_LIMIT = 4


@dataclass(frozen=True)
class ArchiveEvent:
    timestamp: float
    dt_centiseconds: int
    value: int
    section_count: int
    start: int
    end: int


@dataclass(frozen=True)
class ArchiveState:
    start: int
    end: int
    snapshot_end: int


async def setup(bot: BotCore):
    archive_path = Path(bot.config.get("archive_path", DEFAULT_ARCHIVE_PATH))
    flush_interval = max(
        1.0,
        float(bot.config.get(
            "archive_flush_interval",
            DEFAULT_FLUSH_INTERVAL_SECONDS,
        )),
    )
    chunk_event_limit = max(
        1,
        int(bot.config.get(
            "archive_chunk_event_limit",
            DEFAULT_CHUNK_EVENT_LIMIT,
        )),
    )

    pending_parts: list[str] = []
    archive_lock = asyncio.Lock()
    flush_task: asyncio.Task[None] | None = None
    last_event_time: float | None = None
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
                    value=value,
                    section_count=section_count,
                    start=absolute_start,
                    end=absolute_end,
                ))

            line_start += len(line)

        return events

    def event_state(
        events: list[ArchiveEvent],
        snapshot_end: int,
        *,
        empty_position: int,
    ) -> ArchiveState:
        if not events:
            return ArchiveState(
                start=empty_position,
                end=empty_position,
                snapshot_end=snapshot_end,
            )
        return ArchiveState(
            start=events[0].start,
            end=events[-1].end,
            snapshot_end=snapshot_end,
        )

    def read_events_before(end: int, snapshot_end: int) -> list[ArchiveEvent]:
        events_descending: list[ArchiveEvent] = []
        cursor = min(end, snapshot_end)
        chunks_read = 0

        while (
            cursor > 0
            and len(events_descending) < ARCHIVE_PAGE_EVENT_LIMIT
            and chunks_read < ARCHIVE_WINDOW_CHUNK_LIMIT
        ):
            chunk_start, chunk_end = read_chunk_before(cursor)
            if chunk_start == chunk_end:
                break

            chunk_events = parse_chunk(chunk_start, chunk_end)
            eligible_events = [
                event for event in chunk_events
                if event.end <= cursor
            ]
            for event in reversed(eligible_events):
                events_descending.append(event)
                if len(events_descending) >= ARCHIVE_PAGE_EVENT_LIMIT:
                    break

            cursor = chunk_start
            chunks_read += 1

        return list(reversed(events_descending))

    def read_events_after(start: int, snapshot_end: int) -> list[ArchiveEvent]:
        events: list[ArchiveEvent] = []
        cursor = max(0, start)
        chunks_read = 0

        while (
            cursor < snapshot_end
            and len(events) < ARCHIVE_PAGE_EVENT_LIMIT
            and chunks_read < ARCHIVE_WINDOW_CHUNK_LIMIT
        ):
            chunk_start, chunk_end = chunk_bounds_containing_or_after(
                cursor,
                snapshot_end,
            )
            if chunk_start == chunk_end:
                break

            chunk_events = parse_chunk(chunk_start, chunk_end)
            eligible_events = [
                event for event in chunk_events
                if event.start >= cursor
            ]
            for event in eligible_events:
                events.append(event)
                if len(events) >= ARCHIVE_PAGE_EVENT_LIMIT:
                    break

            cursor = chunk_end
            chunks_read += 1

        return events

    def read_events_between(start: int, end: int, snapshot_end: int) -> list[ArchiveEvent]:
        events: list[ArchiveEvent] = []
        cursor = start
        chunks_read = 0

        while (
            cursor < end
            and cursor < snapshot_end
            and chunks_read < ARCHIVE_WINDOW_CHUNK_LIMIT
        ):
            chunk_start, chunk_end = chunk_bounds_containing_or_after(
                cursor,
                snapshot_end,
            )
            if chunk_start == chunk_end:
                break

            chunk_events = parse_chunk(chunk_start, min(chunk_end, snapshot_end))
            events.extend(
                event for event in chunk_events
                if start <= event.start and event.end <= end
            )

            cursor = chunk_end
            chunks_read += 1

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

    @bot.close_callback
    async def close():
        if flush_task is not None and not flush_task.done():
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

        await flush_pending()

    @bot.new_value_callback
    async def archive_value(value: int, timestamp: float):
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
                last_event_time = timestamp
                chunk_started = True
                chunk_event_count = 0

            previous_time = last_event_time if last_event_time is not None else timestamp
            dt_centiseconds = max(0, round((timestamp - previous_time) * 100))
            last_event_time = timestamp
            pending_parts.append(f"{dt_centiseconds},{value};")
            chunk_event_count += 1

    class ArchiveView(PaginatedView[ArchiveState]):
        def __init__(self, *, initial_state: ArchiveState):
            super().__init__(initial_state=initial_state, timeout=300)
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
            return [
                PageSection(
                    title="Monitor Archive",
                    body="No archived monitor values yet.",
                    accent_colour=discord.Color.dark_teal(),
                )
            ]

        def page_header(self, page) -> str | None:
            if self.state.start == self.state.end:
                return "## Monitor Archive"
            return (
                "## Monitor Archive\n"
                f"Archive snapshot: `{self.state.snapshot_end}` bytes\n"
                f"Showing event bytes `{self.state.start}` to `{self.state.end}`"
            )

        def add_controls(self) -> None:
            self.add_item(self.controls)

        def sync_controls(self) -> None:
            self.newer.disabled = self.state.end >= self.state.snapshot_end
            self.older.disabled = self.state.start <= 0
            self.refresh.disabled = False

        def section_for(self, events: list[ArchiveEvent]) -> PageSection:
            lines: list[str] = []
            for event in reversed(events):
                lines.append(self.event_line(event))
            body = "\n".join(lines) if lines else "No archived monitor values yet."
            return PageSection(
                title="Monitor Archive",
                body=body,
                accent_colour=discord.Color.dark_teal(),
            )

        def event_line(self, event: ArchiveEvent) -> str:
            timestamp = int(event.timestamp)
            stamp = dt.datetime.fromtimestamp(
                event.timestamp,
                tz=dt.timezone.utc,
            ).isoformat(timespec="milliseconds")
            return (
                f"`@{event.start}` <t:{timestamp}:T> "
                f"`{stamp}` "
                f"`dt={event.dt_centiseconds}cs` "
                f"`value={event.value}/{event.section_count}`"
            )

        async def load_archive_page(self, state: ArchiveState) -> Page:
            self.state = state
            if state.start == state.end:
                sections = self.empty_sections()
            else:
                events = await asyncio.to_thread(
                    read_events_between,
                    state.start,
                    state.end,
                    state.snapshot_end,
                )
                sections = [self.section_for(events)] if events else self.empty_sections()

            page = Page(
                sections=sections,
                allowed_mentions=self.page_allowed_mentions(),
            )
            self.current_page = page
            self._render_page(page)
            self.sync_controls()
            return page

        async def newest_state(self, snapshot_end: int) -> ArchiveState:
            events = await asyncio.to_thread(
                read_events_before,
                snapshot_end,
                snapshot_end,
            )
            return event_state(
                events,
                snapshot_end,
                empty_position=snapshot_end,
            )

        async def older_state(self) -> ArchiveState:
            events = await asyncio.to_thread(
                read_events_before,
                self.state.start,
                self.state.snapshot_end,
            )
            return event_state(
                events,
                self.state.snapshot_end,
                empty_position=0,
            )

        async def newer_state(self) -> ArchiveState:
            events = await asyncio.to_thread(
                read_events_after,
                self.state.end,
                self.state.snapshot_end,
            )
            return event_state(
                events,
                self.state.snapshot_end,
                empty_position=self.state.snapshot_end,
            )

        async def next_section(
            self,
            state: ArchiveState,
        ) -> SectionRead[ArchiveState] | None:
            return None

        async def previous_section(
            self,
            state: ArchiveState,
        ) -> SectionRead[ArchiveState] | None:
            return None

        async def newer_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            state = await self.newer_state()
            page = await self.load_archive_page(state)
            await interaction.response.edit_message(
                view=self,
                **page.as_edit_kwargs(),
            )

        async def refresh_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            snapshot_end = await archive_snapshot_end()
            page = await self.load_archive_page(
                await self.newest_state(snapshot_end)
            )
            await interaction.response.edit_message(
                view=self,
                **page.as_edit_kwargs(),
            )

        async def older_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            state = await self.older_state()
            page = await self.load_archive_page(state)
            await interaction.response.edit_message(
                view=self,
                **page.as_edit_kwargs(),
            )

        async def close_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await delete_archive_message(interaction)

    @bot.setup.command(
        name="archive",
        description="View archived monitor values",
        perm_requirement=0,
        eph=False,
    )
    async def archive(interaction: discord.Interaction):
        snapshot_end = await archive_snapshot_end()
        view = ArchiveView(
            initial_state=ArchiveState(
                start=snapshot_end,
                end=snapshot_end,
                snapshot_end=snapshot_end,
            ),
        )
        page = await view.load_archive_page(
            await view.newest_state(snapshot_end)
        )
        await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True,
            ephemeral=False,
        )
