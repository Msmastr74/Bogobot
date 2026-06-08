import asyncio
from collections import deque
from typing import Protocol, TypedDict

import discord
import discord.backoff

from utils.monitoring import PersistentChannelMonitor

from bogobot_core import BotCore, TARGET_VIDEO_ID
from utils import groups, tasks
from utils.discord import count_characters, split_text_to_character_limit
import pytchat
from pytchat.processors.default.processor import Chatdata
import time

MAX_ITEMS = 30
MAX_LENGTH = 3900

class LiveChatView(discord.ui.LayoutView):
    def __init__(self, body: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Live Chat"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(body or "-# No messages yet")
        ))

class LiveChatPayload(TypedDict):
    view: LiveChatView

class EmojiBlock(TypedDict):
    id: str
    txt: str
    url: str

class ChatAuthorProtocol(Protocol):
    name: str
    channelId: str
    channelUrl: str
    imageUrl: str
    badgeUrl: str
    type: str
    isVerified: bool
    isChatOwner: bool
    isChatSponsor: bool
    isChatModerator: bool

class ChatItemProtocol(Protocol):
    id: str
    type: str
    timestamp: int
    elapsedTime: str
    datetime: str
    message: str
    messageEx: list[str | EmojiBlock]
    amountValue: float
    amountString: str
    currency: str
    bgColor: int
    author: ChatAuthorProtocol

def format_chat_item(c: ChatItemProtocol) -> str:
    discord_time = f"<t:{c.timestamp // 1000}:t>"
    role_tag = ""
    if c.author.isChatOwner:
        role_tag = " 👑"
    elif c.author.isChatModerator:
        role_tag = " 🛡️"
    elif c.author.isChatSponsor:
        role_tag = " ⭐"
    if c.type == "superChat":
        return f"{discord_time} 💰 **{c.author.name}{role_tag}** sent {c.amountString}: *{c.message}*"
    return f"{discord_time} **{c.author.name}{role_tag}** {c.message}"

chat = None
chat_buffer: deque[ChatItemProtocol] = deque(maxlen=MAX_ITEMS)
backoff = discord.backoff.ExponentialBackoff(base=2)
next_retry_at = 0.0

def format_chat_buffer() -> str:
    messages = [format_chat_item(msg) for msg in chat_buffer]
    while messages:
        body = "\n".join(messages)
        if count_characters(body) <= MAX_LENGTH:
            return body
        messages.pop(0)
    if not chat_buffer:
        return ""
    message = format_chat_item(chat_buffer[-1])
    pieces = split_text_to_character_limit(message, MAX_LENGTH, max_pieces=1)
    return pieces[0] if pieces else ""

async def setup(bot: BotCore):
    manage = groups.manage(bot)
    log = bot.logger.getChild("LiveChatMonitor")

    def initial_payload() -> LiveChatPayload:
        return {"view": LiveChatView(format_chat_buffer())}

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

    async def build_payload() -> LiveChatPayload | None:
        global chat

        now = time.monotonic()

        if now < next_retry_at:
            return None

        try:
            if chat is None or not chat.is_alive():
                create_chat()
            assert chat is not None

            chat_data = chat.get()
            assert isinstance(chat_data, Chatdata)

            chat_buffer.extend(chat_data.items)

            return {
                "view": LiveChatView(format_chat_buffer())
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"pytchat error: {repr(e)}")

            terminate_chat()
            schedule_retry()

            return None
    
    @tasks.loop(seconds=5)
    async def update_chat_monitor():
        payload = await build_payload()
        if payload is not None:
            await chat_monitor.update(payload)
    
    chat_monitor = PersistentChannelMonitor(
        bot,
        storage_key="live_chat_monitor_messages",
        display_name="Live Chat Monitor",
        initial_payload=initial_payload,
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
