import discord
from discord.ext import tasks
from datetime import datetime

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

num_matrix: list[list[tuple[str, float]]] = [[] for _ in range(30)]

async def setup(bot: "BotCore"):
    def get_monitor_channels() -> dict[str, int]:
        """
        Stored in config as:
            monitor_channels: { "channel_id": message_id }
        """
        channels = bot.config.get("monitor_channels")

        if channels is None:
            channels = {}
            bot.config["monitor_channels"] = channels
            bot.save_config()
            return channels

        if not isinstance(channels, dict):
            channels = {}
            bot.config["monitor_channels"] = channels
            bot.save_config()
            return channels

        return channels

    async def fetch_monitor_message(channel_id: int, message_id: int) -> discord.Message | None:
        """
        Fetch the Discord message for a monitor entry.

        Returns None when:
        - the channel no longer exists
        - the message no longer exists
        - the bot cannot access it
        """
        try:
            channel = bot.get_channel(channel_id)

            if channel is None:
                channel = await bot.fetch_channel(channel_id)

            if channel is None:
                return None

            if not hasattr(channel, "fetch_message"):
                return None

            return await channel.fetch_message(message_id)  # pyright: ignore

        except discord.NotFound:
            return None
        except discord.Forbidden:
            return None
        except Exception as e:
            print(f"Fetch monitor message error for {channel_id=} {message_id=}: {e}")
            return None

    @tasks.loop(seconds=0.5)
    async def monitor_loop():
        global num_matrix

        monitor_channels = get_monitor_channels()

        if not monitor_channels:
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

        contents = f"```\n{".".join(num_array)}\n```"
        author = datetime.now().strftime("[%H:%M:%S]")

        stale_channel_ids: list[str] = []

        for channel_id_str, message_id in list(monitor_channels.items()):
            try:
                channel_id = int(channel_id_str)
                message_id = int(message_id)
            except ValueError:
                stale_channel_ids.append(channel_id_str)
                continue

            message = await fetch_monitor_message(channel_id, message_id)

            if message is None:
                stale_channel_ids.append(channel_id_str)
                continue

            try:
                embed = message.embeds[0] if message.embeds else discord.Embed(
                    title="Monitor"
                )

                embed.description = contents
                embed.set_author(name=author)
                embed.set_footer(text="Oldest → Newest [?? = Unknown]")

                await message.edit(embed=embed)

            except discord.NotFound:
                stale_channel_ids.append(channel_id_str)
            except discord.Forbidden:
                stale_channel_ids.append(channel_id_str)
            except Exception as e:
                print(f"Edit Error for monitor channel {channel_id_str}: {e}")

        if stale_channel_ids:
            monitor_channels = get_monitor_channels()

            for channel_id_str in stale_channel_ids:
                monitor_channels.pop(channel_id_str, None)

            bot.config["monitor_channels"] = monitor_channels
            bot.save_config()

    @monitor_loop.before_loop
    async def before_monitor_loop():
        await bot.wait_until_ready()

    @bot.setup.command(
        name="monitor",
        description="Begins monitoring sorted number counts from the stream in this channel",
    )
    async def monitor(interaction: discord.Interaction):
        monitor_channels = get_monitor_channels()

        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.messages.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        channel_id_str = str(channel_id)

        existing_message_id = monitor_channels.get(channel_id_str)

        # Replace any existing monitor message in this channel.
        if existing_message_id is not None:
            try:
                old_message = await fetch_monitor_message(
                    channel_id,
                    int(existing_message_id),
                )

                if old_message is not None:
                    await old_message.delete()

            except Exception as e:
                print(f"Failed deleting old monitor message for {channel_id_str}: {e}")

            monitor_channels.pop(channel_id_str, None)
            bot.config["monitor_channels"] = monitor_channels
            bot.save_config()

        embed = await bot.discord.embeds.send(
            contents="Initializing...",
            title="Serial Number",
            footer="? = Unknown",
            response=False,
        )

        if embed is None:
            await bot.discord.messages.send(
                "Failed to create monitor message.",
                response=True,
            )
            return

        if embed.message_id is None:
            await bot.discord.messages.send(
                "Created monitor message, but could not read its message ID. Try running /monitor again.",
                response=True,
            )
            return

        monitor_channels[channel_id_str] = embed.message_id
        bot.config["monitor_channels"] = monitor_channels
        bot.save_config()

        await bot.discord.messages.send(
            "Monitor system online in this channel.",
            response=True,
        )

    @bot.setup.command(
        name="stop",
        description="Stops the stream monitor in this channel",
    )
    async def stop_monitor(interaction: discord.Interaction):
        monitor_channels = get_monitor_channels()

        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.messages.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        channel_id_str = str(channel_id)
        message_id = monitor_channels.pop(channel_id_str, None)

        if message_id is None:
            await bot.discord.messages.send(
                "Monitor is not currently running in this channel.",
                response=True,
            )
            return

        message = await fetch_monitor_message(channel_id, int(message_id))

        if message is not None:
            try:
                await message.delete()
            except Exception as e:
                print(f"Failed deleting monitor message for {channel_id_str}: {e}")

        bot.config["monitor_channels"] = monitor_channels
        bot.save_config()

        await bot.discord.messages.send(
            "Monitor stopped in this channel.",
            response=True,
        )

    if not monitor_loop.is_running():
        monitor_loop.start()
