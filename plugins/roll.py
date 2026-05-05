import discord
from discord.ext import tasks, commands
import random
import asyncio

# here because Chat asked for it

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
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
    @bot.setup.command(name="bogo", description="bogoes your string", eph=False, perm_requirement=0) #the old bogo was not bogo, just choice.
    async def shuffle(interaction: discord.Interaction, choices: str):
        choices_list = list(choices)
        random.shuffle(choices_list)
        await bot.discord.messages.send(contents=f"{''.join(choices_list)}", response=True)
    @bot.setup.command(name="randfloat", description="Rolls a random float from user specified range", eph=False, perm_requirement=0)
    async def randfloat(interaction: discord.Interaction, max: float, min: float = 0.0):
        if max < min:
            await bot.discord.messages.send(contents=f"Max must be bigger than min. Did you mean `/randfloat {min} {max}`?", response=True)
            return
        await bot.discord.messages.send(contents=f"{random.uniform(min, max)}", response=True)
    
    @bot.setup.command(name="randbool", description="Rolls a random boolean", eph=False, perm_requirement=0)
    async def randbool(interaction: discord.Interaction):
        await bot.discord.messages.send(contents=f"{random.choice([True, False])}", response=True)

    @bot.setup.command(name="bogosort", description="bogosorts a comma-separated list of numbers", eph=False, perm_requirement=0)
    async def bogosort(interaction: discord.Interaction, items: str):
        items_list = items.split(",")
        arr: list[float | int] = []
        for item in items_list:
            try:
                try:
                    arr.append(int(item))
                except ValueError:
                    arr.append(float(item))
            except ValueError:
                await bot.discord.messages.send(contents=f"Invalid item: {item}. All items must be numbers.", response=True)
                return
        if len(arr) == 0:
            await bot.discord.messages.send(contents="You must provide at least one item.", response=True)
            return
        random.shuffle(arr)

        message = await bot.discord.messages.send(
            contents=f"Sorting: `{', '.join(map(str, arr))}`", response=True
        )
        if not message:
            return
        counter = 15
        while arr != sorted(arr):
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: `{', '.join(map(str, arr))}`")
            random.shuffle(arr)
            counter -= 1
            if counter <= 0:
                await asyncio.sleep(1.5)
                await message.edit(contents=f"Sort failed: `{', '.join(map(str, arr))}`")
                await message.add_reaction("<:unsorted:1495482469128999053>")
                return

        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: `{', '.join(map(str, arr))}`")
        await message.add_reaction("<:sorted:1495381291162402939>")
        return
    
    @bot.setup.command(name="bogosortr", description="bogosorts a comma-separated list of numbers?", eph=False, perm_requirement=0)
    async def bogosortr(interaction: discord.Interaction, items: str, percent: float):
        if percent < 0 or percent > 100:
            await bot.discord.messages.send(
                contents="Percent must be between 0 and 100.",
                response=True
            )
            return

        items_list = items.split(",")
        arr: list[float | int] = []

        for item in items_list:
            try:
                try:
                    arr.append(int(item))
                except ValueError:
                    arr.append(float(item))
            except ValueError:
                await bot.discord.messages.send(
                    contents=f"Invalid item: {item}. All items must be numbers.",
                    response=True
                )
                return

        if len(arr) == 0:
            await bot.discord.messages.send(
                contents="You must provide at least one item.",
                response=True
            )
            return

        should_succeed = random.random() < (percent / 100)
        sorted_arr = sorted(arr)

        if not should_succeed and len(set(arr)) <= 1:
            message = await bot.discord.messages.send(
                contents=f"Sorting: `{', '.join(map(str, arr))}`",
                response=True
            )

            if not message:
                return

            await asyncio.sleep(1)
            await message.edit(contents=f"Sort failed: `{', '.join(map(str, arr))}`")
            await message.add_reaction("<:unsorted:1495482469128999053>")
            return

        def shuffle_unsorted(arr: list[float | int]) -> None:
            while True:
                random.shuffle(arr)
                if arr != sorted_arr:
                    return

        if should_succeed:
            random.shuffle(arr)
        else:
            shuffle_unsorted(arr)
        message = await bot.discord.messages.send(
            contents=f"Sorting: `{', '.join(map(str, arr))}`",
            response=True
        )
        if not message:
            return

        counter = 15
        succeed_count = random.randint(0, counter // 2) if should_succeed else 0
        while arr != sorted_arr:
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: `{', '.join(map(str, arr))}`")
            counter -= 1
            if should_succeed and counter <= succeed_count:
                arr = sorted_arr.copy()
            elif should_succeed:
                random.shuffle(arr)
            else:
                shuffle_unsorted(arr)
            if counter <= 0:
                await asyncio.sleep(1.5)
                await message.edit(contents=f"Sort failed: `{', '.join(map(str, arr))}`")
                await message.add_reaction("<:unsorted:1495482469128999053>")
                return
        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: `{', '.join(map(str, arr))}`")
        await message.add_reaction("<:sorted:1495381291162402939>")
        return
