import time
from typing import TypedDict

import discord

from utils.monitoring import PersistentChannelMonitor

from bogobot_core import BotCore
from utils import groups

num_matrix: list[list[tuple[str, float]]] = [[] for _ in range(30)]


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

    def initial_payload() -> MonitorPayload:
        return {"view": MonitorView("Initializing...")}

    async def update_payload() -> MonitorPayload | None:
        global num_matrix

        new_vars, is_new = await bot.get_best_shuffles()

        if not is_new:
            return None

        num_matrix.pop(0)
        num_matrix.append([])

        for i, item in enumerate(new_vars):
            new_var, conf = item

            if conf <= 0 or new_var in ["0", "1", ""]:
                continue

            try:
                value = int(new_var)
            except ValueError:
                continue

            if value > 25:
                continue

            num_matrix[-i - 1].append((new_var.rjust(2, "0"), conf))

        num_array = [
            sublist[0][0] if sublist else "??"
            for sublist in num_matrix
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
        await stream_monitor.initialize()
        stream_monitor.start()
