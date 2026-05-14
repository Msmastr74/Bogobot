from collections import deque
import asyncio
import contextlib
from dataclasses import dataclass
import logging
import os
import sys
from typing import Literal, TYPE_CHECKING

import discord
from discord import app_commands
from utils.pagination import Page, PageSection, PaginatedView, SectionRead

if TYPE_CHECKING:
    from main import BotCore


RESTART_DELAY_SECONDS = 1.0
FALLBACK_CLIENT_REQUESTED = False
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
log_level_mapping: dict[LogLevel, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

@dataclass(frozen=True)
class LogEntry:
    counter: int
    created_at: int
    levelno: int
    logger_name: str
    message: str
    has_exception: bool


@dataclass(frozen=True)
class LogState:
    records: tuple[LogEntry, ...]
    cursor: int
    end: int


class MemoryLogHandler(logging.Handler):
    DEFAULT_LENGTH = 30

    LEVEL_COLORS = {
        logging.DEBUG: discord.Color.light_grey(),
        logging.INFO: discord.Color.green(),
        logging.WARNING: discord.Color.gold(),
        logging.ERROR: discord.Color.red(),
        logging.CRITICAL: discord.Color.dark_red(),
    }
    LEVEL_COLOR_ORDER = sorted(LEVEL_COLORS, reverse=True)

    LEVEL_EMOJIS = {
        logging.DEBUG: "🐞",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❗",
        logging.CRITICAL: "❌",
    }
    LEVEL_EMOJI_ORDER = sorted(LEVEL_EMOJIS, reverse=True)

    def __init__(self, capacity: int):
        super().__init__()
        self.records: deque[LogEntry] = deque(maxlen=capacity)
        self.next_counter = 1

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatter = self.formatter or logging.Formatter()
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{formatter.formatException(record.exc_info)}"
            if record.stack_info:
                message = f"{message}\n{record.stack_info}"
            self.records.append(LogEntry(
                counter=self.next_counter,
                created_at=int(record.created),
                levelno=record.levelno,
                logger_name=record.name,
                message=message,
                has_exception=record.exc_info is not None,
            ))
            self.next_counter += 1
        except Exception:
            self.handleError(record)

    def configure_capacity(self, capacity: int) -> None:
        capacity = max(100, capacity)
        if self.records.maxlen != capacity:
            self.records = deque(self.records, maxlen=capacity)

    def snapshot(self) -> tuple[LogEntry, ...]:
        self.acquire()
        try:
            return tuple(self.records)
        finally:
            self.release()

    def section_for(
        self,
        records: tuple[LogEntry, ...],
        index: int,
        *,
        end: int,
    ) -> PageSection | None:
        total = len(records)
        if index < 0 or index >= min(total, end):
            return None

        entry = records[index]
        return PageSection(
            title="Logs",
            body=self._format_entry(entry),
            accent_colour=self._color_for_level(entry.levelno),
            index=index,
        )

    def _emoji_for_level(self, levelno: int) -> str:
        if levelno <= logging.NOTSET:
            return ""
        for known_level in self.LEVEL_EMOJI_ORDER:
            if levelno >= known_level:
                return self.LEVEL_EMOJIS[known_level]
        return ""

    def _color_for_level(self, levelno: int) -> discord.Color:
        for known_level in self.LEVEL_COLOR_ORDER:
            if levelno >= known_level:
                return self.LEVEL_COLORS[known_level]
        return discord.Color.light_grey()

    def _format_entry(self, entry: LogEntry) -> str:
        header_parts = [
            f"`#{entry.counter}`",
        ]
        if emoji := self._emoji_for_level(entry.levelno):
            header_parts.append(emoji)
        header_parts.extend([
            f"<t:{entry.created_at}:T>",
            f"**{discord.utils.escape_markdown(entry.logger_name)}**",
        ])
        if entry.has_exception:
            message = entry.message.replace("```", "`\u200b``")
            return f"{' '.join(header_parts)}\n```\n{message}\n```"
        return f"{' '.join(header_parts)}\n{discord.utils.escape_markdown(entry.message)}"

    def _default_range(self, total: int) -> tuple[int, int]:
        return max(0, total - self.DEFAULT_LENGTH), total

MEMORY_LOG_HANDLER = MemoryLogHandler(500)
MEMORY_LOG_HANDLER.setLevel(logging.DEBUG)
MEMORY_LOG_HANDLER.setFormatter(logging.Formatter())

class SingleLogView(PaginatedView[LogState]):
    def __init__(
        self,
        *,
        handler: MemoryLogHandler,
        initial_state: LogState,
        owner_id: int,
    ):
        super().__init__(
            initial_state=initial_state,
            owner_id=owner_id,
            timeout=300,
        )
        self.handler = handler

    def page_allowed_mentions(self) -> discord.AllowedMentions | None:
        return discord.AllowedMentions.none()

    def empty_sections(self) -> list[PageSection]:
        return [
            PageSection(
                title="Logs",
                body="(no logs in that range)",
                accent_colour=discord.Color.green(),
            )
        ]

    def page_header(self, page: Page) -> str | None:
        return "## Logs"

class LogsView(PaginatedView[LogState]):
    def __init__(
        self,
        *,
        handler: MemoryLogHandler,
        initial_state: LogState,
        owner_id: int,
    ):
        super().__init__(
            initial_state=initial_state,
            owner_id=owner_id,
            timeout=300,
        )
        self.handler = handler
        self.newer = discord.ui.Button(
            label="Newer",
            style=discord.ButtonStyle.secondary,
        )
        self.jump_edge = discord.ui.Button(
            label="Start",
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
        self.newer.callback = self.newer_action
        self.jump_edge.callback = self.jump_edge_action
        self.refresh.callback = self.refresh_action
        self.older.callback = self.older_action
        self.controls = discord.ui.ActionRow(
            self.newer,
            self.jump_edge,
            self.refresh,
            self.older,
        )

    def page_allowed_mentions(self) -> discord.AllowedMentions | None:
        return discord.AllowedMentions.none()

    def empty_sections(self) -> list[PageSection]:
        return [
            PageSection(
                title="Logs",
                body="(no logs in that range)",
                accent_colour=discord.Color.green(),
            )
        ]

    def page_header(self, page: Page) -> str | None:
        indexes = [section.index for section in page.sections if section.index is not None]
        total = len(self.state.records)
        if not indexes:
            return f"## Logs\nCaptured logs: `{total}`"
        counters = [
            self.state.records[index].counter
            for index in indexes
            if index < len(self.state.records)
        ]
        counter_range = ""
        if counters:
            counter_range = f"\nLog counters: `#{min(counters)}` to `#{max(counters)}`"
        return (
            "## Logs\n"
            f"Captured logs: `{total}`\n"
            f"Showing snapshot indexes `{min(indexes)}` to `{max(indexes) + 1}`"
            f"{counter_range}"
        )

    def fresh_state(self) -> LogState:
        records = self.handler.snapshot()
        start, end = self.handler._default_range(len(records))
        return LogState(records=records, cursor=start, end=end)

    def start_state(self) -> LogState:
        return LogState(records=self.state.records, cursor=0, end=len(self.state.records))

    def end_state(self) -> LogState:
        start, end = self.handler._default_range(len(self.state.records))
        return LogState(records=self.state.records, cursor=start, end=end)

    async def next_section(self, state: LogState) -> SectionRead[LogState] | None:
        section = self.handler.section_for(state.records, state.cursor, end=state.end)
        if section is None:
            return None
        return SectionRead(
            section=section,
            state=LogState(
                records=state.records,
                cursor=state.cursor + 1,
                end=state.end,
            ),
        )

    async def previous_section(self, state: LogState) -> SectionRead[LogState] | None:
        previous_index = state.cursor - 1
        section = self.handler.section_for(state.records, previous_index, end=state.end)
        if section is None:
            return None
        return SectionRead(
            section=section,
            state=LogState(
                records=state.records,
                cursor=previous_index,
                end=state.end,
            ),
        )

    def sync_controls(self) -> None:
        self.newer.disabled = self.next_page_state is None
        self.older.disabled = self.previous_page_state is None
        self.refresh.disabled = False
        self.jump_edge.disabled = not self.state.records or self.start_state() == self.end_state()
        self.jump_edge.label = "End" if self.state.cursor <= 0 else "Start"

    def add_controls(self) -> None:
        self.add_item(self.controls)

    async def newer_action(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.show_next_page(interaction)

    async def refresh_action(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.set_state(interaction, self.fresh_state())

    async def jump_edge_action(
        self,
        interaction: discord.Interaction,
    ) -> None:
        state = self.end_state() if self.state.cursor <= 0 else self.start_state()
        await self.set_state(interaction, state)

    async def older_action(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.show_previous_page(interaction)

class FallbackClient(discord.Client):
    def __init__(self, bot: "BotCore", handler: MemoryLogHandler):
        super().__init__(intents=discord.Intents.default())
        self.source_bot = bot
        self.handler = handler
        self.tree = app_commands.CommandTree(self)
        self.logger = bot.logger.getChild("Fallback")

    @classmethod
    async def start_for(cls, bot: "BotCore") -> None:
        if not bot.config.get("fallback_client", True):
            return
        with contextlib.suppress(Exception):
            await bot.close()
        fallback = cls(bot, MEMORY_LOG_HANDLER)
        async with fallback:
            await fallback.start(bot.config["bot_token"])

    async def setup_hook(self):
        await self._install_commands()
        self.logger.critical("Fallback client is running.")

    async def _install_commands(self) -> None:
        await setup_fallback(self)

async def start_fallback_client(bot: "BotCore") -> None:
    await FallbackClient.start_for(bot)


def fallback_client_requested() -> bool:
    return FALLBACK_CLIENT_REQUESTED


def schedule_fallback_client(bot: "BotCore") -> None:
    global FALLBACK_CLIENT_REQUESTED

    FALLBACK_CLIENT_REQUESTED = True

    async def stop_for_fallback() -> None:
        await asyncio.sleep(RESTART_DELAY_SECONDS)
        bot.logger.critical("Stopping main bot for fallback client by command request.")
        with contextlib.suppress(Exception):
            await bot.close()

    asyncio.create_task(stop_for_fallback())

def schedule_restart(client: discord.Client, logger: logging.Logger) -> None:
    async def restart_process(client: discord.Client, logger: logging.Logger) -> None:
        await asyncio.sleep(RESTART_DELAY_SECONDS)
        logger.critical("Restarting process by command request.")
        with contextlib.suppress(Exception):
            await client.close()
        os.execv(sys.executable, [sys.executable, *sys.argv])
    asyncio.create_task(restart_process(client, logger))

# Admin commands installed on the normal BotCore client.
async def setup(bot: "BotCore"):
    from utils import groups

    manage = groups.manage(bot)
    MEMORY_LOG_HANDLER.configure_capacity(int(bot.config.get("log_capacity", 500)))
    handler = MEMORY_LOG_HANDLER

    @manage.command(
        name="state",
        description="Show or change bot process state",
        perm_requirement=4,
    )
    async def state(
        interaction: discord.Interaction,
        action: Literal["stop", "restart", "info"],
    ):
        if action == "restart":
            await bot.discord.send("Restarting...", response=True)
            schedule_restart(bot, bot.logger)
            return

        if action == "stop":
            if not bot.config.get("fallback_client", True):
                await bot.discord.send("Fallback client is disabled.", response=True)
                return
            await bot.discord.send("Stopping main bot and starting fallback client...", response=True)
            schedule_fallback_client(bot)
            return

        await bot.discord.send(
            "State: main bot is up; fallback client is not active.",
            response=True,
        )

    @manage.command(name="logs", description="Show recent bot logs or write a log message")
    async def logs(interaction: discord.Interaction, message: str | None = None, level: LogLevel = "INFO"):
        if message is not None:
            logger = bot.logger.getChild("UserLog")
            log_message = f"{interaction.user} ({interaction.user.id}): {message}"
            logger.log(log_level_mapping[level], log_message)
            records = handler.snapshot()
            start, end = handler._default_range(len(records))
            
            view = SingleLogView(
                handler=handler,
                initial_state=LogState(
                    records=records,
                    cursor=start,
                    end=end,
                ),
                owner_id=interaction.user.id,
            )
            page = await view.load()
            await bot.discord.send(
                **page.as_send_kwargs(),
                view=view,
                response=True,
                ephemeral=True,
            )
            return

        records = handler.snapshot()
        start, end = handler._default_range(len(records))
        view = LogsView(
            handler=handler,
            initial_state=LogState(
                records=records,
                cursor=start,
                end=end,
            ),
            owner_id=interaction.user.id,
        )
        page = await view.load()
        await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True,
            ephemeral=True,
        )

async def setup_fallback(client: FallbackClient):
    manage = app_commands.Group(
        name="manage", description="Bot management commands"
    )

    @manage.command(name="state", description="Show or change bot process state")
    async def state(
        interaction: discord.Interaction,
        action: Literal["stop", "restart", "info"],
    ):
        if not client.source_bot.is_authorized(interaction.user.id, 1):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return

        if action == "restart":
            await interaction.response.send_message("Restarting...", ephemeral=True)
            schedule_restart(client, client.logger)
            return

        if action == "stop":
            await interaction.response.send_message(
                "Fallback client is already active.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "State: fallback client is active; main bot is not up.",
            ephemeral=True,
        )

    @manage.command(name="logs", description="Show recent bot logs or write a log message")
    async def logs(interaction: discord.Interaction, message: str | None = None, level: LogLevel = "INFO"):
        if not client.source_bot.is_authorized(interaction.user.id, 1):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if message is not None:
            logger = client.logger.getChild("UserLog")
            log_message = f"{interaction.user} ({interaction.user.id}): {message}"
            logger.log(log_level_mapping[level], log_message)
            records = client.handler.snapshot()
            start, end = client.handler._default_range(len(records))
            
            view = SingleLogView(
                handler=client.handler,
                initial_state=LogState(
                    records=records,
                    cursor=start,
                    end=end,
                ),
                owner_id=interaction.user.id,
            )
            page = await view.load()
            await interaction.followup.send(
                **page.as_send_kwargs(),
                view=view,
                ephemeral=True,
            )
            return
        records = client.handler.snapshot()
        start, end = client.handler._default_range(len(records))
        view = LogsView(
            handler=client.handler,
            initial_state=LogState(
                records=records,
                cursor=start,
                end=end,
            ),
            owner_id=interaction.user.id,
        )
        page = await view.load()
        await interaction.followup.send(
            **page.as_send_kwargs(),
            view=view,
            ephemeral=True,
        )

    client.tree.add_command(manage)
