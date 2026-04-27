import discord
from discord.ext import tasks
import random

# here because Chat asked for it

async def setup(bot):
    @bot.setup.command(name="roll", description="Rolls a number from 1-100", eph=False, perm_requirement=0)
    async def roll(interaction: discord.Interaction):
        await bot.discord.messages.send(contents=f"{random.randint(1, 100)}", response=True)