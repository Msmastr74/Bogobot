from collections import deque
import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from main import BotCore


LOG_EMBED_TEXT_LIMIT = 3800
MAX_LOG_MESSAGES = 5
DEFAULT_LOG_LENGTH = 30

LOG_LEVEL_COLORS = {
    logging.DEBUG: discord.Color.light_grey(),
    logging.INFO: discord.Color.green(),
    logging.WARNING: discord.Color.gold(),
    logging.ERROR: discord.Color.red(),
    logging.CRITICAL: discord.Color.dark_red(),
}
LOG_LEVEL_COLOR_ORDER = sorted(LOG_LEVEL_COLORS, reverse=True)

LOG_LEVEL_EMOJIS = {
    logging.DEBUG: "🐞",
    logging.INFO: "ℹ️",
    logging.WARNING: "⚠️",
    logging.ERROR: "❗",
    logging.CRITICAL: "❌",
}
LOG_LEVEL_EMOJI_ORDER = sorted(LOG_LEVEL_EMOJIS, reverse=True)


@dataclass(frozen=True)
class LogEntry:
    created_at: int
    levelno: int
    logger_name: str
    message: str
    has_exception: bool


@dataclass(frozen=True)
class LogChunk:
    text: str
    levelno: int


@dataclass(frozen=True)
class LogRange:
    start: int
    end: int
    label: str
    truncate_mode: str
    msgs: int


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int):
        super().__init__()
        self.records: deque[LogEntry] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatter = self.formatter or logging.Formatter()
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{formatter.formatException(record.exc_info)}"
            if record.stack_info:
                message = f"{message}\n{record.stack_info}"
            self.records.append(LogEntry(
                created_at=int(record.created),
                levelno=record.levelno,
                logger_name=record.name,
                message=message,
                has_exception=record.exc_info is not None,
            ))
        except Exception:
            self.handleError(record)


MEMORY_LOG_HANDLER = MemoryLogHandler(500)
MEMORY_LOG_HANDLER.setLevel(logging.DEBUG)
MEMORY_LOG_HANDLER.setFormatter(logging.Formatter())


def configure_memory_log_capacity(capacity: int) -> None:
    capacity = max(100, capacity)
    if MEMORY_LOG_HANDLER.records.maxlen != capacity:
        MEMORY_LOG_HANDLER.records = deque(MEMORY_LOG_HANDLER.records, maxlen=capacity)


def emoji_for_level(levelno: int) -> str:
    if levelno <= logging.NOTSET:
        return ""
    for known_level in LOG_LEVEL_EMOJI_ORDER:
        if levelno >= known_level:
            return LOG_LEVEL_EMOJIS[known_level]
    return ""


def color_for_level(levelno: int) -> discord.Color:
    for known_level in LOG_LEVEL_COLOR_ORDER:
        if levelno >= known_level:
            return LOG_LEVEL_COLORS[known_level]
    return discord.Color.light_grey()


def format_log_entry(entry: LogEntry) -> str:
    header_parts = [f"<t:{entry.created_at}:T>", f"**{discord.utils.escape_markdown(entry.logger_name)}**"]
    if emoji := emoji_for_level(entry.levelno):
        header_parts.insert(0, emoji)
    if entry.has_exception:
        message = entry.message.replace("```", "`\u200b``")
        return f"{' '.join(header_parts)}\n```\n{message}\n```"
    return f"{' '.join(header_parts)}\n{discord.utils.escape_markdown(entry.message)}"


def chunk_log_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    split_threshold = limit // 3

    def flush_current() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

    for line in text.splitlines() or [""]:
        while line:
            separator_len = 1 if current else 0
            remaining = limit - current_len - separator_len
            if len(line) <= remaining:
                current.append(line)
                current_len += separator_len + len(line)
                break
            if len(line) > split_threshold and remaining > 0:
                current.append(line[:remaining])
                chunks.append("\n".join(current))
                current = []
                current_len = 0
                line = line[remaining:]
                continue
            flush_current()
            if len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        if line == "":
            current.append("")

    flush_current()
    return chunks


