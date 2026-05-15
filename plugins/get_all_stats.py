import discord
import datetime

from typing import TYPE_CHECKING, Iterable
if TYPE_CHECKING:
    from main import BotCore

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

async def setup(bot: 'BotCore'):
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", eph=False, perm_requirement=0)
    async def get_stats(interaction: discord.Interaction):
        stats_list = await bot.info.get_stats_all()
        
        # Use .get() to prevent future KeyErrors if the cache is empty
        shuffles = stats_list.get("shuffles", "Loading...")
        comparisons = stats_list.get("comparisons", "Loading...")
        best_run = stats_list.get("best_run", "Loading...")
        shuffles_sec = stats_list.get("shuffles_sec", "Loading...")
        average_best_shuffle = stats_list.get("average_best_shuffle", "Loading...")
        uptime = stats_list.get("uptime", "Loading...")
        elapsed_time = await bot.info.get_uptime()
        
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
