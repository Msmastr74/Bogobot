import discord
import time

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

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
        
        discord_embed = discord.Embed(
            title="Current Bogosort Statistics",
            description=f"Fetched at: <t:{int(round(time.time()))}:T>\nUpdated at: <t:{int(round(bot._last_ocr_refresh))}:T>",
            color=discord.Color.green()
        )
        
        discord_embed.add_field(name="Shuffles", value=f"{shuffles}", inline=False)
        discord_embed.add_field(name="Comparisons", value=f"{comparisons}", inline=False)
        discord_embed.add_field(name="Best Run", value=f"{best_run}", inline=False)
        discord_embed.add_field(name="Shuffles Per Second", value=f"{shuffles_sec}", inline=False)
        discord_embed.add_field(name="Average Best Shuffle", value=f"{average_best_shuffle}", inline=False)
        discord_embed.add_field(name="Uptime [STREAM]", value=f"{uptime}", inline=False)
        discord_embed.add_field(name="Elapsed Time [STATIC]", value=f"{elapsed_time}", inline=False)
        
        await bot.discord.send_embed(embed=discord_embed, response=True)
