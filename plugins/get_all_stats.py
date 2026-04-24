import discord
from discord.ext import tasks
import datetime

async def setup(bot):
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", perm_requirement=0)
    async def get_all(interaction: discord.Interaction):
        # Defer since OCR takes ~0.25s
        await interaction.response.defer()
    
        # Each call triggers the centralized 'refresh_ocr_data' refresher
        # The first one triggers OCR; the others use the cache to save CPU/Battery
        shuffles = await bot.info.get_shuffles()
        comparisons = await bot.info.get_comparisons()
        best_run = await bot.info.get_best_run()
        shuffles_min = await bot.info.get_shuffles_min()
        serial = await bot.info.get_best_shuffle()

        embed = discord.Embed(title="Current Bogosort Statistics", color=discord.Color.green())
        embed.add_field(name="Recent Serial", value=f"`{serial}`", inline=False)
        embed.add_field(name="Total Shuffles", value=f"`{shuffles}`", inline=True)
        embed.add_field(name="Comparisons", value=f"`{comparisons}`", inline=True)
        embed.add_field(name="Best Run", value=f"`{best_run}`", inline=True)
        embed.add_field(name="Shuffles/min", value=f"`{shuffles_min}`", inline=True)
        embed.set_footer(text=f"Fetched at: {datetime.now().strftime('%H:%M:%S')}")
        
        await interaction.followup.send(embed=embed)
