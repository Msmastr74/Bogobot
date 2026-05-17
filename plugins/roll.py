from typing import Literal

import discord
import random
import asyncio

# here because Chat asked for it

from bogobot_core import BotCore

async def setup(bot: BotCore):
    unsorted_emoji = bot.discord.get_emoji('unsorted')
    sorted_emoji = bot.discord.get_emoji('sorted')
    def split(text: str, delim: str): return list(filter(bool, text.split(delim)))
    @bot.setup.command(name="roll", description="Rolls a number from 1-100", defer=False, perm_requirement=0)
    async def roll(interaction: discord.Interaction):
        await bot.discord.send(contents=f"{random.randint(1, 100)}", response=True)
    
    @bot.setup.command(name="randint", description="Rolls a random number from user specified range", 
                       defer=False, perm_requirement=0)
    async def randint(interaction: discord.Interaction, max: int, min: int = 1):
        if max < min:
            await bot.discord.send(
                contents=f"Max cannot be smaller than min. Did you mean `/randint {min} {max}`?",
                response=True, ephemeral=True
            )
            return
        await bot.discord.send(contents=f"{random.randint(min, max)}", response=True)
    
    @bot.setup.command(name="choice", description="Chooses a random item from a list of items", defer=False, perm_requirement=0)
    async def choice(interaction: discord.Interaction, choices: str, delimiter: str = " "):
        choices_list = split(choices, delimiter)
        if len(choices_list) == 0:
            await bot.discord.send(contents="You must provide at least one choice.", response=True, ephemeral=True)
            return
        c = random.choice(choices_list)
        if not c:
            c = '\u200d'
        await bot.discord.send(contents=c, response=True, safety_filter=True)

    @bot.setup.command(name="bogo", description="bogos your string", defer=False, perm_requirement=0)
    async def bogo(interaction: discord.Interaction, text: str):
        char_list = list(text)
        random.shuffle(char_list)
        await bot.discord.send(contents=f"{''.join(char_list)}", response=True, safety_filter=True)
    
    @bot.setup.command(name="shuffle", description="shuffles", defer=False, perm_requirement=0)
    async def shuffle(
        interaction: discord.Interaction, items: str, delimiter: str = " "
    ):
        items_list = split(items, delimiter)
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "
        if delimiter != " ":
            items_list = [item.strip() for item in items_list]
        random.shuffle(items_list)
        contents = f"{output_delimiter.join(items_list)}"
        if not contents:
            contents = '\u200d'
        await bot.discord.send(contents=contents, response=True, safety_filter=True)
    
    @bot.setup.command(name="randlist", description="Generates a random list of integers in a range", 
                       defer=False, perm_requirement=0)
    async def randlist(interaction: discord.Interaction, length: int, max: int, min: int = 1, delimiter: str = " "):
        if length < 1:
            await bot.discord.send(contents=f"Length {length} is invalid.", response=True, ephemeral=True)
            return
        if max < min:
            await bot.discord.send(
                contents=f"Max cannot be smaller than min. Did you mean `/randlist {length} {min} {max}`?", 
                response=True, ephemeral=True
            )
            return
        if delimiter != " ":
            delimiter = f"{delimiter} "
        delimiter = delimiter.replace('`', ' ')
        rand_list = [random.randint(min, max) for _ in range(length)]
        await bot.discord.send(contents=f"`{delimiter.join(map(str, rand_list))}`", response=True)

    @bot.setup.command(name="randfloat", description="Rolls a random float from user specified range", 
                       defer=False, perm_requirement=0)
    async def randfloat(interaction: discord.Interaction, max: float, min: float = 0.0):
        if max < min:
            await bot.discord.send(
                contents=f"Max cannot be smaller than min. Did you mean `/randfloat {min} {max}`?", response=True,
                ephemeral=True
            )
            return
        await bot.discord.send(contents=f"{random.uniform(min, max)}", response=True)
    
    @bot.setup.command(name="randbool", description="Rolls a random boolean", defer=False, perm_requirement=0)
    async def randbool(interaction: discord.Interaction):
        await bot.discord.send(contents=f"{random.choice([True, False])}", response=True)

    @bot.setup.command(name="bogosort-list", description="Bogosorts a list of numbers", defer=False, perm_requirement=0)
    async def bogosort_list(interaction: discord.Interaction, items: str, delimiter: str = " "):
        items_list = split(items, delimiter)
        arr: list[float | int] = []
        for item in items_list:
            try:
                try:
                    arr.append(int(item))
                except ValueError:
                    arr.append(float(item))
            except ValueError:
                await bot.discord.send(
                    contents=f"Invalid item: {item}. All items must be numbers.", 
                    response=True, ephemeral=True
                )
                return
        random.shuffle(arr)
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "
        output_delimiter = output_delimiter.replace('`', ' ')
        def text():
            return f"`{output_delimiter.join(map(str, arr)) or ' '}`"

        message = await bot.discord.send(
            contents=f"Sorting: {text()}", response=True
        )
        if not message:
            return
        counter = 25
        while arr != sorted(arr):
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: {text()}")
            random.shuffle(arr)
            counter -= 1
            if counter <= 0 and arr != sorted(arr):
                await asyncio.sleep(1.5)
                await message.edit(contents=f"Sort failed: {text()}")
                await message.add_reaction(unsorted_emoji)
                return

        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: {text()}")
        await message.add_reaction(sorted_emoji)
        return
    
    @bot.setup.command(name="bogosort-listr", description="bogosorts a list of number?", defer=False, perm_requirement=0)
    async def bogosort_listr(interaction: discord.Interaction, items: str, percent: float, delimiter: str = " "):
        if percent < 0 or percent > 100:
            await bot.discord.send(
                contents="Percent must be between 0 and 100.",
                response=True, ephemeral=True
            )
            return

        items_list = split(items, delimiter)
        arr: list[float | int] = []

        for item in items_list:
            try:
                try:
                    arr.append(int(item))
                except ValueError:
                    arr.append(float(item))
            except ValueError:
                await bot.discord.send(
                    contents=f"Invalid item: {item}. All items must be numbers.",
                    response=True, ephemeral=True
                )
                return
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "
        output_delimiter = output_delimiter.replace('`', ' ')
        def text(): return output_delimiter.join(map(str, arr))

        should_succeed = random.random() < (percent / 100)
        sorted_arr = sorted(arr)

        if not should_succeed and len(set(arr)) <= 1:
            message = await bot.discord.send(
                contents=f"Sorting: {text()}",
                response=True
            )

            if not message:
                return

            await asyncio.sleep(1.5)
            await message.edit(contents=f"Sort failed: {text()}")
            await message.add_reaction(unsorted_emoji)
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
        message = await bot.discord.send(
            contents=f"Sorting: {text()}",
            response=True
        )
        if not message:
            return

        counter = 25
        succeed_count = random.randint(0, counter // 2) if should_succeed else 0
        while arr != sorted_arr:
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: {text()}")
            counter -= 1
            if should_succeed and counter <= succeed_count:
                arr = sorted_arr.copy()
                break
            elif should_succeed:
                random.shuffle(arr)
            else:
                shuffle_unsorted(arr)
            if counter <= 0:
                await asyncio.sleep(1.5)
                await message.edit(contents=f"Sort failed: {text()}")
                await message.add_reaction(unsorted_emoji)
                return
        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: {text()}")
        await message.add_reaction(sorted_emoji)
        return

    @bot.setup.command(name="bogosort", description="Bogosorts", defer=False, perm_requirement=0)
    async def bogosort(interaction: discord.Interaction, item_count: Literal[1, 2, 3, 4, 5, 6, 7, 8]):
        items: list[tuple[int, str]] = [
            (i, chr(0x2580 + i)) for i in range(1, item_count + 1)
        ]
        random.shuffle(items)
        def format():
            return '`' + ''.join(map(lambda t: t[1], items)) + '`'
        def is_sorted():
            last_v: int | None = None
            for v, _char in items:
                if last_v is not None and v < last_v:
                    return False
                last_v = v
            return True

        message = await bot.discord.send(
            contents=format(), response=True
        )
        if not message:
            return
        counter = 25
        while not is_sorted():
            await asyncio.sleep(0.5)
            if counter <= 0:
                await asyncio.sleep(0.5)
                await message.add_reaction(unsorted_emoji)
                return
            random.shuffle(items)
            await message.edit(contents=format())
            counter -= 1
        await asyncio.sleep(0.5)

        await message.add_reaction(sorted_emoji)
        return
