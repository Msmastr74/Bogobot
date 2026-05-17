import asyncio
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path

import discord

from bogobot_core import BotCore
from utils.pagination import PageSection, PaginatedView, SectionRead


DEFAULT_ARCHIVE_PATH = "archive/monitor.bga"
DEFAULT_FLUSH_INTERVAL_SECONDS = 60.0
ARCHIVE_CLOSE_CUSTOM_ID = "bogobot:archive:close"


@dataclass(frozen=True)
class ArchiveEvent:
    index: int
    timestamp: float
    dt_centiseconds: int
    value: int
    section_count: int


@dataclass(frozen=True)
class ArchiveState:
    events: list[ArchiveEvent]
    cursor: int


async def setup(bot: BotCore):
    archive_path = Path(bot.config.get("archive_path", DEFAULT_ARCHIVE_PATH))
    flush_interval = max(
        1.0,
        float(bot.config.get(
            "archive_flush_interval",
            DEFAULT_FLUSH_INTERVAL_SECONDS,
        )),
    )

    pending_parts: list[str] = []
    archive_lock = asyncio.Lock()
    flush_task: asyncio.Task[None] | None = None
    last_event_time: float | None = None
    chunk_started = False

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

    def load_archive_events_from_file() -> list[ArchiveEvent]:
        if not archive_path.exists():
            return []

        events: list[ArchiveEvent] = []
        current_time: float | None = None
        section_count = bot.SORT_SECTION_COUNT

        with archive_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if line.startswith("{"):
                    try:
                        header = json.loads(line)
                        if int(header.get("version", 0)) != 1:
                            current_time = None
                            continue
                        current_time = float(header["base_epoch_time"])
                        section_count = int(header["section_count"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        current_time = None
                    continue

                if current_time is None:
                    continue

                for token in line.split(";")[:-1]:
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
                        index=len(events),
                        timestamp=current_time,
                        dt_centiseconds=dt_centiseconds,
                        value=value,
                        section_count=section_count,
                    ))

        return events

    async def load_archive_events() -> list[ArchiveEvent]:
        await flush_pending()
        try:
            return await asyncio.to_thread(load_archive_events_from_file)
        except OSError as e:
            bot.logger.warning(f"Could not read monitor archive: {e}")
            return []

    async def flush_pending() -> None:
        nonlocal pending_parts

        async with archive_lock:
            if not pending_parts:
                return

            text = "".join(pending_parts)
            pending_parts = []

        try:
            await asyncio.to_thread(append_text, text)
        except OSError as e:
            async with archive_lock:
                pending_parts.insert(0, text)
            bot.logger.warning(f"Could not save monitor archive: {e}")

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
        nonlocal chunk_started, last_event_time

        if value < 0 or value > bot.SORT_SECTION_COUNT:
            return

        async with archive_lock:
            if (
                not chunk_started
                or (
                    last_event_time is not None
                    and timestamp < last_event_time
                )
            ):
                pending_parts.append(chunk_header(timestamp))
                last_event_time = timestamp
                chunk_started = True

            previous_time = last_event_time if last_event_time is not None else timestamp
            dt_centiseconds = max(0, round((timestamp - previous_time) * 100))
            last_event_time = timestamp
            pending_parts.append(f"{dt_centiseconds},{value};")

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
            indexes = [section.index for section in page.sections if section.index is not None]
            total = len(self.state.events)
            if not indexes:
                return f"## Monitor Archive\nArchived values: `{total}`"
            return (
                "## Monitor Archive\n"
                f"Archived values: `{total}`\n"
                f"Showing records `{min(indexes)}` to `{max(indexes)}`"
            )

        def add_controls(self) -> None:
            self.add_item(self.controls)

        def sync_controls(self) -> None:
            self.newer.disabled = self.next_page_state is None
            self.older.disabled = self.previous_page_state is None
            self.refresh.disabled = False

        def section_for(self, index: int) -> PageSection | None:
            if index < 0 or index >= len(self.state.events):
                return None

            event = self.state.events[index]
            timestamp = int(event.timestamp)
            stamp = dt.datetime.fromtimestamp(
                event.timestamp,
                tz=dt.timezone.utc,
            ).isoformat(timespec="milliseconds")
            return PageSection(
                title="Monitor Archive",
                body=(
                    f"`#{event.index}` <t:{timestamp}:T> "
                    f"`{stamp}` "
                    f"`dt={event.dt_centiseconds}cs` "
                    f"`value={event.value}/{event.section_count}`"
                ),
                accent_colour=discord.Color.dark_teal(),
                index=index,
            )

        async def next_section(
            self,
            state: ArchiveState,
        ) -> SectionRead[ArchiveState] | None:
            section = self.section_for(state.cursor)
            if section is None:
                return None
            return SectionRead(
                section=section,
                state=ArchiveState(
                    events=state.events,
                    cursor=state.cursor + 1,
                ),
            )

        async def previous_section(
            self,
            state: ArchiveState,
        ) -> SectionRead[ArchiveState] | None:
            previous_index = state.cursor - 1
            section = self.section_for(previous_index)
            if section is None:
                return None
            return SectionRead(
                section=section,
                state=ArchiveState(
                    events=state.events,
                    cursor=previous_index,
                ),
            )

        async def newer_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.show_next_page(interaction)

        async def refresh_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            events = await load_archive_events()
            await self.set_state(
                interaction,
                ArchiveState(events=events, cursor=len(events)),
                direction="previous",
            )

        async def older_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.show_previous_page(interaction)

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
        events = await load_archive_events()
        view = ArchiveView(
            initial_state=ArchiveState(
                events=events,
                cursor=len(events),
            ),
        )
        page = await view.load(direction="previous")
        await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True,
            ephemeral=False,
        )
