import discord
from discord.ext import tasks
from datetime import datetime

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

num_matrix: list[list[tuple[str, float]]] = [[] for _ in range(30)]

async def setup(bot: 'BotCore'):
    monitor_embed: 'BotCore._Discord._Embeds.EmbedHandle | None' = None
    @tasks.loop(seconds=0.5)
    async def monitor_loop():
        global num_matrix
        # Get the value and the "is_new" flag from our updated core
        new_vars, is_new = await bot.info.get_best_shuffles()
        
        # If OCR didn't run because the file hasn't changed, do nothing
        if not is_new:
            return

        # We have fresh data; update the matrix
        num_matrix.pop(0)
        num_matrix.append([])

        for i in range(len(new_vars)):
            new_var, conf = new_vars[i]
            if new_var in ["0", "1", ""] or int(new_var) > 25:
                continue
            num_matrix[-i - 1].append((new_var, conf))

        num_array = []
        for sublist in num_matrix:
            if not sublist:
                num_array.append("?")
                continue

            scores = {}
            counts = {}

            for value, conf in sublist:
                scores[value] = scores.get(value, 0.0) + conf
                counts[value] = counts.get(value, 0) + 1

            # 1. Find the absolute highest score to set the competitive threshold
            absolute_max_score = max(scores.values())
            
            # 2. Identify candidates whose scores are within 50% of the max score
            # Formula: Score >= (MaxScore * 0.5)
            candidates = [v for v, s in scores.items() if s >= (absolute_max_score * 0.5)]

            # 3. From the candidate pool, choose the one with the highest frequency (mode)
            max_val = "?"
            max_count = -1
            max_score = -1.0

            for value in candidates:
                curr_count = counts[value]
                curr_score = scores[value]

                # Priority 1: Highest Count (Mode)
                # Priority 2: Total Confidence (Tie-break)
                if (curr_count > max_count) or (curr_count == max_count and curr_score > max_score):
                    max_count = curr_count
                    max_score = curr_score
                    max_val = value

            num_array.append(max_val)

        # Edit the message
        try:
            assert monitor_embed is not None
            await monitor_embed.edit(contents=".".join(num_array), author=datetime.now().strftime('[%H:%M:%S]'))
        except Exception as e:
            print(f"Edit Error: {e}")
    
    @bot.setup.command(name="monitor", description="Begins monitoring sorted number counts from the stream")
    async def monitor(interaction: discord.Interaction):
        nonlocal monitor_embed
        bot.setup.channel_id(interaction.channel_id)
        
        # 2. Create the message
        embed = await bot.discord.embeds.send(contents="Initializing...", title="Serial Number", footer="? = Unknown")
        
        assert embed is not None, 'monitor_embed is None, check default_channel_id'
        monitor_embed = embed
        
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
