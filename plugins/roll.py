import discord
import random
import asyncio

# here because Chat asked for it

from bogobot_core import BotCore

UNSORTED_EMOJI_ID = 1495482469128999053
SORTED_EMOJI_ID = 1495381291162402939
async def setup(bot: BotCore):
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
        await bot.discord.send(contents=f"{random.choice(choices_list)}", response=True)

    @bot.setup.command(name="bogo", description="bogos your string", defer=False, perm_requirement=0)
    async def bogo(interaction: discord.Interaction, text: str):
        char_list = list(text)
        random.shuffle(char_list)
        await bot.discord.send(contents=f"{''.join(char_list)}", response=True)
    
    @bot.setup.command(name="shuffle", description="shuffles", defer=False, perm_requirement=0)
    async def shuffle(
        interaction: discord.Interaction, items: str, delimiter: str = " "
    ):
        items_list = split(items, delimiter)
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "
        if delimiter != " ":
            items_list = [item.strip() for item in items_list]
        random.shuffle(items_list)
        await bot.discord.send(contents=f"{output_delimiter.join(items_list)}", response=True)
    
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
        rand_list = [random.randint(min, max) for _ in range(length)]
        await bot.discord.send(contents=f"{delimiter.join(map(str, rand_list))}", response=True)

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

    @bot.setup.command(name="bogosort", description="bogosorts a list of numbers", defer=False, perm_requirement=0)
    async def bogosort(interaction: discord.Interaction, items: str, delimiter: str = " "):
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
        def text(): return output_delimiter.join(map(str, arr))

        message = await bot.discord.send(
            contents=f"Sorting: `{text()}`", response=True
        )
        if not message:
            return
        counter = 15
        while arr != sorted(arr):
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: `{text()}`")
            random.shuffle(arr)
            counter -= 1
            if counter <= 0 and arr != sorted(arr):
                await asyncio.sleep(1.5)
                await message.edit(contents=f"Sort failed: `{text()}`")
                await message.add_reaction(UNSORTED_EMOJI_ID)
                return

        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: `{text()}`")
        await message.add_reaction(SORTED_EMOJI_ID)
        return
    
    @bot.setup.command(name="bogosortr", description="bogosorts?", defer=False, perm_requirement=0)
    async def bogosortr(interaction: discord.Interaction, items: str, percent: float, delimiter: str = " "):
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
        def text(): return output_delimiter.join(map(str, arr))

        should_succeed = random.random() < (percent / 100)
        sorted_arr = sorted(arr)

        if not should_succeed and len(set(arr)) <= 1:
            message = await bot.discord.send(
                contents=f"Sorting: `{text()}`",
                response=True
            )

            if not message:
                return

            await asyncio.sleep(1.5)
            await message.edit(contents=f"Sort failed: `{text()}`")
            await message.add_reaction(UNSORTED_EMOJI_ID)
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
            contents=f"Sorting: `{text()}`",
            response=True
        )
        if not message:
            return

        counter = 15
        succeed_count = random.randint(0, counter // 2) if should_succeed else 0
        while arr != sorted_arr:
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: `{text()}`")
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
                await message.edit(contents=f"Sort failed: `{text()}`")
                await message.add_reaction(UNSORTED_EMOJI_ID)
                return
        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: `{text()}`")
        await message.add_reaction(SORTED_EMOJI_ID)
        return
