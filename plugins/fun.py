import discord
from utils import tasks
import random
import re
import time

from bogobot_core import BotCore
from utils import groups
from utils.ai import action
from plugins import ai as ai_plugin

class BogonameView(discord.ui.LayoutView):
    def __init__(self, original_name: str, new_name: str) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay('## Bogoname'),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**Original Name**\n{discord.utils.escape_markdown(original_name)}"),
            discord.ui.TextDisplay(f"**Bogoed name**\n{discord.utils.escape_markdown(new_name)}"),
            accent_colour=discord.Colour.random()
        ))
        self.add_item(discord.ui.TextDisplay(f"-# <t:{int(time.time())}:f>"))

EMOJI_ALIAS_RE = re.compile(r":([A-Za-z0-9_]+):")

async def setup(bot: BotCore):
    bogo = groups.bogo(bot)

    @tasks.loop(seconds=15)
    async def update_status():
        if not bot.user:
            return
        if ai_plugin.ai_on_break():
            return
        text = bot.user.name
        tlist = list(text)
        chance = random.random()
        if chance < 0.1:
            pass # unshuffled
        elif chance < 0.4 and tlist.count('-') > 0:
            left = tlist[0:tlist.index('-')]
            right = tlist[tlist.index('-')+1:]
            random.shuffle(left)
            random.shuffle(right)
            tlist = left + ['-'] + right
        elif chance < 0.4 and text == 'Bogobot':
            left = tlist[0:4]
            right = tlist[4:]
            random.shuffle(left)
            random.shuffle(right)
            tlist = left + right
        else:
            random.shuffle(tlist)
        shuffled_text = ''.join(tlist)
        if bot.is_closed():
            return
        await bot.discord.change_presence(activity=discord.CustomActivity(name=shuffled_text))
    
    @bot.init_callback
    async def init():
        if not update_status.is_running():
            update_status.start()
    
    @bot.close_callback
    async def close():
        if update_status.is_running():
            update_status.cancel()

    @bogo.command(name="name", description="Bogoes your name", perm_requirement=0, defer=False)
    @action(
        "bogo name",
        "Shuffle the user's display name. Does not take any parameters.",
    )
    async def bogo_name(interaction: discord.Interaction):
        member = interaction.user
        if isinstance(member, discord.User):
            await bot.discord.send(
                "You must be in a guild to use this command!",
                response=True,
                ephemeral=True,
            )
            return
        original_name = member.display_name
        name_list = list(original_name)
        random.shuffle(name_list)
        new_name = ''.join(name_list)
        try:
            await member.edit(nick=new_name)
        except discord.Forbidden:
            await bot.discord.send(
                f"{getattr(bot.user, 'mention', 'The bot')} could not set your nickname.",
                response=True,
                ephemeral=True,
            )
            return
        await bot.discord.send(
            view=BogonameView(original_name, new_name),
            response=True,
            safety_filter=True
        )
