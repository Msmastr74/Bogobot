from bogobot_core import BotCore
import asyncio
import os

if os.path.exists('local_config.json'):
    bot = BotCore('local_config.json')
else:
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
