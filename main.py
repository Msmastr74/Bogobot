from bogobot_core import BotCore
import asyncio
import os
import sys

if os.path.exists('local_config.json'):
    bot = BotCore('local_config.json')
else:
    bot = BotCore()

async def start():
    async with bot:
        await bot.load_plugins("plugins")
        await bot.run_bot()

def log_fatal_exception(exc_type, exc, traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, traceback)
        return

    bot.logger.critical("Fatal error", exc_info=(exc_type, exc, traceback))

if __name__ == "__main__":
    sys.excepthook = log_fatal_exception

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        pass
