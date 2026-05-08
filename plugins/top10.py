# hey its the thing made by tomcat!

import discord
import aiohttp

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    # Rank to emoji mapping
    RANK_EMOJIS = {
        "champion": "<:champion:1498536682046357644>",
        "grandmaster": "<:grandmaster:1498536634164051978>",
        "master": "<:master:1498537542104907858>",
        "diamond": "<:diamond:1498537469124018186>",
        "platinum": "<:platinum:1498537400828166194>",
        "gold": "<:gold:1498537254895878295>",
        "silver": "<:silver:1498535347834060800>",
        "bronze": "<:bronze:1498896875728666654>"
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
            bot.logger.warning(f"Error fetching leaderboard: {e}")
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
            streak = int(player.get("current_streak", "0"))
            streak_text = ""
            if streak > 0:
                streak_text = f"🔥 {streak}"
            elif streak < 0:
                streak_text = f"❄️ {abs(streak)}"

            line = f"{pos}. {rank_emoji} **{name}** - ELO: {elo} | Win Rate: {win_rate}%"
            if streak_text:
                line += f" | {streak_text}"
            formatted_lines.append(line)

        return "\n".join(formatted_lines)

    @bot.setup.command(name="top", description="Gets the top 10 players in sortoffs!", eph=False, perm_requirement=0)
    async def top(interaction: discord.Interaction):
        raw_lb = await fetch_leaderboard()
        rows = format_leaderboard(rows=raw_lb, limit=10)
        
        embed = await bot.discord.send_embed(contents="Top players ranked by elo", title="Leaderboard", footer="Data from swapjs.dev", color=discord.Color.gold(), response=True)
        assert embed is not None

        if not rows:
            await embed.edit_embed(contents="No data available")
        else:
            await embed.edit_embed(contents=f"{rows}")
