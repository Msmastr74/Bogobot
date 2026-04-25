import discord
from discord.ext import tasks
import datetime

async def setup(bot):
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", perm_requirement=0)
    async def get_all(interaction: discord.Interaction):
        
        stats_list = await bot.info.get_stats_all()
        
        shuffles = stats_list["shuffles"]
        comparisons = stats_list["comparisons"]
        best_run = stats_list["best_run"]
        shuffles_min = stats_list["shuffles_min"]
        serial = await bot.info.get_best_shuffle()

        bot.discord.embeds.send(title="Current Bogosort Statistics", color=discord.Color.green(), footer=f"Fetched at: {datetime.now().strftime('%H:%M:%S')}", response=True)
        bot.discord.embeds.edit(name="Recent Serial", value=f"`{serial}`", add_field=True)
        bot.discord.embeds.edit(name="Total Shuffles", value=f"`{shuffles}`", add_field=True)
        bot.discord.embeds.edit(name="Comparisons", value=f"`{comparisons}`", add_field=True)
        bot.discord.embeds.edit(name="Best Run", value=f"`{best_run}`", add_field=True)
        bot.discord.embeds.edit(name="Shuffles/min", value=f"`{shuffles_min}`", add_field=True)
