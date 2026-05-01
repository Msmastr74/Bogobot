import discord
from discord.ext import tasks
import time

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", eph=False, perm_requirement=0)
    async def get_all(interaction: discord.Interaction):
        
        stats_list = await bot.info.get_stats_all()
        
        # Use .get() to prevent future KeyErrors if the cache is empty
        shuffles = stats_list.get("shuffles", "Loading...")
        comparisons = stats_list.get("comparisons", "Loading...")
        best_run = stats_list.get("best_run", "Loading...")
        shuffles_min = stats_list.get("shuffles_min", "Loading...")
        elapsed_time = await bot.info.get_uptime()
        

        # Send the base embed
        await bot.discord.embeds.send(contents=f"Fetched at: <t:{int(round(time.time()))}:T>", title="Current Bogosort Statistics", color=discord.Color.green(), response=True)
        
        # Rapid-fire the fields 
        await bot.discord.embeds.edit(contents=f"{shuffles}", title="Shuffles", add_field=True)
        await bot.discord.embeds.edit(contents=f"{comparisons}", title="Comparisons", add_field=True)
        await bot.discord.embeds.edit(contents=f"{best_run}", title="Best Run", add_field=True)
        await bot.discord.embeds.edit(contents=f"{shuffles_min}", title="Shuffles Per Second", add_field=True)
        await bot.discord.embeds.edit(contents=f"{elapsed_time}", title="Elapsed Time", add_field=True)
