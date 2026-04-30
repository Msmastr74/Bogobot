import discord
from discord.ext import tasks
import random

# here because Chat asked for it

async def setup(bot):
    @bot.setup.command(name="roll", description="Rolls a number from 1-100", eph=False, perm_requirement=0)
    async def roll(interaction: discord.Interaction):
        await bot.discord.messages.send(contents=f"{random.randint(1, 100)}", response=True)
    @bot.setup.command(name="randint", description="Rolls a random number from user specified range", eph=False, perm_requirement=0)
    async def randint(interaction: discord.Interaction, max: int, min: int = 1):
        if max < min:
            await bot.discord.messages.send(contents=f"Max must be bigger than min. Did you mean `/randint {min} {max}`?", response=True)
            return
        await bot.discord.messages.send(contents=f"{random.randint(min, max)}", response=True)