import time
from typing import TypedDict

import aiohttp
import discord
from pydantic import ValidationError

from utils.monitoring import PersistentChannelMonitor
from utils import groups, tasks
from ai import AIParam, action
from utils.schemas import SortoffsLeaderboard, SortoffsPlayer

from bogobot_core import BotCore

LEADERBOARD_URL = "https://swapjs.dev/api/group/leaderboard"
LEADERBOARD_LIMIT = 25
LEADERBOARD_MONITOR_INTERVAL_SECONDS = 120

class LeaderboardView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        subtitle: str,
        rows: list[SortoffsPlayer],
        updated_at: int | None = None,
        limit: int = LEADERBOARD_LIMIT,
        bot: BotCore
    ):
        super().__init__(timeout=None)
        
        self.bot = bot

        c = discord.ui.Container(
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.TextDisplay(subtitle),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self._body(rows, limit=limit))
        )

        footer = "Data from swapjs.dev"
        if updated_at is not None:
            footer = f"{footer} - Updated <t:{updated_at}:R>"
        c.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        self.add_item(c)

    def _body(self, rows: list[SortoffsPlayer], *, limit: int) -> str:
        if not rows:
            return "No leaderboard data available."

        return "\n".join(
            self._row(player)
            for player in rows[:limit]
        )

    def _row(self, player: SortoffsPlayer) -> str:
        pos = player.pos
        rank = "grandchampion" if pos == 1 else player.rank
        rank_emoji = self.bot.discord.get_emoji(rank)
        streak = player.current_streak

        streak_text = ""
        if streak > 0:
            streak_text = f" | 🔥 {streak}"
        elif streak < 0:
            streak_text = f" | ❄️ {abs(streak)}"

        return (
            f"{pos}. {rank_emoji} **{player.name}** - "
            f"ELO: {player.elo} | Win Rate: {player.win_rate}%{streak_text}"
        )

class LeaderboardPayload(TypedDict):
    view: LeaderboardView

async def setup(bot: BotCore):
    manage = groups.manage(bot)

    async def fetch_leaderboard() -> list[SortoffsPlayer]:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(LEADERBOARD_URL) as resp:
                    if resp.status == 200:
                        try:
                            return SortoffsLeaderboard.model_validate(await resp.json()).rows
                        except ValidationError:
                            bot.logger.warning("Leaderboard API returned an unexpected payload shape")
                            return []
                    bot.logger.warning(f"Leaderboard fetch failed with HTTP {resp.status}")
        except Exception as e:
            bot.logger.warning(f"Error fetching leaderboard: {e}")
        return []
    
    def leaderboard_payload(
        rows: list[SortoffsPlayer],
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
    )
    @action(
        "leaderboard",
        "Show a leaderboard rank range.",
        params={
            "a": AIParam("One rank boundary from 1 to 100.", int),
            "b": AIParam("The other rank boundary from 1 to 100.", int)
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
        capability="monitor.leaderboard",
    )
    leaderboard_monitor.command(
        manage,
        name="leaderboard_monitor",
        description="Manage leaderboard monitor",
    )

    @tasks.loop(seconds=LEADERBOARD_MONITOR_INTERVAL_SECONDS)
    async def update_leaderboard_monitor():
        await leaderboard_monitor.update(await monitor_payload())

    @bot.init_callback
    async def init():
        await leaderboard_monitor.initialize()
        if not update_leaderboard_monitor.is_running():
            update_leaderboard_monitor.start()

    @bot.close_callback
    async def close():
        if update_leaderboard_monitor.is_running():
            update_leaderboard_monitor.cancel()
