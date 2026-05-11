import discord
from discord.ext import tasks
import random

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    @tasks.loop(seconds=15)
    async def update_status():
        if not bot.user:
            return
        text = bot.user.name
        tlist = list(text)
        random.shuffle(tlist)
        shuffled_text = ''.join(tlist)
        if bot.is_closed():
            return
        await bot.change_presence(activity=discord.CustomActivity(name=shuffled_text))
    
    @bot.init_callback
    async def init():
        if not update_status.is_running():
            update_status.start()
