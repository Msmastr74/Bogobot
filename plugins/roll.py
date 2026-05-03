import discord
from discord.ext import tasks, commands
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
    @bot.setup.command(name="choice", description="Chooses a random item from a list of items", eph=False, perm_requirement=0)
    async def choice(interaction: discord.Interaction, choices: str):
        choices_list = choices.split(",")
        if len(choices_list) == 0:
            await bot.discord.messages.send(contents="You must provide at least one choice.", response=True)
            return
        await bot.discord.messages.send(contents=f"{random.choice(choices_list)}", response=True)
    @bot.setup.command(name="bogo", description="shuffles", eph=False, perm_requirement=0)
    async def bogo(interaction: discord.Interaction, choices: str):
        choices_list = choices.split(",")
        if len(choices_list) == 0:
            await bot.discord.messages.send(contents="You must provide at least one choice.", response=True)
            return
        random.shuffle(choices_list)
        await bot.discord.messages.send(contents=f"{', '.join(choices_list)}", response=True)
    @bot.setup.command(name="randfloat", description="Rolls a random float from user specified range", eph=False, perm_requirement=0)
    async def randfloat(interaction: discord.Interaction, max: float, min: float = 0.0):
        if max < min:
            await bot.discord.messages.send(contents=f"Max must be bigger than min. Did you mean `/randfloat {min} {max}`?", response=True)
            return
        await bot.discord.messages.send(contents=f"{random.uniform(min, max)}", response=True)
    @bot.setup.command(name="randbool", description="Rolls a random boolean", eph=False, perm_requirement=0)
    async def randbool(interaction: discord.Interaction):
        await bot.discord.messages.send(contents=f"{random.choice([True, False])}", response=True)
