import discord
from utils import tasks
import random

from bogobot_core import BotCore

async def setup(bot: BotCore):
    @tasks.loop(seconds=15)
    async def update_status():
        if not bot.user:
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
        await bot.change_presence(activity=discord.CustomActivity(name=shuffled_text))
    
    @bot.init_callback
    async def init():
        if not update_status.is_running():
            update_status.start()
    
    @bot.close_callback
    async def close():
        if update_status.is_running():
            update_status.cancel()
