import asyncio
import io
import time
from typing import Any, Iterable, TypedDict, cast

import aiohttp
import discord
import datetime

from bogobot_core import BotCore
from PIL import Image
from utils.ai import AIParam, action

from utils.monitoring import PersistentChannelMonitor
from utils import groups
from plugins.stats import SortSectionReader, format_duration

BOGOSTREAM_LEADERBOARD_API_URL = "https://bogo.swapjs.dev/api/leaderboard"
BOGOSTREAM_CONTRIBUTOR_API_URL = "https://bogo.swapjs.dev/api/contributor"
STREAMBOARD_LIMIT = 10
StatsFieldGroup = tuple[str | None, Iterable[tuple[str, str]]]


class StreamBadge(TypedDict, total=False):
    id: str
    name: str
    rarity: str
    held: bool
    edition: int | None
    value: int | None


class StreamboardEntry(TypedDict, total=False):
    nickname: str
    total: int
    rate: int
    devices: int
    badges: list[StreamBadge]


class StreamboardLeaderboard(TypedDict, total=False):
    top: list[StreamboardEntry]
    count: int
    sum_all: int
    view: str
    updated_at: int


class StreamContributor(TypedDict, total=False):
    nickname: str
    total: int
    all_time_best: int
    active_ms: int
    max_session_ms: int
    badges: list[StreamBadge]
    created_at: int


def format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "Unknown"


def datetime_from_epoch_ms(value: Any) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromtimestamp(int(value) / 1000)
    except (TypeError, ValueError, OSError):
        return None


def format_badges(badges: list[StreamBadge], *, held_only: bool = False) -> str:
    if held_only:
        badges = [
            badge
            for badge in badges
            if badge.get("held")
        ]
    if not badges:
        return "None"
    return ", ".join(
        badge.get("name") or badge.get("id") or "unknown"
        for badge in badges
    )

class StatsView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title = "Bogostream Statistics",
        groups: Iterable[StatsFieldGroup],
        updated_at: datetime.datetime | None = None
    ):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        field_container = discord.ui.Container()
        for index, (group_title, fields) in enumerate(groups):
            fields = list(fields)
            if not fields:
                continue
            if index and len(field_container.children) > 0:
                field_container.add_item(discord.ui.Separator())
            if group_title is not None:
                field_container.add_item(discord.ui.TextDisplay(f"### {group_title}"))
            field_container.add_item(
                discord.ui.TextDisplay("\n".join(
                    f"**{header}**\n{content}"
                    for header, content in fields
                ))
            )
        self.add_item(field_container)
        
        if updated_at is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(round(updated_at.timestamp()))}:T>"
            ))

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
        
        self.add_item(discord.ui.TextDisplay("## Bogosort Stream Sort State"))
        colors = (self.RED, self.GREEN)
        container = discord.ui.Container(
            discord.ui.TextDisplay(
                f"Current best shuffle in position: `{correct_count}/{total_count}`"
            ),
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
        self.add_item(container)

        if timestamp is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(round(timestamp.timestamp()))}:T>"
            ))


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

        updated_at = datetime_from_epoch_ms(max(
            int(total.get("updated_at", 0)),
            int(current.get("updated_at", 0)),
        ))
        if updated_at is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Updated at <t:{int(round(updated_at.timestamp()))}:T>"
            ))

    def _leaderboard_text(
        self,
        leaderboard: StreamboardLeaderboard,
        *,
        value_key: str,
    ) -> str:
        rows = leaderboard.get("top", [])
        if not rows:
            return "No contributors found."

        lines = [
            self._row(index, row, value_key=value_key)
            for index, row in enumerate(rows, start=1)
        ]
        sum_all = leaderboard.get("sum_all")
        if sum_all is not None:
            label = "Total" if value_key == "total" else "Combined rate"
            lines.append(f"\n{label}: `{format_count(sum_all)}`")
        return "\n".join(lines)

    def _row(self, index: int, row: StreamboardEntry, *, value_key: str) -> str:
        nickname = row.get("nickname", "unknown")
        value = format_count(row.get(value_key, 0))
        suffix = "/s" if value_key == "rate" else ""
        badges = format_badges(row.get("badges", []))
        devices = row.get("devices")
        devices_text = f" | Threads: `{devices}`" if devices is not None else ""
        return (
            f"{index}. **{nickname}** - `{value}{suffix}`"
            f"{devices_text}\n-# {badges}"
        )


