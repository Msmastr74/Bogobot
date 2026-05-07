import time
from typing import TYPE_CHECKING

import discord
from discord.ext import tasks

if TYPE_CHECKING:
    from main import BotCore


num_matrix: list[list[tuple[str, float]]] = [[] for _ in range(30)]

MONITOR_USAGE_TYPE = "monitor"


async def setup(bot: "BotCore"):
    def get_monitor_messages() -> dict[str, int]:
        """
        Stored in config as:

            monitor_messages: {
                "channel_id": message_id
            }

        Legacy migration:

            monitor_channels: {
                "channel_id": message_id
            }

        Channel/proxy tracking itself is handled separately by:

            bot.channels
        """

        messages = bot.config.get("monitor_messages")

        if not isinstance(messages, dict):
            messages = {}
            bot.config["monitor_messages"] = messages
            bot.save_config()
            return messages

        normalized: dict[str, int] = {}

        for channel_id_str, message_id in messages.items():
            try:
                normalized[str(int(channel_id_str))] = int(message_id)
            except (TypeError, ValueError):
                continue

        if normalized != messages:
            bot.config["monitor_messages"] = normalized
            bot.save_config()
            return normalized

        return messages

    def save_monitor_messages(monitor_messages: dict[str, int]) -> None:
        bot.config["monitor_messages"] = monitor_messages
        bot.save_config()

    async def ensure_monitor_proxy(channel_id: int):
        """
        Return an existing ChannelProxy if present.

        If missing, try to register this channel for monitor usage. This helps
        after restarts or config migrations where monitor_messages exists but
        the reusable channel system has not yet recorded monitor usage.
        """

        proxy = bot.channels.get(channel_id)

        if proxy is not None:
            return proxy

        return await bot.channels.add_channel(
            MONITOR_USAGE_TYPE,
            channel_id,
        )

    async def remove_monitor_channel(channel_id: int) -> None:
        await bot.channels.remove_channel(
            MONITOR_USAGE_TYPE,
            channel_id,
        )

    async def reconcile_monitor_channels() -> None:
        """
        Ensure every channel in monitor_messages has a ChannelProxy.

        Removes stale monitor entries when the channel is not available.
        """

        monitor_messages = get_monitor_messages()
        stale_channel_ids: list[str] = []

        for channel_id_str in list(monitor_messages.keys()):
            try:
                channel_id = int(channel_id_str)
            except ValueError:
                stale_channel_ids.append(channel_id_str)
                continue

            proxy = await ensure_monitor_proxy(channel_id)

            if proxy is None:
                stale_channel_ids.append(channel_id_str)

        if stale_channel_ids:
            monitor_messages = get_monitor_messages()

            for channel_id_str in stale_channel_ids:
                monitor_messages.pop(channel_id_str, None)

                try:
                    await remove_monitor_channel(int(channel_id_str))
                except ValueError:
                    pass

            save_monitor_messages(monitor_messages)

    @tasks.loop(seconds=1)
    async def monitor_loop():
        global num_matrix

        monitor_messages = get_monitor_messages()

        if not monitor_messages:
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

        stale_channel_ids: list[str] = []

        for channel_id_str, message_id in list(monitor_messages.items()):
            try:
                channel_id = int(channel_id_str)
                message_id = int(message_id)
            except (TypeError, ValueError):
                stale_channel_ids.append(channel_id_str)
                continue

            proxy = await ensure_monitor_proxy(channel_id)

            if proxy is None:
                stale_channel_ids.append(channel_id_str)
                continue

            embed = discord.Embed(
                title="Monitor",
                description=f"<t:{int(round(time.time()))}:T>\n{contents}",
            )
            embed.set_footer(text="Oldest → Newest [?? = Unknown]")

            await proxy.edit(
                message_id,
                embed=embed,
                wait=False,
            )

        if stale_channel_ids:
            monitor_messages = get_monitor_messages()

            for channel_id_str in stale_channel_ids:
                monitor_messages.pop(channel_id_str, None)

                try:
                    await remove_monitor_channel(int(channel_id_str))
                except ValueError:
                    pass

            save_monitor_messages(monitor_messages)

    @bot.setup.command(
        name="monitor",
        description="Begins monitoring sorted number counts from the stream in this channel",
    )
    async def monitor(interaction: discord.Interaction):
        monitor_messages = get_monitor_messages()

        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.messages.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        channel_id_str = str(channel_id)

        proxy = await bot.channels.add_channel(
            MONITOR_USAGE_TYPE,
            channel_id,
        )

        if proxy is None:
            await bot.discord.messages.send(
                "I cannot access this channel.",
                response=True,
            )
            return

        existing_message_id = monitor_messages.get(channel_id_str)

        # Replace any existing monitor message in this channel.
        if existing_message_id is not None:
            await proxy.delete(
                int(existing_message_id),
                wait=False,
            )

            monitor_messages.pop(channel_id_str, None)
            save_monitor_messages(monitor_messages)

        embed = discord.Embed(
            title="Monitor",
            description="Initializing...",
        )
        embed.set_footer(text="Oldest → Newest [?? = Unknown]")

        message = await proxy.send(
            embed=embed,
            wait=True,
        )

        if message is None:
            await remove_monitor_channel(channel_id)

            await bot.discord.messages.send(
                "Failed to create monitor message.",
                response=True,
            )
            return

        monitor_messages[channel_id_str] = message.id
        save_monitor_messages(monitor_messages)

        await bot.discord.messages.send(
            "Monitor system online in this channel.",
            response=True,
        )

    @bot.setup.command(
        name="stop",
        description="Stops the stream monitor in this channel",
    )
    async def stop_monitor(interaction: discord.Interaction):
        monitor_messages = get_monitor_messages()

        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.messages.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        channel_id_str = str(channel_id)
        message_id = monitor_messages.pop(channel_id_str, None)

        if message_id is None:
            await bot.discord.messages.send(
                "Monitor is not currently running in this channel.",
                response=True,
            )
            return

        save_monitor_messages(monitor_messages)

        proxy = bot.channels.get(channel_id)

        if proxy is not None:
            await proxy.delete(
                int(message_id),
                wait=False,
            )

        # Remove channel usage after delete is queued.
        await remove_monitor_channel(channel_id)

        await bot.discord.messages.send(
            "Monitor stopped in this channel.",
            response=True,
        )

    @bot.init_callback
    async def init():
        await bot.channels.wait_until_ready()
        await reconcile_monitor_channels()

        if not monitor_loop.is_running():
            monitor_loop.start()