def chunk_log_entries(entries: list[LogEntry], limit: int) -> list[LogChunk]:
    chunks: list[LogChunk] = []
    current: list[str] = []
    current_len = 0
    current_levelno = logging.DEBUG

    def flush_current() -> None:
        nonlocal current, current_len, current_levelno
        if current:
            chunks.append(LogChunk(text="\n".join(current), levelno=current_levelno))
            current = []
            current_len = 0
            current_levelno = logging.DEBUG

    for entry in entries:
        entry_text = format_log_entry(entry)
        if len(entry_text) > limit:
            flush_current()
            chunks.extend(LogChunk(text=chunk, levelno=entry.levelno) for chunk in chunk_log_text(entry_text, limit))
            continue
        separator_len = 2 if current else 0
        if current_len + separator_len + len(entry_text) > limit:
            flush_current()
            separator_len = 0
        current.append(entry_text)
        current_len += separator_len + len(entry_text)
        current_levelno = max(current_levelno, entry.levelno)

    flush_current()
    return chunks


def selected_log_entries(handler: MemoryLogHandler, log_range: LogRange) -> tuple[list[LogEntry], int]:
    records = list(handler.records)
    return records[log_range.start:log_range.end], len(records)


def truncated_chunks(chunks: list[LogChunk], log_range: LogRange) -> list[LogChunk]:
    truncated = len(chunks) > log_range.msgs
    if not truncated:
        return chunks
    if log_range.truncate_mode == "last":
        kept = chunks[-log_range.msgs:]
        kept[0] = LogChunk(text="... truncated ...\n" + kept[0].text, levelno=kept[0].levelno)
        return kept
    kept = chunks[:log_range.msgs]
    kept[-1] = LogChunk(text=kept[-1].text + "\n... truncated ...", levelno=kept[-1].levelno)
    return kept


def logs_embeds(handler: MemoryLogHandler, *, log_range: LogRange) -> list[discord.Embed]:
    entries, total = selected_log_entries(handler, log_range)
    chunks = chunk_log_entries(entries, LOG_EMBED_TEXT_LIMIT) or [LogChunk("(no logs in that range)", logging.INFO)]
    chunks = truncated_chunks(chunks, log_range)
    embeds: list[discord.Embed] = []

    for index, chunk in enumerate(chunks):
        title = "Logs" if len(chunks) == 1 else f"Logs {index + 1}/{len(chunks)}"
        prefix = f"Captured logs: `{total}`\n{log_range.label}:\n\n" if index == 0 else ""
        embeds.append(discord.Embed(
            title=title,
            description=f"{prefix}{chunk.text}",
            color=color_for_level(chunk.levelno),
        ))

    return embeds


def has_negative_offset(*offsets: int | None) -> bool:
    return any(offset is not None and offset < 0 for offset in offsets)


def clamped(position: int, total: int) -> int:
    return max(0, min(position, total))


def default_log_range(total: int) -> tuple[int, int, str]:
    return max(0, total - DEFAULT_LOG_LENGTH), total, "last"


def explicit_log_range(
    total: int,
    *,
    start_from_last: int | None,
    end_at_last: int | None,
    start_from_first: int | None,
    end_at_first: int | None,
) -> tuple[int, int, str]:
    start: int | None = None
    end: int | None = None
    truncate_mode = "last"

    if start_from_first is not None:
        start = clamped(start_from_first, total)
        truncate_mode = "first"
    elif start_from_last is not None:
        start = clamped(total - start_from_last, total)

    if end_at_first is not None:
        end = clamped(end_at_first, total)
        if start is None:
            start = end
            end = clamped(start + DEFAULT_LOG_LENGTH, total)
            truncate_mode = "first"
    elif end_at_last is not None:
        end = clamped(total - end_at_last, total)
        if start is None:
            start = clamped(end - DEFAULT_LOG_LENGTH, total)

    if start is None:
        start = max(0, total - DEFAULT_LOG_LENGTH)
    if end is None:
        if start_from_last is not None:
            end = start
            start = clamped(end - DEFAULT_LOG_LENGTH, total)
        else:
            end = clamped(start + DEFAULT_LOG_LENGTH, total)
    if start > end:
        start, end = end, start

    return start, end, truncate_mode