class StreamContributorView(discord.ui.LayoutView):
    def __init__(self, contributor: StreamContributor) -> None:
        super().__init__(timeout=None)
        nickname = contributor.get("nickname", "unknown")
        self.add_item(discord.ui.TextDisplay(f"## {nickname}"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(self._body(contributor)),
        ))

        created_at = datetime_from_epoch_ms(contributor.get("created_at"))
        if created_at is not None:
            self.add_item(discord.ui.TextDisplay(
                f"-# Contributor since <t:{int(round(created_at.timestamp()))}:R>"
            ))

    def _body(self, contributor: StreamContributor) -> str:
        active_seconds = int(contributor.get("active_ms", 0)) // 1000
        max_session_seconds = int(contributor.get("max_session_ms", 0)) // 1000
        return "\n".join([
            f"Total: `{format_count(contributor.get('total', 0))}`",
            f"All-time best: `{contributor.get('all_time_best', 0)}`",
            f"Active time: `{format_duration(active_seconds)}`",
            f"Longest session: `{format_duration(max_session_seconds)}`",
            f"Badges: {format_badges(contributor.get('badges', []), held_only=True)}",
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

    def stats_payload(title="Bogostream Statistics Monitor") -> StatsPayload:
        stats_list = bot.stats

        # Use .get() to prevent future KeyErrors if the cache is empty
        shuffles = stats_list.get("shuffles", "Loading...")
        comparisons = stats_list.get("comparisons", "Loading...")
        best_run = stats_list.get("best_run", "Loading...")
        shuffles_sec = stats_list.get("shuffles_sec", "Loading...")
        average_best_shuffle = stats_list.get("average_best_shuffle", "Loading...")
        uptime = stats_list.get("uptime", "Loading...")
        elapsed_time = bot.get_stream_uptime()
        api_total_fields: list[tuple[str, str]] = []
        api_tick_fields: list[tuple[str, str]] = []
        api_contributor_fields: list[tuple[str, str]] = []
        if using_api_stats() or "engine_total" in stats_list or "crowd_total" in stats_list:
            api_total_fields = [
                ("Engine Total", stats_list.get("engine_total", "Loading...")),
                ("Crowd Total", stats_list.get("crowd_total", "Loading...")),
                ("Combined Total", stats_list.get("combined_total", "Loading...")),
                ("Engine Rate", stats_list.get("engine_rate", "Loading...")),
                ("Crowd Rate", stats_list.get("crowd_rate", "Loading...")),
            ]
            api_tick_fields = [
                ("Best At", stats_list.get("best_at", "Loading...")),
                ("Tick Best", stats_list.get("tick_best", "Loading...")),
                ("Tick Best Source", stats_list.get("tick_best_source", "Loading...")),
            ]
            api_contributor_fields = [
                ("Active Contributors", stats_list.get("active_contributors", "Loading...")),
                ("Record Holder", stats_list.get("record_holder", "Loading...")),
                ("Contributions Open", stats_list.get("contributions_open", "Loading...")),
                ("Solve Confirmed", stats_list.get("solve_confirmed", "Loading...")),
            ]
        
        updated_at = (
            datetime.datetime.fromtimestamp(bot._last_ocr_refresh)
            if bot._last_ocr_refresh > 0 else
            None
        )
        view = StatsView(
            title=title,
            groups=[
                (None, [
                    ("Source", "Bogostream API" if using_api_stats() else "OCR"),
                ]),
                ("Stream", [
                    ("Shuffles", shuffles),
                    ("Comparisons", comparisons),
                    ("Best Run", best_run),
                    ("Shuffles Per Second", shuffles_sec),
                    ("Average Best Shuffle", average_best_shuffle),
                ]),
                ("Bogostream API", api_total_fields),
                ("Recent Best", api_tick_fields),
                ("Contributors", api_contributor_fields),
                ("Timing", [
                    ("Uptime [STREAM]", uptime),
                    ("Elapsed Time [STATIC]", elapsed_time),
                ]),
            ],
            updated_at=updated_at,
        )
        return { 'view': view }
    
    @bot.setup.command(name="get_stats", description="Retrieve all current stream statistics", eph=False, perm_requirement=0)
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
        if not isinstance(data, dict) or not isinstance(data.get("top"), list):
            return None
        return cast(StreamboardLeaderboard, data)

    async def fetch_contributor(
        session: aiohttp.ClientSession,
        nickname: str,
    ) -> StreamContributor | None:
        data = await fetch_json(
            session,
            contributor_url,
            params={"nickname": nickname},
        )
        if not isinstance(data, dict) or not isinstance(data.get("nickname"), str):
            return None
        return cast(StreamContributor, data)

    @bot.setup.command(
        name="streamboard",
        description="Show Bogostream contributor rankings or a contributor profile",
        eph=False,
        perm_requirement=0,
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
        await stats_monitor.tick()

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

    @bot.setup.command(name="get_sort", description="Retrieve the current sort state", defer=False, perm_requirement=0)
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
        update_payload=stats_payload,
    )
    stats_monitor.command(
        manage,
        name="stats_monitor",
        description="Start or stop stats monitor in this channel",
    )
    
    @bot.init_callback
    async def init():
        await stats_monitor.initialize()
