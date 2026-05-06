import discord
from discord.ext import tasks
import time

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

    async def get_monitor_partial_message(
        channel_id: int,
        message_id: int,
    ) -> discord.PartialMessage | None:
        """
        Return a partial message handle without fetching the full message.

        Returns None when:
        - the channel no longer exists
        - the bot cannot access the channel
        - the channel does not support partial messages
        """
        try:
            channel = bot.get_channel(channel_id)

            if channel is None:
                channel = await bot.fetch_channel(channel_id)

            if channel is None:
                return None

            if not hasattr(channel, "get_partial_message"):
                return None

            return channel.get_partial_message(message_id)  # pyright: ignore

        except discord.NotFound:
            return None
        except discord.Forbidden:
            return None
        except Exception as e:
            bot.logger.warning(f"Partial message error for {channel_id=} {message_id=}: {e}")
            return None

    def save_monitor_channels(monitor_channels: dict[str, int]) -> None:
        bot.config["monitor_channels"] = monitor_channels
        bot.save_config()

    @tasks.loop(seconds=1)
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

        joined_nums = ".".join(num_array)
        contents = f"```\n{joined_nums}\n```"

        stale_channel_ids: list[str] = []

        for channel_id_str, message_id in list(monitor_channels.items()):
            try:
                channel_id = int(channel_id_str)
                message_id = int(message_id)
            except ValueError:
                stale_channel_ids.append(channel_id_str)
                continue

            message = await get_monitor_partial_message(channel_id, message_id)

            if message is None:
                stale_channel_ids.append(channel_id_str)
                continue

            try:
                embed = discord.Embed(
                    title="Monitor",
                    description=f"<t:{int(round(time.time()))}:T>\n{contents}",
                )
                embed.set_footer(text="Oldest → Newest [?? = Unknown]")

                await message.edit(embed=embed)

            except discord.NotFound:
                stale_channel_ids.append(channel_id_str)
            except discord.Forbidden:
                stale_channel_ids.append(channel_id_str)
            except Exception as e:
                bot.logger.warning(f"Edit Error for monitor channel {channel_id_str}: {e}")

        if stale_channel_ids:
            monitor_channels = get_monitor_channels()

            for channel_id_str in stale_channel_ids:
                monitor_channels.pop(channel_id_str, None)

            save_monitor_channels(monitor_channels)

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
                old_message = await get_monitor_partial_message(
                    channel_id,
                    int(existing_message_id),
                )

                if old_message is not None:
                    await old_message.delete()

            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass
            except Exception as e:
                bot.logger.warning(f"Failed deleting old monitor message for {channel_id_str}: {e}")

            monitor_channels.pop(channel_id_str, None)
            save_monitor_channels(monitor_channels)

        embed = await bot.discord.embeds.send(
            contents="Initializing...",
            title="Monitor",
            footer="Oldest → Newest [?? = Unknown]",
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
        save_monitor_channels(monitor_channels)

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

        save_monitor_channels(monitor_channels)

        message = await get_monitor_partial_message(channel_id, int(message_id))

        if message is not None:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass
            except Exception as e:
                bot.logger.warning(f"Failed deleting monitor message for {channel_id_str}: {e}")

        await bot.discord.messages.send(
            "Monitor stopped in this channel.",
            response=True,
        )

    if not monitor_loop.is_running():
        monitor_loop.start()
