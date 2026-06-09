import asyncio
import io
from typing import Any, Awaitable, Callable

import discord

from bogobot_core import BotCore, current_interaction
from utils.ai import AIParam, action
from utils.discord import chunk_text
from utils.sandboxed_executor import Language, SandboxedExecutor, PythonLanguage, JavascriptLanguage

BLANK_CHAR = "\u200d"
SOURCE_EXTENSIONS = {
    "javascript": "js",
    "python": "py",
}


def source_filename(language: Language) -> str:
    extension = SOURCE_EXTENSIONS.get(language.name, "txt")
    return f"program.{extension}"


def source_file(language: Language, code: str) -> discord.File:
    return discord.File(
        io.BytesIO(code.encode("utf-8")),
        filename=source_filename(language),
    )


async def setup(bot: BotCore) -> None:
    fuel = int(bot.config.get("code_sandbox_fuel", 25_000_000_000))
    languages = [
        PythonLanguage(),
        JavascriptLanguage()
    ]
    execution_lock = asyncio.Lock()
    def setup_language(language: Language, fuel: int):
        executor = SandboxedExecutor(
            fuel=fuel,
            language=language
        )
        @bot.setup.command(language.name, description=f"Execute {language.name} code.", defer=False)
        @action(
            language.name,
            f"Execute {language.name} code in a sandboxed environment — output will only be shown to the user.",
            params={
                "code": AIParam("Code to execute.")
            }
        )
        async def command(interaction: discord.Interaction, code: str | None = None):
            if code is None:
                await interaction.response.send_modal(ProgramInputModal(
                    bot,
                    callback=lambda code: execute_code(code, executor),
                    label_text=f"{language.name.capitalize()} code"
                ))
                return
            await execute_code(code, executor)
    for language in languages:
        setup_language(language, fuel)

    async def execute_code(code: str, executor: SandboxedExecutor):
        await bot.discord.defer(ephemeral=False)
        async with execution_lock:
            try:
                result = await executor.execute(code)
                chunks = chunk_text(result, 3900, max_chunks=3) or [""]
                clen = 0
                for index, chunk in enumerate(chunks):
                    view = discord.ui.LayoutView(timeout=None)
                    view.add_item(discord.ui.TextDisplay(f"```ansi\n{chunk or BLANK_CHAR}\n```"))
                    if index == 0:
                        view.add_item(discord.ui.File(f"attachment://{source_filename(executor.language)}"))
                    clen += len(chunk)
                    kwargs: dict[str, Any] = {
                        'file': source_file(executor.language, code)
                    } if index == 0 else {}
                    await bot.discord.send(
                        view=view,
                        response=True,
                        safety_filter=True,
                        **kwargs
                    )
                if clen < len(result):
                    await bot.discord.send("`[TRUNCATED]`", response=True)
            except Exception as e:
                await bot.discord.send(
                    f"{type(e).__name__}: {e}",
                    response=True,
                    safety_filter=True,
                    file=source_file(executor.language, code),
                )

class ProgramInputModal(discord.ui.Modal, title="Program"):
    def __init__(
        self,
        bot: BotCore,
        *,
        callback: Callable[[str], Awaitable[Any]],
        label_text: str
    ) -> None:
        super().__init__()
        self.bot = bot
        self.callback = callback
        self.code = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.file_upload = discord.ui.FileUpload(
            required=False,
            min_values=0,
            max_values=1,
        )
        self.add_item(discord.ui.Label(text=label_text, component=self.code))
        self.add_item(discord.ui.Label(
            text="Program file", component=self.file_upload,
            description=(
                "If provided, the file contents are inserted before the inline code. "
            ),
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        token = current_interaction.set(interaction)
        await self.bot.discord.defer(ephemeral=False)

        try:
            try:
                code = await self._read_code()
            except ValueError as exc:
                await self.bot.discord.send(
                    str(exc),
                    response=True,
                    ephemeral=True,
                )
                return
            
            if not code.strip():
                await self.bot.discord.send(
                    "Provide code in the text box or upload a source file.",
                    response=True,
                    ephemeral=True,
                )
                return

            await self.callback(code)

        finally:
            current_interaction.reset(token)

    async def _read_code(self) -> str:
        code = ""
        if self.file_upload.values:
            attachment = self.file_upload.values[0]
            data = await attachment.read()
            code += data.decode("utf-8", errors="ignore")
        if self.code.value:
            if code:
                code += "\n"
            code += self.code.value
        return code
