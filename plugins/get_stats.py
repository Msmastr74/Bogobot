import asyncio
import io
import time
from typing import Any, Iterable, TypedDict

import aiohttp
import discord
import datetime
from pydantic import ValidationError

from bogobot_core import BotCore
from PIL import Image
from ai import AIParam, action

from utils.monitoring import PersistentChannelMonitor
from utils import groups
from plugins.stats import SortSectionReader, format_duration
from utils.schemas import (
    CachedStatsDisplay,
    StatValue,
    StreamContributor,
    StreamboardEntry,
    StreamboardLeaderboard,
)

BOGOSTREAM_LEADERBOARD_API_URL = "https://bogo.swapjs.dev/api/leaderboard"
BOGOSTREAM_CONTRIBUTOR_API_URL = "https://bogo.swapjs.dev/api/contributor"
STREAMBOARD_LIMIT = 10
StatsFieldGroup = tuple[str | None, Iterable[tuple[str, str]]]


def format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "Unknown"


class StatsView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title = "Bogostream Statistics",
        groups: Iterable[StatsFieldGroup],
        updated_at: datetime.datetime | None = None
    ):
        super().__init__(timeout=None)
        c = discord.ui.Container(
            discord.ui.TextDisplay(f"## {title}")
        )
        for index, (group_title, fields) in enumerate(groups):
            fields = list(fields)
            if not fields:
                continue
            if index and len(c.children) > 0:
                c.add_item(discord.ui.Separator())
            if group_title is not None:
                c.add_item(discord.ui.TextDisplay(f"### {group_title}"))
            c.add_item(
                discord.ui.TextDisplay("\n".join(
                    f"{header}: `{content}`"
                    for header, content in fields
                ))
            )
        if updated_at is not None:
            c.add_item(discord.ui.Separator())
            c.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(updated_at.timestamp())}:T>"
            ))
        self.add_item(c)

class SortView(discord.ui.LayoutView):
    RED = '\x1b[31m'
    GREEN = '\x1b[32m'
    RESET = '\x1b[0m'
    
    def __init__(
        self,
        *,
        sort_state: list[tuple[bool, int]],
        correct_count: int,
        total_count: int,
        image: discord.File | None = None,
        timestamp: datetime.datetime | None = None
    ):
        super().__init__(timeout=None)
        
        colors = (self.RED, self.GREEN)
        container = discord.ui.Container(
            discord.ui.TextDisplay("## Bogostream Sort State"),
            discord.ui.TextDisplay(
                f"Current best shuffle in position: `{correct_count}/{total_count}`"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "```ansi\n" +
                ' '.join(
                    map( # Brackets are for mobile - which doesn't support colours in ANSI
                        lambda t: f"{colors[t[0]]}{'[' if t[0] else ''}{t[1]}{']' if t[0] else ''}{self.RESET}"
                    , sort_state)
                ) + "\n```"
            ),
        )
        if image is not None:
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        image
                    )
                )
            )
        if timestamp is not None:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(timestamp.timestamp())}:T>"
            ))
        self.add_item(container)



class StreamboardView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        total: StreamboardLeaderboard,
        current: StreamboardLeaderboard,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Bogostream Leaderboard"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("### Current"),
            discord.ui.TextDisplay(self._leaderboard_text(current, value_key="rate")),
        ))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("### Total"),
            discord.ui.TextDisplay(self._leaderboard_text(total, value_key="total")),
        ))

        updated_at = max(
            total.updated_datetime(),
            current.updated_datetime(),
            key=lambda value: value.timestamp() if value is not None else 0,
        )
        if updated_at is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(updated_at.timestamp())}:T>"
            ))

    def _leaderboard_text(
        self,
        leaderboard: StreamboardLeaderboard,
        *,
        value_key: str,
    ) -> str:
        rows = leaderboard.top
        if not rows:
            return "No contributors found."

        lines = [
            self._row(index, row, value_key=value_key)
            for index, row in enumerate(rows, start=1)
        ]
        sum_all = leaderboard.sum_all
        if sum_all is not None:
            label = "Total" if value_key == "total" else "Combined rate"
            lines.append(f"\n{label}: `{format_count(sum_all)}`")
        return "\n".join(lines)

    def _row(self, index: int, row: StreamboardEntry, *, value_key: str) -> str:
        nickname = row.nickname
        value = format_count(getattr(row, value_key))
        suffix = "/s" if value_key == "rate" else ""
        badges = row.badges_text()
        devices = row.devices
        devices_text = f" | Threads: `{devices}`" if devices is not None else ""
        return (
            f"{index}. **{nickname}** - `{value}{suffix}`"
            f"{devices_text}\n-# {badges}"
        )


