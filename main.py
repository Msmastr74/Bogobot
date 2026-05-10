from bogobot_core import BotCore
import asyncio
import contextlib
import os
import sys

from plugins.healthcheck import start_fallback_healthcheck

if os.path.exists('local_config.json'):
    bot = BotCore('local_config.json')
else:
    bot = BotCore()

fallback_requested = False

async def start():
    global fallback_requested
    loop = asyncio.get_running_loop()

    async def stop_for_fallback():
        with contextlib.suppress(Exception):
            await bot.close()

    def log_loop_exception(loop, context):
        global fallback_requested

        exception = context.get("exception")
        message = context.get("message", "Unhandled asyncio exception")
        fallback_requested = True

        if exception is None:
            bot.logger.critical(message)
        else:
            bot.logger.critical(
                message,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

        loop.create_task(stop_for_fallback())

    loop.set_exception_handler(log_loop_exception)

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
    except Exception as e:
        bot.logger.critical(
            "Fatal error; starting fallback healthcheck client.",
            exc_info=(type(e), e, e.__traceback__),
        )
        asyncio.run(start_fallback_healthcheck(bot))
    else:
        if fallback_requested:
            bot.logger.critical("Starting fallback healthcheck client.")
            asyncio.run(start_fallback_healthcheck(bot))
