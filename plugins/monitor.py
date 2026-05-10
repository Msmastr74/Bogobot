import time
from typing import Any, TYPE_CHECKING, cast

import discord
from discord.ext import tasks
from utils.tracker import Tracker

if TYPE_CHECKING:
    from main import BotCore


num_matrix: list[list[tuple[str, float]]] = [[] for _ in range(30)]


async def setup(bot: "BotCore"):
    import groups

    manage = groups.manage(bot)

    async def load_monitor_messages() -> dict[str, Any]:
        """
        Stored in config as:

            monitor_messages: {
                "channel_id": message_id
            }

        Legacy migration:

            monitor_channels: {
                "channel_id": message_id
            }

        Edits are coalesced by:

            bot.edits
        """

        messages = bot.config.get("monitor_messages")

        if not isinstance(messages, dict):
            return {}

        return messages

    async def save_monitor_messages(monitor_messages: dict[str, int]) -> None:
        bot.config["monitor_messages"] = monitor_messages
        await bot.save_config()

    async def normalize_monitor_message(channel_id_str: str, message_id: Any) -> tuple[int, int] | None:
        try:
            return int(channel_id_str), int(message_id)
        except (TypeError, ValueError):
            return None

    async def validate_monitor_message(channel_id: int, message_id: int) -> bool:
        return (await ensure_monitor_message(channel_id, message_id)) is not None

    monitor_messages = Tracker[int, int](
        load=load_monitor_messages,
        save=save_monitor_messages,
        normalize=normalize_monitor_message,
        validate=validate_monitor_message,
    )

    async def ensure_monitor_message(channel_id: int, message_id: int):
        """
        Return a coalescer for this monitor message if the channel is available.
        """

        existing = bot.edits.get(message_id)
        if existing is not None:
            return existing

        message = partial_message(channel_id, message_id)
        if message is None:
            return None

        return bot.edits.register(message)

    def partial_message(
        channel_id: int,
        message_id: int,
    ) -> discord.PartialMessage | None:
        channel = bot.get_channel(channel_id)

        if channel is None or not hasattr(channel, "get_partial_message"):
            return None

        return cast(Any, channel).get_partial_message(message_id)

    async def delete_monitor_message(channel_id: int, message_id: int) -> None:
        if await bot.edits.delete(message_id):
            return

        message = partial_message(channel_id, message_id)
        if message is None:
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    async def reconcile_monitor_channels() -> None:
        """
        Ensure every channel in monitor_messages can still be edited.

        Removes stale monitor entries when the channel is not available.
        """

        await monitor_messages.load()
        await monitor_messages.prune_stale()

    @tasks.loop(seconds=1)
    async def monitor_loop():
        global num_matrix

        await monitor_messages.prune_stale()
        stored_messages = await monitor_messages.items()

        if not stored_messages:
            return

        new_vars, is_new = await bot.info.get_best_shuffles()

        # If OCR didn't run because the file hasn't changed, do nothing.
        if not is_new:
            return

        # We have fresh data; update the matrix.
        num_matrix.pop(0)
        num_matrix.append([])

        for i, item in enumerate(new_vars):
            new_var, conf = item

            if conf <= 0:
                continue

            if new_var in ["0", "1", ""]:
                continue

            try:
                value = int(new_var)
            except ValueError:
                continue

            if value > 25:
                continue

            num_matrix[-i - 1].append((new_var.rjust(2, "0"), conf))

        num_array: list[str] = []

        for sublist in num_matrix:
            if not sublist:
                num_array.append("??")
                continue

            num_array.append(sublist[0][0])

        joined_nums = ".".join(num_array)
        contents = f"```\n{joined_nums}\n```"

        for channel_id, message_id in list(stored_messages.items()):
            coalescer = await ensure_monitor_message(channel_id, message_id)

            if coalescer is None:
                continue

            embed = discord.Embed(
                title="Monitor",
                description=f"<t:{int(round(time.time()))}:T>\n{contents}",
            )
            embed.set_footer(text="Oldest → Newest [?? = Unknown]")

            await coalescer.edit(
                embed=embed,
                wait=False,
            )

    @manage.command(
        name="monitor",
        description="Begins monitoring sorted number counts from the stream in this channel",
    )
    async def monitor(interaction: discord.Interaction):
        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        embed = discord.Embed(
            title="Monitor",
            description="Initializing...",
        )
        embed.set_footer(text="Oldest → Newest [?? = Unknown]")

        channel = interaction.channel or bot.get_channel(channel_id)

        if channel is None or not hasattr(channel, "send"):
            message = None
        else:
            try:
                message = await cast(Any, channel).send(embed=embed)
            except (discord.NotFound, discord.Forbidden):
                message = None

        if message is None:
            await bot.discord.send(
                "I cannot access this channel.",
                response=True,
            )
            return

        existing_message_id = await monitor_messages.get(channel_id)

        # Replace any existing monitor message in this channel.
        if existing_message_id is not None:
            await delete_monitor_message(channel_id, int(existing_message_id))

            await monitor_messages.remove(channel_id)

        bot.edits.register(message)
        await monitor_messages.set(channel_id, message.id)

        await bot.discord.send(
            "Monitor system online in this channel.",
            response=True,
        )

    @manage.command(
        name="stop_monitor",
        description="Stops the stream monitor in this channel",
    )
    async def stop_monitor(interaction: discord.Interaction):
        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        message_id = await monitor_messages.get(channel_id)

        if message_id is None:
            await bot.discord.send(
                "Monitor is not currently running in this channel.",
                response=True,
            )
            return

        await monitor_messages.remove(channel_id)

        await delete_monitor_message(channel_id, int(message_id))

        await bot.discord.send(
            "Monitor stopped in this channel.",
            response=True,
        )

    @bot.init_callback
    async def init():
        await reconcile_monitor_channels()

        if not monitor_loop.is_running():
            monitor_loop.start()
