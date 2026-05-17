import time
from typing import TypedDict

import discord

from utils.monitoring import PersistentChannelMonitor

from bogobot_core import BotCore
from utils import groups

num_matrix: list[str | None] = [None for _ in range(30)]


class MonitorView(discord.ui.LayoutView):
    def __init__(self, body: str):
        # Static LayoutViews with timeout=None avoid discord.py's dispatch
        # listener machinery because they contain no interactive components.
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Monitor"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(body or "\u200b"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("-# Oldest -> Newest [?? = Unknown]"),
        ))

class MonitorPayload(TypedDict):
    view: MonitorView


async def setup(bot: BotCore):
    manage = groups.manage(bot)
    pending_values: list[int] = []
    initialized = False

    def initial_payload() -> MonitorPayload:
        return {"view": MonitorView("Initializing...")}

    async def update_payload() -> MonitorPayload | None:
        global num_matrix

        if not pending_values:
            return None

        values = pending_values.copy()
        pending_values.clear()

        for value in values:
            num_matrix.pop(0)
            num_matrix.append(None)

            if value > bot.SORT_SECTION_COUNT:
                continue

            num_matrix[-1] = str(value).rjust(2, "0")

        num_array = [
            value if value is not None else "??"
            for value in num_matrix
        ]
        contents = f"```\n{'.'.join(num_array)}\n```"

        return {"view": MonitorView(f"<t:{int(round(time.time()))}:T>\n{contents}")}

    stream_monitor = PersistentChannelMonitor(
        bot,
        storage_key="monitor_messages",
        display_name="Monitor",
        initial_payload=initial_payload,
        update_payload=update_payload,
    )
    stream_monitor.command(
        manage,
        name="monitor",
        description="Start or stop stream monitoring in this channel",
    )

    @bot.init_callback
    async def init():
        nonlocal initialized
        await stream_monitor.initialize()
        initialized = True
        await stream_monitor.tick()

    @bot.new_value_callback
    async def new_value(value: int, timestamp: float):
        pending_values.append(value)
        if initialized:
            await stream_monitor.tick()
