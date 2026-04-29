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


def format_leaderboard_embed(rows: list, limit: int = 10) -> discord.Embed:
  """Format leaderboard as a Discord embed"""
  if not rows:
    embed = discord.Embed(title="Leaderboard", description="No data available")
    return embed

  embed = discord.Embed(
      title="sortoffs Leaderboard",
      description="Top players ranked by elo",
      color=discord.Color.gold()
  )

  leaderboard_text = ""
  for player in rows[:limit]:
    rank_emoji = RANK_EMOJIS.get(player.get("rank", "").lower(), "•")
    pos = player.get("pos", "?")
    name = player.get("name", "Unknown")
    elo = player.get("elo", 0)
    win_rate = player.get("win_rate", 0)

    leaderboard_text += f"{pos}. {rank_emoji} **{name}**\nELO: {elo} | Win Rate: {win_rate}%\n\n"

  embed.add_field(name="Players", value=leaderboard_text, inline=False)
  embed.set_footer(text="Data from swapjs.dev")

  return embed

# i wrote this tho


async def setup(bot):
  @bot.command(name="top", description="Gets the top 10 players in sortoffs!",
               ephemeral=False, permissions_required=0)
  async def roll(interaction: discord.Interaction):
    await interaction.response.defer()

    rows = await fetch_leaderboard()
    embed = format_leaderboard_embed(rows, limit=10)

    await interaction.followup.send(embed=embed)
