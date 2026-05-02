import discord
from discord.ext import tasks
from datetime import datetime

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

new_var = ""
num_array = ["0"]
num_array *= 30
async def setup(bot: 'BotCore'):
    monitor_embed: 'BotCore._Discord._Embeds.EmbedHandle' = None
    @tasks.loop(seconds=0.5)
    async def monitor_loop():
        global num_array
        # Get the value and the "is_new" flag from our updated core
        new_var, is_new = await bot.info.get_best_shuffle()
        
        # If OCR didn't run because the file hasn't changed, do nothing
        if not is_new:
            return 
        if new_var in ["0", "1", ""] or int(new_var) > 25:
            new_var = "!"

        # We have fresh data; update the array
        num_array.pop(0)
        num_array.append(new_var)

        # Only edit the message when there is actually a change
        try:
            await monitor_embed.edit(contents=".".join(num_array), author=datetime.now().strftime('[%H:%M:%S]'))
        except Exception as e:
            print(f"Edit Error: {e}")
    @bot.setup.command(name="monitor", description="Begins monitoring sorted number counts from the stream")
    async def monitor(interaction: discord.Interaction):
        nonlocal monitor_embed
        bot.setup.channel_id(interaction.channel_id)
        
        # 2. Create the message
        monitor_embed = await bot.discord.embeds.send(contents="Initializing...", title="Serial Number", footer="! = Flagged as incorrect")
        
        assert monitor_embed is not None, 'monitor_embed is None, check default_channel_id'
        
        # 3. Follow up so the user knows it's done
        await bot.discord.messages.send("Monitor system online.", response=True)
        
        if not monitor_loop.is_running():
            monitor_loop.start()
    @bot.setup.command(name="stop", description="Stops the stream monitor")
    async def stop_monitor(interaction: discord.Interaction):
        nonlocal monitor_embed
        # 1. Stop the loop
        if monitor_loop.is_running():
            monitor_loop.stop()
            
            # 2. Optional: Clean up the Discord message
            if monitor_embed:
                await monitor_embed.delete()
            
            # 3. Confirm to the user
            await bot.discord.messages.send("Monitor stopped.", response=True)
        else:
            await bot.discord.messages.send("Monitor is not currently running.", response=True)
