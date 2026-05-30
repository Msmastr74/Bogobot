from typing import Any, Awaitable, Callable

import discord

from bogobot_core import BotCore, current_interaction
from utils.ai import AIParam, action
from utils.discord import chunk_text
from utils.sandboxed_executor import SandboxedExecutor, PythonLanguage, JavascriptLanguage

BLANK_CHAR = "\u200d"
async def setup(bot: BotCore) -> None:
    python_executor = SandboxedExecutor(
        fuel=int(bot.config.get("code_sandbox_fuel", 25_000_000_000)),
        language=PythonLanguage()
    )
    @bot.setup.command("python", description="Execute python code.", perm_requirement=0, defer=False)
    @action(
        "python",
        "Execute python code in a sandboxed environment — output will only be shown to the user.",
        params={
            "code": AIParam("Code to execute.")
        }
    )
    async def python(interaction: discord.Interaction, code: str | None = None):
        if code is None:
            await interaction.response.send_modal(ProgramInputModal(
                lambda code: execute_code(code, python_executor), "Javascript Code"
            ))
            return
        await execute_code(code, python_executor)
    js_executor = SandboxedExecutor(
        fuel=int(bot.config.get("code_sandbox_fuel", 25_000_000_000)),
        language=JavascriptLanguage()
    )
    @bot.setup.command("javascript", description="Execute javascript code.", perm_requirement=0, defer=False)
    @action(
        "javascript",
        "Execute javascript code in a sandboxed environment — output will only be shown to the user.",
        params={
            "code": AIParam("Code to execute.")
        }
    )
    async def javascript(interaction: discord.Interaction, code: str | None = None):
        if code is None:
            await interaction.response.send_modal(ProgramInputModal(
                lambda code: execute_code(code, js_executor), "Javascript Code"
            ))
            return
        await execute_code(code, js_executor)
    
    async def execute_code(code: str, executor: SandboxedExecutor):
        await bot.discord.defer(ephemeral=False)
        try:
            result = await executor.execute(code)
            chunks = chunk_text(result, 3900, max_chunks=3) or [""]
            clen = 0
            for chunk in chunks:
              view = discord.ui.LayoutView(timeout=None)
              view.add_item(discord.ui.TextDisplay(f"```ansi\n{chunk or BLANK_CHAR}\n```"))
              clen += len(chunk)
              await bot.discord.send(view=view, response=True, safety_filter=True)
            if clen < len(result):
              await bot.discord.send("`[TRUNCATED]`", response=True)
        except Exception as e:
            await bot.discord.send(f"{type(e).__name__}: {e}", response=True, safety_filter=True)
    
    class ProgramInputModal(discord.ui.Modal, title="Program"):
        code = discord.ui.TextInput(
            label="Code",
            style=discord.TextStyle.paragraph,
            required=True
        )
        def __init__(self, callback: Callable[[str], Awaitable[Any]], label: str) -> None:
            super().__init__()
            self.code.label = label
            self.callback = callback
        
        async def on_submit(self, interaction: discord.Interaction):
            token = current_interaction.set(interaction)
            await bot.discord.defer(ephemeral=False)
            try:
                await self.callback(self.code.value)
            finally:
                current_interaction.reset(token)
