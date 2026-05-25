import asyncio
from dataclasses import dataclass, replace
import json
from pathlib import Path

import discord

from bogobot_core import BotCore
from utils.pagination import PageSection, PaginatedView, SectionRead
from utils.ai import AIParam, action


DEFAULT_ARCHIVE_PATH = "archive/monitor.bga"
DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0
DEFAULT_CHUNK_EVENT_LIMIT = 200
ARCHIVE_CLOSE_CUSTOM_ID = "bogobot:archive:close"
ARCHIVE_BUFFER_EVENT_LIMIT = 200
ARCHIVE_HEADER_SCAN_BLOCK_SIZE = 64 * 1024


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
            self.freeze = discord.ui.Button(
                label="Freeze",
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
            self.freeze.callback = self.freeze_action
            self.close.callback = self.close_action
            self.controls = discord.ui.ActionRow(
                self.newer,
                self.refresh,
                self.older,
                self.freeze,
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
            timestamp_ms = round(event.timestamp * 1000)
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

        async def freeze_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            if self.current_page is None:
                return

            frozen_view = discord.ui.LayoutView(timeout=None)
            header = self.page_header(self.current_page)
            if header:
                frozen_view.add_item(discord.ui.TextDisplay(header))
            frozen_view.add_item(discord.ui.Container(
                discord.ui.TextDisplay(self._page_body_text(self.current_page.sections)),
                accent_colour=self.page_accent_colour(self.current_page),
            ))
            await interaction.response.edit_message(
                view=frozen_view,
                **self.current_page.as_edit_kwargs(),
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
    @action(
        "archive",
        "View monitor archive.",
        params={
            "value": AIParam(type=int | None, required=False),
        },
    )
    async def archive(
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
        await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True,
            ephemeral=False,
        )
