import asyncio
import os
import sys
from bogobot_core import BotCore
import asyncio.__main__  # type: ignore[reportMissingImports]
import site

async def async_setup():
    if os.path.exists('local_config.json'):
        bot = BotCore('local_config.json')
    else:
        bot = BotCore()
    await bot.__aenter__()
    await bot.load_plugins("plugins")
    return bot

# --- Cleaned Native CPython REPL Logic ---
def start_async_repl(bot):
    sys.audit("cpython.run_stdin")

    if os.getenv('PYTHON_BASIC_REPL'):
        CAN_USE_PYREPL = False # noqa: F401
    else:
        try:
            from _pyrepl.main import CAN_USE_PYREPL  # type: ignore[reportMissingImports] # noqa: F401
        except ImportError:
            CAN_USE_PYREPL = False

    return_code = 0
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Dictionary injection
    repl_locals = {'asyncio': asyncio, 'bot': bot}
    
    scope_source = {**globals(), **locals()}
    for key in {'__name__', '__package__', '__loader__', '__spec__', '__builtins__', '__file__'}:
        if key in scope_source:
            repl_locals[key] = scope_source[key]

    console = asyncio.__main__.AsyncIOInteractiveConsole(repl_locals, loop)

    asyncio.__main__.console = console
    asyncio.__main__.loop = loop
    asyncio.__main__.repl_future = None
    asyncio.__main__.keyboard_interrupted = False
    asyncio.__main__.CAN_USE_PYREPL = CAN_USE_PYREPL

    try:
        import readline  # NoQA
    except ImportError:
        readline = None

    interactive_hook = getattr(sys, "__interactivehook__", None)

    if interactive_hook is not None:
        sys.audit("cpython.run_interactivehook", interactive_hook)
        interactive_hook()

    if interactive_hook is site.register_readline:
        try:
            import rlcompleter
        except Exception:  # noqa: E722
            pass
        else:
            if readline is not None:
                completer = rlcompleter.Completer(console.locals)
                readline.set_completer(completer.complete)

    # FIXED: Using Python's default named handler prevents the racing thread exception
    repl_thread = asyncio.__main__.REPLThread()
    repl_thread.daemon = True
    repl_thread.start()

    while True:
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            asyncio.__main__.keyboard_interrupted = True
            if asyncio.__main__.repl_future and not asyncio.__main__.repl_future.done():
                asyncio.__main__.repl_future.cancel()
            repl_thread.interrupt()
            continue
        else:
            break

    console.write('exiting asyncio REPL...\n')
    loop.close()


if __name__ == "__main__":
    initialized_bot = asyncio.run(async_setup())
    
    try:
        start_async_repl(initialized_bot)
    finally:
        asyncio.run(initialized_bot.__aexit__(None, None, None))
