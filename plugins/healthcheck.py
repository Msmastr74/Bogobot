from collections import deque
import contextlib
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from main import BotCore

class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int):
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            self.handleError(record)

MEMORY_LOG_HANDLER = MemoryLogHandler(500)
MEMORY_LOG_HANDLER.setLevel(logging.DEBUG)
LOG_EMBED_TEXT_LIMIT = 4000
MAX_LOG_MESSAGES = 5
DEFAULT_LOG_LENGTH = 30

@dataclass(frozen=True)
class LogRange:
    start: int
    end: int
    label: str
    truncate_mode: str
    msgs: int

def configure_memory_log_capacity(capacity: int) -> None:
    capacity = max(100, capacity)
    if MEMORY_LOG_HANDLER.records.maxlen != capacity:
        MEMORY_LOG_HANDLER.records = deque(MEMORY_LOG_HANDLER.records, maxlen=capacity)

def selected_log_lines(
    handler: MemoryLogHandler,
    *,
    log_range: LogRange,
) -> tuple[list[str], str, int, str]:
    records = list(handler.records)
    return (
        records[log_range.start:log_range.end],
        log_range.label,
        len(records),
        log_range.truncate_mode,
    )

def chunk_log_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    split_threshold = limit // 3

    def flush_current():
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

def logs_embeds(
    handler: MemoryLogHandler,
    *,
    log_range: LogRange,
) -> list[discord.Embed]:
    selected, label, total, truncate_mode = selected_log_lines(
        handler,
        log_range=log_range,
    )

    if selected:
        text = "\n".join(selected)
    else:
        text = "(no logs in that range)"

    all_chunks = chunk_log_text(text, LOG_EMBED_TEXT_LIMIT)
    truncated = len(all_chunks) > log_range.msgs

    if truncated and truncate_mode == "last":
        chunks = all_chunks[-log_range.msgs:]
        chunks[0] = "... truncated ...\n" + chunks[0]
    else:
        chunks = all_chunks[:log_range.msgs]
        if truncated:
            chunks[-1] = chunks[-1] + "\n... truncated ..."

    embeds = []

    for index, chunk in enumerate(chunks):
        title = "Logs" if len(chunks) == 1 else f"Logs {index + 1}/{len(chunks)}"
        prefix = f"Captured logs: `{total}`\n{label}:\n" if index == 0 else ""
        embeds.append(discord.Embed(
            title=title,
            description=f"{prefix}```\n{chunk}\n```",
        ))

    return embeds

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

            log_range, error = validate_log_range(
                len(self.handler.records),
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

            embeds = logs_embeds(
                self.handler,
                log_range=log_range,
            )
            for embed in embeds:
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

    if (
        (start_from_last is not None and start_from_last < 0)
        or (end_at_last is not None and end_at_last < 0)
        or (start_from_first is not None and start_from_first < 0)
        or (end_at_first is not None and end_at_first < 0)
    ):
        return None, "Log offsets must be non-negative."

    if start_from_first is not None and start_from_last is not None:
        return None, "Use only one start offset."

    if end_at_first is not None and end_at_last is not None:
        return None, "Use only one end offset."

    def clamp(position: int) -> int:
        return max(0, min(position, total))

    if (
        start_from_first is None
        and start_from_last is None
        and end_at_first is None
        and end_at_last is None
    ):
        start = max(0, total - DEFAULT_LOG_LENGTH)
        end = total
        truncate_mode = "last"
    else:
        start: int | None = None
        end: int | None = None
        truncate_mode = "last"

        if start_from_first is not None:
            start = clamp(start_from_first)
            truncate_mode = "first"
        elif start_from_last is not None:
            start = clamp(total - start_from_last)

        if end_at_first is not None:
            end = clamp(end_at_first)
            if start is None:
                start = end
                end = clamp(start + DEFAULT_LOG_LENGTH)
                truncate_mode = "first"
        elif end_at_last is not None:
            end = clamp(total - end_at_last)
            if start is None:
                start = clamp(end - DEFAULT_LOG_LENGTH)

        if start is None:
            start = max(0, total - DEFAULT_LOG_LENGTH)
        if end is None:
            if start_from_last is not None:
                end = start
                start = clamp(end - DEFAULT_LOG_LENGTH)
            else:
                end = clamp(start + DEFAULT_LOG_LENGTH)

        if start > end:
            start, end = end, start

    return LogRange(
        start=start,
        end=end,
        label=f"Showing log indexes `{start}` to `{end}`",
        truncate_mode=truncate_mode,
        msgs=msgs,
    ), None

async def setup(bot: "BotCore"):
    import groups

    manage = groups.manage(bot)
    configure_memory_log_capacity(int(bot.config.get("healthcheck_log_capacity", 500)))
    handler = MEMORY_LOG_HANDLER

    @manage.command(
        name="logs",
        description="Show recent bot logs",
    )
    async def logs(
        interaction: discord.Interaction,
        start_from_last: int | None = None,
        end_at_last: int | None = None,
        start_from_first: int | None = None,
        end_at_first: int | None = None,
        msgs: int = 1,
    ):
        log_range, error = validate_log_range(
            len(handler.records),
            start_from_last=start_from_last,
            end_at_last=end_at_last,
            start_from_first=start_from_first,
            end_at_first=end_at_first,
            msgs=msgs,
        )
        if error:
            await bot.discord.send(
                error,
                response=True,
            )
            return
        assert log_range is not None

        for embed in logs_embeds(handler, log_range=log_range):
            await bot.discord.send(
                embed=embed,
                response=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
