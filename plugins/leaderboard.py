import time
from typing import TypedDict

import aiohttp
import discord

from utils.monitoring import PersistentChannelMonitor
from utils import groups, tasks
from utils.ai import AIParam, action

from bogobot_core import BotCore

class Player(TypedDict, total=False):
    pos: int
    name: str
    elo: int
    peak_elo: int
    rank: str
    games_played: int
    win_rate: int
    current_streak: int
    max_win_streak: int

LEADERBOARD_URL = "https://swapjs.dev/api/group/leaderboard"
LEADERBOARD_LIMIT = 25
LEADERBOARD_MONITOR_INTERVAL_SECONDS = 120

class LeaderboardView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        subtitle: str,
        rows: list[Player],
        updated_at: int | None = None,
        limit: int = LEADERBOARD_LIMIT,
        bot: BotCore
    ):
        super().__init__(timeout=None)
        
        self.bot = bot

        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        self.add_item(discord.ui.TextDisplay(subtitle))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(self._body(rows, limit=limit)),
            accent_colour=discord.Color.gold(),
        ))

        footer = "Data from swapjs.dev"
        if updated_at is not None:
            footer = f"{footer} - Updated <t:{updated_at}:R>"
        self.add_item(discord.ui.TextDisplay(f"-# {footer}"))

    def _body(self, rows: list[Player], *, limit: int) -> str:
        if not rows:
            return "No leaderboard data available."

        return "\n".join(
            self._row(player)
            for player in rows[:limit]
        )

    def _row(self, player: Player) -> str:
        pos = player.get("pos", "?")
        rank = "grandchampion" if pos == 1 else player.get("rank", "")
        rank_emoji = self.bot.discord.get_emoji(rank)
        name = player.get("name", "Unknown")
        elo = player.get("elo", 0)
        win_rate = player.get("win_rate", 0)

        try:
            streak = int(player.get("current_streak", "0"))
        except (TypeError, ValueError):
            streak = 0

        streak_text = ""
        if streak > 0:
            streak_text = f" | 🔥 {streak}"
        elif streak < 0:
            streak_text = f" | ❄️ {abs(streak)}"

        return (
            f"{pos}. {rank_emoji} **{name}** - "
            f"ELO: {elo} | Win Rate: {win_rate}%{streak_text}"
        )

class LeaderboardPayload(TypedDict):
    view: LeaderboardView

async def setup(bot: BotCore):
    manage = groups.manage(bot)

    async def fetch_leaderboard() -> list[Player]:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(LEADERBOARD_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rows = data.get("rows", [])
                        return rows if isinstance(rows, list) else []
                    bot.logger.warning(f"Leaderboard fetch failed with HTTP {resp.status}")
        except Exception as e:
            bot.logger.warning(f"Error fetching leaderboard: {e}")
        return []
    
    def leaderboard_payload(
        rows: list[Player],
        *,
        title: str = "Leaderboard",
        subtitle: str = "Top players ranked by ELO",
        updated_at: int | None = None,
        **kwargs
    ) -> LeaderboardPayload:
        return {
            "view": LeaderboardView(
                title=title,
                subtitle=subtitle,
                rows=rows,
                updated_at=updated_at,
                bot=bot,
                **kwargs
            )
        }

    async def monitor_payload():
        return leaderboard_payload(
            await fetch_leaderboard(),
            updated_at=int(time.time()),
            title="Leaderboard Monitor"
        )
    
    @bot.setup.command(
        name="leaderboard",
        description="Gets the leaderboard in sortoffs!",
        defer=False,
        perm_requirement=0,
    )
    @action(
        "leaderboard",
        "Show the leaderboard.",
        params={
            "a": AIParam("A leaderboard boundary from 1 to 100.", int),
            "b": AIParam("A leaderboard boundary from 1 to 100.", int)
        }
    )
    async def leaderboard(interaction: discord.Interaction, a: int, b: int):
        if a < 1 or a > 100 or b < 1 or b > 100:
            await bot.discord.send(
                "Leaderboard boundaries must be between 1 and 100.",
                response=True, ephemeral=True
            )
            return
        await bot.discord.defer()
        rows = await fetch_leaderboard()
        await bot.discord.send(
            response=True,
            **leaderboard_payload(
                rows[min(a, b) - 1:max(a, b)],
                subtitle=f"{min(a, b)} to {max(a, b)} players ranked by ELO",
                updated_at=int(time.time()),
                limit=40
            ),
        )

    @bot.setup.command(
        name="top",
        description=f"Gets the top {LEADERBOARD_LIMIT} players in sortoffs!",
        eph=False,
        perm_requirement=0,
    )
    @action(
        "top",
        "Show the top leaderboard.",
    )
    async def top(interaction: discord.Interaction):
        await bot.discord.send(
            response=True,
            **leaderboard_payload(
                await fetch_leaderboard(),
                updated_at=int(time.time())
            )
        )

    @bot.setup.command(
        name="bottom",
        description=f"Gets the bottom {LEADERBOARD_LIMIT} players in sortoffs!",
        eph=False,
        perm_requirement=0,
    )
    @action(
        "bottom",
        "Show the bottom leaderboard.",
    )
    async def bottom(interaction: discord.Interaction):
        rows = await fetch_leaderboard()
        await bot.discord.send(
            response=True,
            **leaderboard_payload(
                rows[-LEADERBOARD_LIMIT:],
                subtitle="Bottom players ranked by ELO",
                updated_at=int(time.time()),
            ),
        )

    leaderboard_monitor = PersistentChannelMonitor(
        bot,
        storage_key="leaderboard_monitor_messages",
        display_name="Leaderboard monitor",
        initial_payload=monitor_payload,
        update_payload=monitor_payload,
    )
    leaderboard_monitor.command(
        manage,
        name="leaderboard_monitor",
        description="Manage leaderboard monitor",
    )

    @tasks.loop(seconds=LEADERBOARD_MONITOR_INTERVAL_SECONDS)
    async def update_leaderboard_monitor():
        await leaderboard_monitor.tick()

    @bot.init_callback
    async def init():
        await leaderboard_monitor.initialize()
        if not update_leaderboard_monitor.is_running():
            update_leaderboard_monitor.start()

    @bot.close_callback
    async def close():
        if update_leaderboard_monitor.is_running():
            update_leaderboard_monitor.cancel()
