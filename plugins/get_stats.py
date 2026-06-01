import io

import discord
import datetime

from typing import Iterable, TypedDict
from bogobot_core import BotCore
from PIL import Image
from utils.ai import action

from utils.monitoring import PersistentChannelMonitor
from utils import groups

class StatsView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title = "Bogostream Statistics",
        fields: Iterable[tuple[str, str]],
        updated_at: datetime.datetime | None = None
    ):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        field_container = discord.ui.Container()
        for header, content in fields:
            field_container.add_item(
                discord.ui.TextDisplay(f"**{header}**\n{content}")
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

class StatsPayload(TypedDict):
    view: StatsView

async def setup(bot: BotCore) -> None:
    manage = groups.manage(bot)
    
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
        api_fields: list[tuple[str, str]] = []
        if using_api_stats() or "engine_total" in stats_list or "crowd_total" in stats_list:
            api_fields = [
                ("Engine Total", stats_list.get("engine_total", "Loading...")),
                ("Crowd Total", stats_list.get("crowd_total", "Loading...")),
                ("Engine Rate", stats_list.get("engine_rate", "Loading...")),
                ("Crowd Rate", stats_list.get("crowd_rate", "Loading...")),
                ("Tick Best", stats_list.get("tick_best", "Loading...")),
                ("Tick Best Source", stats_list.get("tick_best_source", "Loading...")),
                ("Active Contributors", stats_list.get("active_contributors", "Loading...")),
                ("Record Holder", stats_list.get("record_holder", "Loading...")),
            ]
        
        updated_at = (
            datetime.datetime.fromtimestamp(bot._last_ocr_refresh)
            if bot._last_ocr_refresh > 0 else
            None
        )
        view = StatsView(
            title=title,
            fields=[
                ("Source", "Bogostream API" if using_api_stats() else "OCR"),
                ("Shuffles", shuffles),
                ("Comparisons", comparisons),
                ("Best Run", best_run),
                ("Shuffles Per Second", shuffles_sec),
                ("Average Best Shuffle", average_best_shuffle),
                ("Uptime [STREAM]", uptime),
                *api_fields,
                ("Elapsed Time [STATIC]", elapsed_time),
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

    last_frame: Image.Image | None = None
    last_value: tuple[
        list[tuple[bool, int]], int, float, Image.Image | None
    ] | None = None
    @bot.new_value_callback
    async def on_new_value(sort_state: list[tuple[bool, int]], correct_count: int, timestamp: float):
        nonlocal last_value
        last_value = (
            sort_state, correct_count, timestamp, last_frame
        )
        await stats_monitor.tick()

    @bot.new_frame_callback
    def on_new_frame(frame: Image.Image):
        nonlocal last_frame
        last_frame = frame
    
    async def sort_payload() -> tuple[SortView | None, discord.File | None]:
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
        "Show the current sort state.",
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
