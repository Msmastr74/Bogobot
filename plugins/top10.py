# hey its the thing made by tomcat!

import discord
from discord.ext import tasks
import aiohttp
import json

# i used claude lmao i aint doin allat

# Rank to emoji mapping
RANK_EMOJIS = {
    "champion": "<:champion:1498536682046357644>",      # Crown
    # Diamond (high tier)
    "grandmaster": "<:grandmaster:1498536634164051978>",
    "master": "<:master:1498537542104907858>",        # Star
    "diamond": "<:diamond:1498537469124018186>",       # Diamond shape
    "platinum": "<:platinum:1498537400828166194>",      # Silver medal
    "gold": "<:gold:1498537254895878295>",          # Gold medal
    "silver": "<:silver:1498535347834060800>",        # White circle
    "bronze": "<:bronze:1498896875728666654>",        # Bronze medal
}

async def fetch_leaderboard():
    """Fetch the leaderboard data from the API"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://swapjs.dev/api/group/leaderboard") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("rows", [])
    except Exception as e:
        print(f"Error fetching leaderboard: {e}")
    return []


def format_leaderboard(rows: list, limit: int = 10) -> str:
    """Format leaderboard data with emojis into a readable string"""
    if not rows:
        return "No leaderboard data available."

    formatted_lines = []

    for player in rows[:limit]:
        rank_emoji = RANK_EMOJIS.get(player.get("rank", "").lower(), "•")
        pos = player.get("pos", "?")
        name = player.get("name", "Unknown")
        elo = player.get("elo", 0)
        win_rate = player.get("win_rate", 0)

        line = f"{pos}. {rank_emoji} **{name}** - ELO: {elo} | Win Rate: {win_rate}%"
        formatted_lines.append(line)

    return "\n".join(formatted_lines)

# i wrote this tho

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    @bot.setup.command(name="top", description="Gets the top 10 players in sortoffs!", eph=False, perm_requirement=0)
    async def top(interaction: discord.Interaction):
        raw_lb = await fetch_leaderboard()
        rows = format_leaderboard(rows=raw_lb, limit=10)
        
        embed = await bot.discord.embeds.send(contents="Top players ranked by elo", title="Leaderboard", footer="Data from swapjs.dev", color=discord.Color.gold(), response=True)
        assert embed is not None

        if not rows:
            await embed.edit(contents="No data availiable")
        else:
            await embed.edit(contents=f"{rows}")
