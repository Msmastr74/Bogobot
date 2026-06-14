from typing import Literal

import discord
import random
import asyncio
import unicodedata
from pyuca import Collator

# here because Chat asked for it

from bogobot_core import BotCore
from utils import groups
from ai import AIParam, action

FALSE_SPACE = '\u200d'
async def setup(bot: BotCore):
    unsorted_emoji = bot.discord.get_emoji('unsorted')
    sorted_emoji = bot.discord.get_emoji('sorted')
    collator = Collator()
    bogo = groups.bogo(bot)

    def split(text: str, delim: str):
        return list(
            filter(bool, 
                map(lambda t: t.strip(), text.split(delim))
            )
        )
    @bogo.command(name="roll", description="Rolls a number from 1-100", defer=False)
    @action(
        "bogo roll",
        "Roll a random number from 1 to 100.",
    )
    async def roll(interaction: discord.Interaction):
        await bot.discord.send(contents=f"{random.randint(1, 100)}", response=True)
    
    @bot.setup.command(name="randint", description="Roll a random integer", 
                       defer=False)
    @action(
        "randint",
        "Roll a random integer.",
        params={
            "max": AIParam(type=int),
            "min": AIParam(type=int, required=False, default=1),
        }
    )
    async def randint(interaction: discord.Interaction, max: int, min: int = 1):
        if max < min:
            await bot.discord.send(
                contents=f"Max cannot be smaller than min. Did you mean `/randint {min} {max}`?",
                response=True, ephemeral=True
            )
            return
        await bot.discord.send(contents=f"{random.randint(min, max)}", response=True)
    
    @bogo.command(name="choice", description="Chooses a random item from a list of items", defer=False)
    @action(
        "bogo choice",
        "Choose a random item.",
        params={
            "choices": AIParam(),
            "delimiter": AIParam("Delimiter between choices.", required=False, default=" "),
        },
    )
    async def choice(interaction: discord.Interaction, choices: str, delimiter: str = " "):
        choices_list = split(choices, delimiter)
        if len(choices_list) == 0:
            await bot.discord.send(contents="You must provide at least one choice.", response=True, ephemeral=True)
            return
        c = random.choice(choices_list)
        if not c:
            c = FALSE_SPACE
        await bot.discord.send(contents=c, response=True, safety_filter=True)

    @bogo.command(name="bogo", description="bogos your string", defer=False)
    @action(
        "bogo",
        "Shuffle text characters.",
        params={
            "text": AIParam(),
        },
    )
    async def bogo_bogo(interaction: discord.Interaction, text: str):
        char_list = list(text)
        random.shuffle(char_list)
        await bot.discord.send(contents=f"{''.join(char_list)}", response=True, safety_filter=True)
    
    @bogo.command(name="shuffle", description="shuffles", defer=False)
    @action(
        "bogo shuffle",
        "Shuffle list items.",
        params={
            "items": AIParam(),
            "delimiter": AIParam("Delimiter between items.", required=False, default=" "),
        },
    )
    async def shuffle(
        interaction: discord.Interaction, items: str, delimiter: str = " "
    ):
        items_list = split(items, delimiter)
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "
        random.shuffle(items_list)
        contents = f"{output_delimiter.join(items_list)}" or FALSE_SPACE
        await bot.discord.send(contents=contents, response=True, safety_filter=True)

    @bot.setup.command(name="sort", description="Sorts a list of items", defer=False)
    @action(
        "sort",
        "Sort list items.",
        params={
            "mode": AIParam(type=Literal["numerical", "lexicographic"]),
            "items": AIParam(),
            "delimiter": AIParam("Delimiter between items.", required=False, default=" "),
        },
    )
    async def sort(
        interaction: discord.Interaction,
        mode: Literal["numerical", "lexicographic"],
        items: str,
        delimiter: str = " ",
    ):
        items_list = split(items, delimiter)
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "

        if mode == "numerical":
            parsed_items: list[float | int] = []
            for item in items_list:
                try:
                    try:
                        parsed_items.append(int(item))
                    except ValueError:
                        parsed_items.append(float(item))
                except ValueError:
                    await bot.discord.send(
                        contents=f"Invalid item: {item}. All items must be numbers.",
                        response=True,
                        ephemeral=True
                    )
                    return
            sorted_items = list(map(str, sorted(parsed_items)))
        else:
            sorted_items = sorted(
                [
                    unicodedata.normalize("NFC", item)
                    for item in items_list
                ],
                key=collator.sort_key,
            )

        contents = output_delimiter.join(sorted_items) or FALSE_SPACE
        await bot.discord.send(contents=contents, response=True, safety_filter=True)
    
    @bot.setup.command(name="randlist", description="Generate random integers", 
                       defer=False)
    @action(
        "randlist",
        "Generate random integers.",
        params={
            "length": AIParam(type=int),
            "max": AIParam(type=int),
            "min": AIParam(type=int, required=False, default=1),
            "delimiter": AIParam("Delimiter between numbers.", required=False, default=" "),
        },
    )
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

    @bot.setup.command(name="randfloat", description="Roll a random float", 
                       defer=False)
    @action(
        "randfloat",
        "Roll a random float.",
        params={
            "max": AIParam(type=float),
            "min": AIParam(type=float, required=False, default=0.0),
        }
    )
    async def randfloat(interaction: discord.Interaction, max: float, min: float = 0.0):
        if max < min:
            await bot.discord.send(
                contents=f"Max cannot be smaller than min. Did you mean `/randfloat {min} {max}`?", response=True,
                ephemeral=True
            )
            return
        await bot.discord.send(contents=f"{random.uniform(min, max)}", response=True)
    
    @bot.setup.command(name="randbool", description="Rolls a random boolean", defer=False)
    @action(
        "randbool",
        "Roll true or false.",
    )
    async def randbool(interaction: discord.Interaction):
        await bot.discord.send(contents=f"{random.choice([True, False])}", response=True)

    @bogo.command(name="sort-list", description="Bogosorts a list of numbers", defer=False)
    @action(
        "bogo sort-list",
        "Bogosort numbers.",
        params={
            "items": AIParam(),
            "delimiter": AIParam("Delimiter between numbers.", required=False, default=" "),
        },
    )
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
            return f"`{output_delimiter.join(map(str, arr)) or FALSE_SPACE}`"

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

    @bogo.command(name="sort-lexicographic", description="Bogosorts a list of strings", defer=False)
    @action(
        "bogo sort-lexicographic",
        "Bogosort strings.",
        params={
            "items": AIParam(),
            "delimiter": AIParam("Delimiter between strings.", required=False, default=" "),
        },
    )
    async def bogosort_lexicographic(interaction: discord.Interaction, items: str, delimiter: str = " "):
        arr = [
            unicodedata.normalize("NFC", item)
            for item in split(items, delimiter)
        ]
        random.shuffle(arr)
        output_delimiter = delimiter if delimiter == " " else f"{delimiter} "
        def text():
            contents = output_delimiter.join(arr) or FALSE_SPACE
            contents = contents.replace('`', ' ')
            return f"`{contents}`"
        def sorted_arr():
            return sorted(arr, key=collator.sort_key)

        message = await bot.discord.send(
            contents=f"Sorting: {text()}", response=True
        )
        if not message:
            return
        counter = 25
        while arr != sorted_arr():
            await asyncio.sleep(0.5)
            await message.edit(contents=f"Sorting: {text()}")
            random.shuffle(arr)
            counter -= 1
            if counter <= 0 and arr != sorted_arr():
                await asyncio.sleep(1.5)
                await message.edit(contents=f"Sort failed: {text()}")
                await message.add_reaction(unsorted_emoji)
                return

        await asyncio.sleep(1.5)
        await message.edit(contents=f"Sorted: {text()}")
        await message.add_reaction(sorted_emoji)
        return
    
    @bogo.command(name="sort-listr", description="bogosorts a list of number?", defer=False)
    @action(
        "bogo sort-listr",
        "Bogosort numbers with success chance.",
        params={
            "items": AIParam(),
            "percent": AIParam(type=float),
            "delimiter": AIParam("Delimiter between numbers.", required=False, default=" "),
        },
    )
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
        def text():
            return f"`{output_delimiter.join(map(str, arr)) or FALSE_SPACE}`"

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

    @bogo.command(name="sort", description="Bogosorts", defer=False)
    @action(
        "bogo sort",
        "Bogosort a tiny list.",
        params={
            "item_count": AIParam("Number of items to sort, from 1 to 8.", type=int),
        },
    )
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
