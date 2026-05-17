from collections import deque
import contextlib
from dataclasses import dataclass
import logging
import os
import sys
from typing import Literal, TYPE_CHECKING

import discord
from discord import app_commands
from utils.pagination import Page, PageSection, PaginatedView, SectionRead
from utils import groups
from utils.transformers import IntTransformer

if TYPE_CHECKING:
    from bogobot_core import BotCore


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
        end = len(records)
        return LogState(records=records, cursor=end, end=end)

    def start_state(self) -> LogState:
        return LogState(records=self.state.records, cursor=0, end=len(self.state.records))

    def end_state(self) -> LogState:
        end = len(self.state.records)
        return LogState(records=self.state.records, cursor=end, end=end)

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
        self.jump_edge.disabled = (
            self.next_page_state is None and
            self.previous_page_state is None
        )
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
        await self.set_state(interaction, self.fresh_state(), "previous")

    async def jump_edge_action(
        self,
        interaction: discord.Interaction,
    ) -> None:
        state = self.end_state() if self.state.cursor <= 0 else self.start_state()
        await self.set_state(interaction, state, direction=self.state.cursor <= 0 and "previous" or "next")

    async def older_action(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.show_previous_page(interaction)

# Admin commands installed on the normal BotCore client.
async def setup(bot: "BotCore"):
    manage = groups.manage(bot)
    MEMORY_LOG_HANDLER.configure_capacity(int(bot.config.get("log_capacity", 3000)))
    handler = MEMORY_LOG_HANDLER

    @manage.command(
        name="state",
        description="Show or change bot process state",
        perm_requirement=4,
    )
    async def state(
        interaction: discord.Interaction,
        action: Literal["stop", "restart"],
    ):
        if action == "restart":
            await bot.discord.send("Restarting...", response=True)
            bot.logger.critical("Restarting process by command request.")
            with contextlib.suppress(Exception):
                await bot.close()
            os.execv(sys.executable, [sys.executable, *sys.argv])
            return

        if action == "stop":
            await bot.discord.send("Stopping main bot...", response=True)
            bot.logger.critical("Shutting down process by command request.")
            with contextlib.suppress(Exception):
                await bot.close()
            sys.exit(0)
            return

    @manage.command(name="logs", description="Show recent bot logs or write a log message")
    async def logs(interaction: discord.Interaction, message: str | None = None, level: LogLevel = "INFO"):
        if message is not None:
            logger = bot.logger.getChild("UserLog")
            log_message = f"{interaction.user} ({interaction.user.id}): {message}"
            logger.log(log_level_mapping[level], log_message)
            records = handler.snapshot()
            end = len(records)
            
            view = SingleLogView(
                handler=handler,
                initial_state=LogState(
                    records=records,
                    cursor=end - 1,
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
        end = len(records)
        view = LogsView(
            handler=handler,
            initial_state=LogState(
                records=records,
                cursor=end,
                end=end,
            ),
            owner_id=interaction.user.id,
        )
        page = await view.load(direction="previous")
        await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True,
            ephemeral=True,
        )


    @manage.command(name="message", description="React to, pin, edit, reply to, or delete a message", perm_requirement=3)
    async def message(
        interaction: discord.Interaction,
        action: Literal['delete', 'react', 'unreact', 'pin', 'unpin', 'edit'],
        message_id: app_commands.Transform[int, IntTransformer],
        channel_id: app_commands.Transform[int, IntTransformer] | None = None,
        emoji: str | None = None,
        content: str | None = None
    ):
        channel_id = channel_id or interaction.channel_id
        if not channel_id:
            await bot.discord.send(
                contents="Failed to get channel id.",
                response=True
            )
            return
        messageable = bot.get_partial_messageable(channel_id)
        message = messageable.get_partial_message(message_id)
        try:
            if action == 'delete':
                # Make sure message is from the bot
                msg = await message.fetch()
                if msg.author != bot.user:
                    await bot.discord.send(
                        contents=f"You can only delete messages sent by {bot.user.mention if bot.user else 'this bot'}!",
                        response=True
                    )
                    return
                await message.delete()
            elif action == 'react':
                if emoji is None:
                    await bot.discord.send(
                        contents="You must provide an emoji to react with.",
                        response=True
                    )
                    return
                await message.add_reaction(emoji)
            elif action == 'unreact':
                if emoji is None:
                    await bot.discord.send(
                        contents="You must provide an emoji to remove.",
                        response=True
                    )
                    return
                if bot.user is None:
                    await bot.discord.send(
                        contents="Error: `bot.user` is None.",
                        response=True
                    )
                    return
                await message.remove_reaction(emoji, bot.user)
            elif action == 'pin':
                await message.pin()
            elif action == 'unpin':
                await message.unpin()
            elif action == 'edit':
                if content is None:
                    await bot.discord.send(
                        contents="You must provide content to edit to.",
                        response=True
                    )
                    return
                await message.edit(content=content, view=None)
            elif action == 'reply':
                if content is None:
                    await bot.discord.send(
                        contents="You must provide content to reply with.",
                        response=True
                    )
                    return
                await message.reply(content=content)
            
            await bot.discord.send(
                contents=f"Action `{action}` succeeded.",
                response=True
            )
        except discord.HTTPException as e:
            await bot.discord.send(
                contents=f"Action `{action}` failed with error:\n```{type(e).__qualname__}: {e}```",
                response=True
            )