class StreamContributorView(discord.ui.LayoutView):
    def __init__(self, contributor: StreamContributor) -> None:
        super().__init__(timeout=None)
        c = discord.ui.Container(
            discord.ui.TextDisplay(f"## {contributor.nickname}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self._body(contributor)),
        )

        created_at = contributor.created_datetime()
        if created_at is not None:
            c.add_item(discord.ui.TextDisplay(
                f"-# Contributor since <t:{int(created_at.timestamp())}:R>"
            ))
        self.add_item(c)

    def _body(self, contributor: StreamContributor) -> str:
        active_seconds = contributor.active_ms // 1000
        max_session_seconds = contributor.max_session_ms // 1000
        return "\n".join([
            f"Total: `{format_count(contributor.total)}`",
            f"All-time best: `{contributor.all_time_best}`",
            f"Active time: `{format_duration(active_seconds)}`",
            f"Longest session: `{format_duration(max_session_seconds)}`",
            f"Badges: {contributor.badges_text(held_only=True)}",
        ])


class StatsPayload(TypedDict):
    view: StatsView

async def setup(bot: BotCore) -> None:
    manage = groups.manage(bot)
    streamboard_url = str(bot.config.get(
        "bogostream_leaderboard_api_url",
        BOGOSTREAM_LEADERBOARD_API_URL,
    ))
    contributor_url = str(bot.config.get(
        "bogostream_contributor_api_url",
        BOGOSTREAM_CONTRIBUTOR_API_URL,
    ))
    
    def using_api_stats() -> bool:
        stats_source = str(bot.config.get("stats_source", "api")).lower()
        return stats_source in {"api", "event", "events"}

    def cached_stats_display(
        stats: dict[str, StatValue],
        *,
        source: str,
        elapsed_time: str,
    ) -> CachedStatsDisplay:
        return CachedStatsDisplay.model_validate({
            **stats,
            "source": source,
            "elapsed_time": elapsed_time,
        })

    def stats_payload(title="Bogostream Statistics Monitor") -> StatsPayload:
        stats_list = bot.stats

        elapsed_time = bot.get_stream_uptime()
        display_model = cached_stats_display(
            stats_list,
            source="Bogostream API" if using_api_stats() else "OCR",
            elapsed_time=elapsed_time,
        )
        
        updated_at = (
            datetime.datetime.fromtimestamp(bot._last_ocr_refresh)
            if bot._last_ocr_refresh > 0 else
            None
        )
        view = StatsView(
            title=title,
            groups=display_model.groups(),
            updated_at=updated_at,
        )
        return { 'view': view }
    
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", eph=False)
    @action(
        "get_stats",
        "Show current stream statistics.",
    )
    async def get_stats(interaction: discord.Interaction):
        await bot.discord.send(
            **stats_payload(title="Bogostream Statistics"),
            response=True
        )

    async def fetch_json(
        session: aiohttp.ClientSession,
        url: str,
        *,
        params: dict[str, Any],
    ) -> Any | None:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                bot.logger.warning(f"Bogostream API returned HTTP {response.status} for {url}")
                return None
            return await response.json()

    async def fetch_streamboard(
        session: aiohttp.ClientSession,
        view: str,
    ) -> StreamboardLeaderboard | None:
        data = await fetch_json(
            session,
            streamboard_url,
            params={
                "view": view,
                "limit": STREAMBOARD_LIMIT,
            },
        )
        if data is None:
            return None
        try:
            return StreamboardLeaderboard.model_validate(data)
        except ValidationError:
            bot.logger.warning("Bogostream leaderboard API returned an unexpected payload shape")
            return None

    async def fetch_contributor(
        session: aiohttp.ClientSession,
        nickname: str,
    ) -> StreamContributor | None:
        data = await fetch_json(
            session,
            contributor_url,
            params={"nickname": nickname},
        )
        if data is None:
            return None
        try:
            return StreamContributor.model_validate(data)
        except ValidationError:
            bot.logger.warning("Bogostream contributor API returned an unexpected payload shape")
            return None

    @bot.setup.command(
        name="streamboard",
        description="Show Bogostream contributor rankings or a contributor profile",
        eph=False,
    )
    @action(
        "streamboard",
        "Show the Bogostream contributor leaderboard, or a contributor profile by nickname.",
        params={
            "user": AIParam("Optional Bogostream contributor nickname.", type=str | None, required=False, default=None),
        },
    )
    async def streamboard(interaction: discord.Interaction, user: str | None = None):
        await bot.discord.defer()
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if user:
                contributor = await fetch_contributor(session, user)
                if contributor is None:
                    await bot.discord.send(
                        f"No Bogostream contributor found for `{discord.utils.escape_markdown(user)}`.",
                        response=True,
                        ephemeral=True,
                    )
                    return
                await bot.discord.send(
                    view=StreamContributorView(contributor),
                    response=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            total, current = await asyncio.gather(
                fetch_streamboard(session, "total"),
                fetch_streamboard(session, "current"),
            )
            if total is None or current is None:
                await bot.discord.send(
                    "Bogostream leaderboard data is not available right now.",
                    response=True,
                    ephemeral=True,
                )
                return
            await bot.discord.send(
                view=StreamboardView(total=total, current=current),
                response=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    last_frame: Image.Image | None = None
    last_frame_ts: float = 0
    last_value: tuple[
        list[tuple[bool, int]], int, float, Image.Image | None
    ] | None = None
    sort_reader = SortSectionReader(bot)

    @bot.new_value_callback
    async def on_new_value(sort_state: list[tuple[bool, int]], correct_count: int, timestamp: float):
        await stats_monitor.update(stats_payload())

    @bot.new_frame_callback
    def on_new_frame(frame: Image.Image):
        nonlocal last_frame, last_frame_ts
        last_frame = frame
        last_frame_ts = time.time()
    
    async def sort_payload() -> tuple[SortView | None, discord.File | None]:
        nonlocal last_value

        frame = last_frame
        if frame is None:
            return None, None

        sort_changed, best_shuffle_sections, sort_values, new_values = sort_reader.analyze(frame)
        if sort_changed or last_value is None:
            last_value = (
                new_values,
                sum(best_shuffle_sections),
                last_frame_ts,
                frame,
            )

        if last_value is None:
            return None, None

        sort_state, correct_count, timestamp, frame = last_value
        file: discord.File | None = None
        if frame:
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            buffer.seek(0)
            file = discord.File(buffer, filename=f"sort_{timestamp}.png")

        total_count = len(sort_state)
        return SortView(
            sort_state=sort_state,
            correct_count=correct_count,
            total_count=total_count,
            timestamp=datetime.datetime.fromtimestamp(timestamp),
            image=file
        ), file

    @bot.setup.command(name="get_sort", description="Retrieve the current sort state", defer=False)
    @action(
        "get_sort",
        "Show the latest color-extracted sort state from the cached video frame.",
    )
    async def get_sort(interaction: discord.Interaction):
        view, file = await sort_payload()
        if view is None:
            await bot.discord.send(
                "No sort data available yet.",
                response=True,
                ephemeral=True
            )
            return

        await bot.discord.send(
            view=view,
            files=[file] if file else None,
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    stats_monitor = PersistentChannelMonitor(
        bot,
        storage_key="stats_monitor_messages",
        display_name="Stats monitor",
        initial_payload=stats_payload,
    )
    stats_monitor.command(
        manage,
        name="stats_monitor",
        description="Start or stop stats monitor in this channel",
    )
    
    @bot.init_callback
    async def init():
        await stats_monitor.initialize()
