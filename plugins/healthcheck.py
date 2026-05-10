from collections import deque
import contextlib
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

def ensure_memory_log_handler(
    logger: logging.Logger,
    *,
    capacity: int = 500,
) -> MemoryLogHandler:
    for handler in logger.handlers:
        if isinstance(handler, MemoryLogHandler):
            capacity = max(100, capacity)
            if handler.records.maxlen != capacity:
                handler.records = deque(handler.records, maxlen=capacity)
            return handler

    handler = MemoryLogHandler(max(100, capacity))
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s.%(msecs)03d %(levelname)-8s | %(name)-15s ] %(message)s",
        "%d %H:%M:%S",
    ))
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return handler

ROOT_LOG_HANDLER = ensure_memory_log_handler(logging.getLogger())

def format_logs(
    handler: MemoryLogHandler,
    *,
    start_from_last: int = 0,
    end_at_last: int = 30,
    start_from_first: int | None = None,
    end_at_first: int | None = None,
) -> str:
    records = list(handler.records)

    if start_from_first is not None or end_at_first is not None:
        start = start_from_first or 0
        end = end_at_first if end_at_first is not None else len(records)
        selected = records[start:end]
        label = f"Showing offsets `{start}` to `{end}` from first"
    else:
        newest_first = list(reversed(records))
        selected = list(reversed(newest_first[start_from_last:end_at_last]))
        label = f"Showing offsets `{start_from_last}` to `{end_at_last}` from latest"

    if selected:
        text = "\n".join(selected)
    else:
        text = "(no logs in that range)"

    if len(text) > 1800:
        text = "... truncated ...\n" + text[-1800:]

    return (
        f"Captured logs: `{len(records)}`\n"
        f"{label}:\n"
        f"```\n{text}\n```"
    )

class FallbackHealthcheckClient(discord.Client):
    def __init__(self, bot: "BotCore", handler: MemoryLogHandler):
        super().__init__(intents=discord.Intents.default())
        self.source_bot = bot
        self.handler = handler
        self.tree = app_commands.CommandTree(self)
        self.logger = bot.logger.getChild("Fallback")
        self._install_commands()

    def _authorized(self, interaction: discord.Interaction) -> bool:
        owner_id = self.source_bot.config.get("owner_uid")
        auth_list = self.source_bot.config.get("authorized_users", [])
        user_id = interaction.user.id
        return user_id == owner_id or user_id in auth_list

    def _install_commands(self) -> None:
        manage = app_commands.Group(name="manage")

        @manage.command(name="logs", description="Show recent bot logs")
        async def logs(
            interaction: discord.Interaction,
            start_from_last: int = 0,
            end_at_last: int = 30,
            start_from_first: int | None = None,
            end_at_first: int | None = None,
        ):
            if not self._authorized(interaction):
                await interaction.response.send_message("Unauthorized.", ephemeral=True)
                return

            error = validate_log_range(
                start_from_last=start_from_last,
                end_at_last=end_at_last,
                start_from_first=start_from_first,
                end_at_first=end_at_first,
            )
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return

            await interaction.response.send_message(
                format_logs(
                    self.handler,
                    start_from_last=start_from_last,
                    end_at_last=end_at_last,
                    start_from_first=start_from_first,
                    end_at_first=end_at_first,
                ),
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

    fallback = FallbackHealthcheckClient(bot, ROOT_LOG_HANDLER)
    async with fallback:
        await fallback.start(bot.config["bot_token"])

def validate_log_range(
    *,
    start_from_last: int = 0,
    end_at_last: int = 30,
    start_from_first: int | None = None,
    end_at_first: int | None = None,
) -> str | None:
    if (
        start_from_last < 0
        or end_at_last < 0
        or (start_from_first is not None and start_from_first < 0)
        or (end_at_first is not None and end_at_first < 0)
    ):
        return "Log offsets must be non-negative."

    if start_from_first is not None or end_at_first is not None:
        start = start_from_first or 0
        if end_at_first is not None and end_at_first <= start:
            return "`end_at_first` must be greater than `start_from_first`."
    elif end_at_last <= start_from_last:
        return "`end_at_last` must be greater than `start_from_last`."

    return None

async def setup(bot: "BotCore"):
    import groups

    manage = groups.manage(bot)
    capacity = max(100, int(bot.config.get("healthcheck_log_capacity", 500)))
    handler = ensure_memory_log_handler(logging.getLogger(), capacity=capacity)

    @manage.command(
        name="logs",
        description="Show recent bot logs",
    )
    async def logs(
        interaction: discord.Interaction,
        start_from_last: int = 0,
        end_at_last: int = 30,
        start_from_first: int | None = None,
        end_at_first: int | None = None,
    ):
        error = validate_log_range(
            start_from_last=start_from_last,
            end_at_last=end_at_last,
            start_from_first=start_from_first,
            end_at_first=end_at_first,
        )
        if error:
            await bot.discord.send(
                error,
                response=True,
            )
            return

        await bot.discord.send(
            format_logs(
                handler,
                start_from_last=start_from_last,
                end_at_last=end_at_last,
                start_from_first=start_from_first,
                end_at_first=end_at_first,
            ),
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
