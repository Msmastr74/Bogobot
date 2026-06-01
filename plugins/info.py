import discord
from discord import app_commands

from bogobot_core import BotCore
from utils.discord import count_characters


BOGOBOT_DESCRIPTION = (
    "Bogobot is a specialized Discord bot designed for monitoring the "
    "[24/7 Bogosort Livestream](https://www.youtube.com/live/7Y5eyyUNsYo). "
    "The bot now uses the Bogostream stats API by default, with OCR still "
    "available as a fallback pipeline for stream-derived statistics and "
    "sort-state tracking."
)
MAX_COMMAND_TEXT_CHARACTERS = 3900


class HelpBox(discord.ui.LayoutView):
    def __init__(
        self,
        commands: list[app_commands.Command],
        context_menus: list[app_commands.ContextMenu],
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Bogobot"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(BOGOBOT_DESCRIPTION),
            discord.ui.TextDisplay(self._command_text(commands, context_menus)),
        ))

    def _command_text(
        self,
        commands: list[app_commands.Command],
        context_menus: list[app_commands.ContextMenu],
    ) -> str:
        lines = [
            f"`/{command.qualified_name}` - {command.description}"
            for command in sorted(commands, key=lambda command: command.qualified_name)
        ]
        context_lines = [
            f"`{context_menu.name}`"
            for context_menu in sorted(context_menus, key=lambda context_menu: context_menu.name)
        ]
        if context_lines:
            lines = [
                *lines,
                "",
                "### Context Menu",
                *context_lines,
            ]
        text = "\n".join(lines) or "No commands available."
        if count_characters(text) <= MAX_COMMAND_TEXT_CHARACTERS:
            return text

        kept: list[str] = []
        total = 0
        omitted = 0
        for line in lines:
            next_total = total + count_characters(line) + (1 if kept else 0)
            if next_total > MAX_COMMAND_TEXT_CHARACTERS - 80:
                omitted += 1
                continue
            kept.append(line)
            total = next_total

        footer = f"...and {omitted} more commands."
        return "\n".join([*kept, footer])


async def setup(bot: BotCore):
    @bot.setup.command(
        name="help",
        description="Show bot information and commands",
        perm_requirement=0,
        defer=False,
        eph=True,
    )
    async def help(interaction: discord.Interaction):
        commands = [
            command
            for command in bot.tree.walk_commands()
            if isinstance(command, app_commands.Command)
        ]
        context_menus = [
            command
            for command in bot.tree.get_commands()
            if isinstance(command, app_commands.ContextMenu)
        ]
        await bot.discord.send(
            view=HelpBox(commands, context_menus),
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
