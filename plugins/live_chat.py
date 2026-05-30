from typing import TypedDict

import discord
import discord.backoff

from utils.monitoring import PersistentChannelMonitor

from bogobot_core import BotCore
from utils import groups, tasks
import pytchat
from pytchat.processors.default.processor import Chatdata
import time

TARGET_VIDEO_ID = "DgfiqGPmGWY"

class LiveChatView(discord.ui.LayoutView):
    def __init__(self, body: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Live Chat"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(body or "\u200d")
        ))

class LiveChatPayload(TypedDict):
    view: LiveChatView


chat = None
backoff = discord.backoff.ExponentialBackoff(base=2)
next_retry_at = 0.0
async def setup(bot: BotCore):
    manage = groups.manage(bot)
    log = bot.logger.getChild("LiveChatMonitor")

    def initial_payload() -> LiveChatPayload:
        return {"view": LiveChatView("")}

    def terminate_chat() -> None:
        global chat

        if chat is not None:
            try:
                chat.terminate()
            except Exception:
                pass

        chat = None


    def create_chat() -> None:
        global chat, backoff, next_retry_at

        terminate_chat()

        chat = pytchat.create(video_id=TARGET_VIDEO_ID, hold_exception=False)

        next_retry_at = 0.0

        log.info("pytchat connected.")


    def schedule_retry() -> None:
        global next_retry_at

        delay = backoff.delay()
        next_retry_at = time.monotonic() + delay

        log.info(f"pytchat reconnect scheduled in {delay:.1f}s")

    async def update_payload() -> LiveChatPayload | None:
        global chat

        now = time.monotonic()

        # Non-blocking retry delay
        if now < next_retry_at:
            return {"view": LiveChatView("")}

        try:
            if chat is None or not chat.is_alive():
                create_chat()
            assert chat is not None

            chat_data = chat.get()
            assert isinstance(chat_data, Chatdata)
            messages = []

            for msg in chat_data.sync_items():
                messages.append(f"{msg.author.name}: {msg.message}")

            return  {
                "view": LiveChatView("\n".join(messages))
            }

        except Exception as e:
            log.warning(f"pytchat error: {repr(e)}")

            terminate_chat()
            schedule_retry()

            return {"view": LiveChatView("")}
    
    @tasks.loop(seconds=5)
    async def update_chat_monitor():
        await chat_monitor.tick()
    
    chat_monitor = PersistentChannelMonitor(
        bot,
        storage_key="live_chat_monitor_messages",
        display_name="Live Chat Monitor",
        initial_payload=initial_payload,
        update_payload=update_payload,
    )
    chat_monitor.command(
        manage,
        name="live_chat",
        description="Manage live chat monitor",
    )

    @bot.init_callback
    async def init():
        try:
            create_chat()
        except Exception as e:
            log.warning(f"Initial pytchat connection failed: {repr(e)}")
            schedule_retry()
        await chat_monitor.initialize()
        update_chat_monitor.start()
    
    @bot.close_callback
    async def close():
        update_chat_monitor.cancel()
        terminate_chat()
