import time
from typing import Any, TYPE_CHECKING, TypedDict

import aiohttp
import discord

from utils.monitoring import PersistentChannelMonitor

if TYPE_CHECKING:
    from main import BotCore


LEADERBOARD_URL = "https://swapjs.dev/api/group/leaderboard"
LEADERBOARD_LIMIT = 10
LEADERBOARD_MONITOR_INTERVAL_SECONDS = 120

RANK_EMOJIS = {
    "champion": "<:champion:1498536682046357644>",
    "grandmaster": "<:grandmaster:1498536634164051978>",
    "master": "<:master:1498537542104907858>",
    "diamond": "<:diamond:1498537469124018186>",
    "platinum": "<:platinum:1498537400828166194>",
    "gold": "<:gold:1498537254895878295>",
    "silver": "<:silver:1498535347834060800>",
    "bronze": "<:bronze:1498896875728666654>",
}


class LeaderboardView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        subtitle: str,
        rows: list[dict[str, Any]],
        updated_at: int | None = None,
        limit: int = LEADERBOARD_LIMIT,
    ):
        super().__init__(timeout=None)

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

    def _body(self, rows: list[dict[str, Any]], *, limit: int) -> str:
        if not rows:
            return "No leaderboard data available."

        return "\n".join(
            self._row(player)
            for player in rows[:limit]
        )

    def _row(self, player: dict[str, Any]) -> str:
        rank_emoji = RANK_EMOJIS.get(str(player.get("rank", "")).lower(), "-")
        pos = player.get("pos", "?")
        name = player.get("name", "Unknown")
        elo = player.get("elo", 0)
        win_rate = player.get("win_rate", 0)

        try:
            streak = int(player.get("current_streak", "0"))
        except (TypeError, ValueError):
            streak = 0

        streak_text = ""
        if streak > 0:
            streak_text = f" | Win streak: {streak}"
        elif streak < 0:
            streak_text = f" | Loss streak: {abs(streak)}"

        return (
            f"{pos}. {rank_emoji} **{name}** - "
            f"ELO: {elo} | Win Rate: {win_rate}%{streak_text}"
        )

class LeaderboardPayload(TypedDict):
    view: LeaderboardView


async def setup(bot: "BotCore"):
    from utils import groups

    manage = groups.manage(bot)

    async def fetch_leaderboard() -> list[dict[str, Any]]:
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
        rows: list[dict[str, Any]],
        *,
        title: str = "Leaderboard",
        subtitle: str = "Top players ranked by ELO",
        updated_at: int | None = None,
    ) -> LeaderboardPayload:
        return {
            "view": LeaderboardView(
                title=title,
                subtitle=subtitle,
                rows=rows,
                updated_at=updated_at,
            )
        }

    async def top_payload():
        return leaderboard_payload(
            await fetch_leaderboard(),
            updated_at=int(time.time()),
        )

    @bot.setup.command(
        name="top",
        description="Gets the top 10 players in sortoffs!",
        eph=False,
        perm_requirement=0,
    )
    async def top(interaction: discord.Interaction):
        await bot.discord.send(
            response=True,
            **await top_payload(),
        )

    @bot.setup.command(
        name="bottom",
        description="Gets the bottom 10 players in sortoffs!",
        eph=False,
        perm_requirement=0,
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

    @bot.setup.command(
        name="middle",
        description="Gets the middle 10 players in sortoffs!",
        eph=False,
        perm_requirement=0,
    )
    async def middle(interaction: discord.Interaction):
        rows = await fetch_leaderboard()
        middle_index = len(rows) // 2
        start = max(0, middle_index - (LEADERBOARD_LIMIT // 2))
        await bot.discord.send(
            response=True,
            **leaderboard_payload(
                rows[start:start + LEADERBOARD_LIMIT],
                subtitle="Middle players ranked by ELO",
                updated_at=int(time.time()),
            ),
        )

    leaderboard_monitor = PersistentChannelMonitor(
        bot,
        storage_key="leaderboard_monitor_messages",
        display_name="Leaderboard monitor",
        initial_payload=top_payload,
        update_payload=top_payload,
        interval_seconds=LEADERBOARD_MONITOR_INTERVAL_SECONDS,
    )
    leaderboard_monitor.command(
        manage,
        name="leaderboard_monitor",
        description="Start or stop leaderboard monitoring in this channel",
    )

    @bot.init_callback
    async def init():
        await leaderboard_monitor.initialize()
        leaderboard_monitor.start()
