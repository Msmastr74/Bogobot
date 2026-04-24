from bogobot_core import BotCore
import asyncio
import sys
import importlib
import io
import contextlib

bot = BotCore()

async def start():
    async with bot:
        await bot.load_plugins("plugins")
        await bot.run_bot()

if __name__ == "__main__":
    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        pass