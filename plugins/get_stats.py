import io

import discord
import datetime

from typing import Iterable
from bogobot_core import BotCore
from PIL import Image

class StatsView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        fields: Iterable[tuple[str, str]],
        updated_at: datetime.datetime | None = None
    ):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Bogosort Stream Statistics"))
        field_container = discord.ui.Container()
        for header, content in fields:
            field_container.add_item(
                discord.ui.TextDisplay(f"**{header}**\n{content}")
            )
        self.add_item(field_container)
        
        if updated_at is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(round(updated_at.timestamp()))}:T>"
            ))

class SortView(discord.ui.LayoutView):
    RED = '\x1b[31m'
    GREEN = '\x1b[32m'
    RESET = '\x1b[0m'
    
    def __init__(
        self,
        *,
        sort_state: list[tuple[bool, int]],
        correct_count: int,
        total_count: int,
        image: discord.File | None = None,
        timestamp: datetime.datetime | None = None
    ):
        super().__init__(timeout=None)
        
        self.add_item(discord.ui.TextDisplay("## Bogosort Stream"))
        colors = (self.RED, self.GREEN)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "```ansi\n"
                ' '.join(
                    map(
                        lambda t: f"{colors[t[0]]}{t[1]}{self.RESET}"
                    , sort_state)
                )
            ),
            discord.ui.TextDisplay(
                f"Best shuffle: `{correct_count}/{total_count}`"
            ),
        )
        if image is not None:
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image
                    )
                )
            )
        self.add_item(container)

        if timestamp is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(round(timestamp.timestamp()))}:T>"
            ))

async def setup(bot: BotCore):
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", eph=False, perm_requirement=0)
    async def get_stats(interaction: discord.Interaction):
        stats_list = bot.stats
        
        # Use .get() to prevent future KeyErrors if the cache is empty
        shuffles = stats_list.get("shuffles", "Loading...")
        comparisons = stats_list.get("comparisons", "Loading...")
        best_run = stats_list.get("best_run", "Loading...")
        shuffles_sec = stats_list.get("shuffles_sec", "Loading...")
        average_best_shuffle = stats_list.get("average_best_shuffle", "Loading...")
        uptime = stats_list.get("uptime", "Loading...")
        elapsed_time = await bot.get_stream_uptime()
        
        view = StatsView(
            fields=[
                ("Shuffles", shuffles),
                ("Comparisons", comparisons),
                ("Best Run", best_run),
                ("Shuffles Per Second", shuffles_sec),
                ("Average Best Shuffle", average_best_shuffle),
                ("Uptime [STREAM]", uptime),
                ("Elapsed Time [STATIC]", elapsed_time),
            ],
            updated_at = datetime.datetime.fromtimestamp(bot._last_ocr_refresh)
        )
        
        await bot.discord.send(
            view=view,
            response=True
        )

    last_frame: Image.Image | None = None
    last_value: tuple[
        list[tuple[bool, int]], int, float, Image.Image | None
    ] | None = None
    @bot.new_value_callback
    def on_new_value(sort_state: list[tuple[bool, int]], correct_count: int, timestamp: float):
        nonlocal last_value
        last_value = (
            sort_state, correct_count, timestamp, last_frame
        )
    @bot.new_frame_callback
    def on_new_frame(frame: Image.Image):
        nonlocal last_frame
        last_frame = frame
    
    @bot.setup.command(name="get_sort", description="Retrieve the current sort state", defer=False, perm_requirement=0)
    async def get_sort(interaction: discord.Interaction):
        if last_value is None:
            await bot.discord.send(
                "No sort data available yet.",
                response=True,
                ephemeral=True
            )
            return
        
        sort_state, correct_count, timestamp, frame = last_value
        file: discord.File | None = None
        if frame:
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            buffer.seek(0)
            file = discord.File(buffer, filename=f"sort_{timestamp}.png")

        total_count = len(sort_state)
        await bot.discord.send(
            view=SortView(
                sort_state=sort_state,
                correct_count=correct_count,
                total_count=total_count,
                timestamp=datetime.datetime.fromtimestamp(timestamp),
                image=file
            ),
            files=[file] if file else None,
            response=True
        )