def validate_log_range(
    total: int,
    *,
    start_from_last: int | None = None,
    end_at_last: int | None = None,
    start_from_first: int | None = None,
    end_at_first: int | None = None,
    msgs: int = 1,
) -> tuple[LogRange | None, str | None]:
    if not 1 <= msgs <= MAX_LOG_MESSAGES:
        return None, f"`msgs` must be between 1 and {MAX_LOG_MESSAGES}."
    if has_negative_offset(start_from_last, end_at_last, start_from_first, end_at_first):
        return None, "Log offsets must be non-negative."
    if start_from_first is not None and start_from_last is not None:
        return None, "Use only one start offset."
    if end_at_first is not None and end_at_last is not None:
        return None, "Use only one end offset."

    if start_from_first is None and start_from_last is None and end_at_first is None and end_at_last is None:
        start, end, truncate_mode = default_log_range(total)
    else:
        start, end, truncate_mode = explicit_log_range(
            total,
            start_from_last=start_from_last,
            end_at_last=end_at_last,
            start_from_first=start_from_first,
            end_at_first=end_at_first,
        )

    return LogRange(
        start=start,
        end=end,
        label=f"Showing log indexes `{start}` to `{end}`",
        truncate_mode=truncate_mode,
        msgs=msgs,
    ), None


def log_range_for(
    handler: MemoryLogHandler,
    *,
    start_from_last: int | None,
    end_at_last: int | None,
    start_from_first: int | None,
    end_at_first: int | None,
    msgs: int,
) -> tuple[LogRange | None, str | None]:
    return validate_log_range(
        len(handler.records),
        start_from_last=start_from_last,
        end_at_last=end_at_last,
        start_from_first=start_from_first,
        end_at_first=end_at_first,
        msgs=msgs,
    )


class FallbackHealthcheckClient(discord.Client):
    def __init__(self, bot: "BotCore", handler: MemoryLogHandler):
        super().__init__(intents=discord.Intents.default())
        self.source_bot = bot
        self.handler = handler
        self.tree = app_commands.CommandTree(self)
        self.logger = bot.logger.getChild("Fallback")
        self._install_commands()

    def _install_commands(self) -> None:
        manage = app_commands.Group(name="manage")

        @manage.command(name="logs", description="Show recent bot logs")
        async def logs(
            interaction: discord.Interaction,
            start_from_last: int | None = None,
            end_at_last: int | None = None,
            start_from_first: int | None = None,
            end_at_first: int | None = None,
            msgs: int = 1,
        ):
            if not self.source_bot.is_authorized(interaction.user.id, 1):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            log_range, error = log_range_for(
                self.handler,
                start_from_last=start_from_last,
                end_at_last=end_at_last,
                start_from_first=start_from_first,
                end_at_first=end_at_first,
                msgs=msgs,
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            assert log_range is not None
            for embed in logs_embeds(self.handler, log_range=log_range):
                await interaction.followup.send(
                    embed=embed,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        self.tree.add_command(manage)

    async def setup_hook(self):
        self.logger.critical("Fallback healthcheck client is running.")


async def start_fallback_healthcheck(bot: "BotCore") -> None:
    if not bot.config.get("fallback_healthcheck", True):
        return
    with contextlib.suppress(Exception):
        await bot.close()
    fallback = FallbackHealthcheckClient(bot, MEMORY_LOG_HANDLER)
    async with fallback:
        await fallback.start(bot.config["bot_token"])


async def setup(bot: "BotCore"):
    from utils import groups

    manage = groups.manage(bot)
    configure_memory_log_capacity(int(bot.config.get("healthcheck_log_capacity", 500)))
    handler = MEMORY_LOG_HANDLER

    @manage.command(name="logs", description="Show recent bot logs")
    async def logs(
        interaction: discord.Interaction,
        start_from_last: int | None = None,
        end_at_last: int | None = None,
        start_from_first: int | None = None,
        end_at_first: int | None = None,
        msgs: int = 1,
    ):
        log_range, error = log_range_for(
            handler,
            start_from_last=start_from_last,
            end_at_last=end_at_last,
            start_from_first=start_from_first,
            end_at_first=end_at_first,
            msgs=msgs,
        )
        if error:
            await bot.discord.send(error, response=True)
            return
        assert log_range is not None
        for embed in logs_embeds(handler, log_range=log_range):
            await bot.discord.send(
                embed=embed,
                response=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
