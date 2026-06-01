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
MAX_COMMAND_CHOICES = 25


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


class CommandSignatureBox(discord.ui.LayoutView):
    def __init__(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Bogobot"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"`{self._signature(command)}`"),
            discord.ui.TextDisplay(self._description(command)),
        ))

    def _display_name(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> str:
        if isinstance(command, app_commands.Command):
            return f"/{command.qualified_name}"
        return command.name

    def _signature(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> str:
        if isinstance(command, app_commands.ContextMenu):
            return command.name

        parts = [f"/{command.qualified_name}"]
        for parameter in command.parameters:
            optional = "" if parameter.required else "?"
            parts.append(
                f"{parameter.display_name}: {self._parameter_type(parameter)}{optional}"
            )
        return " ".join(parts)

    def _description(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> str:
        if isinstance(command, app_commands.ContextMenu):
            return "Context menu command."
        return command.description

    def _parameter_type(self, parameter: app_commands.commands.Parameter) -> str:
        if parameter.choices:
            choices = " | ".join(str(choice.name) for choice in parameter.choices)
            return choices
        match parameter.type:
            case discord.AppCommandOptionType.string:
                return "str"
            case discord.AppCommandOptionType.integer:
                return "int"
            case discord.AppCommandOptionType.boolean:
                return "bool"
            case discord.AppCommandOptionType.number:
                return "float"
            case discord.AppCommandOptionType.user:
                return "user"
            case discord.AppCommandOptionType.channel:
                return "channel"
            case discord.AppCommandOptionType.role:
                return "role"
            case discord.AppCommandOptionType.mentionable:
                return "mentionable"
            case discord.AppCommandOptionType.attachment:
                return "attachment"
            case _:
                return parameter.type.name


def normalize_command_name(name: str) -> str:
    return " ".join(name.strip().removeprefix("/").split()).casefold()


def all_help_commands(
    bot: BotCore,
) -> tuple[list[app_commands.Command], list[app_commands.ContextMenu]]:
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
    return commands, context_menus


def find_help_command(
    commands: list[app_commands.Command],
    context_menus: list[app_commands.ContextMenu],
    name: str,
) -> app_commands.Command | app_commands.ContextMenu | None:
    normalized = normalize_command_name(name)
    for command in commands:
        if normalize_command_name(command.qualified_name) == normalized:
            return command
    for context_menu in context_menus:
        if normalize_command_name(context_menu.name) == normalized:
            return context_menu
    return None


def command_choices(
    commands: list[app_commands.Command],
    context_menus: list[app_commands.ContextMenu],
    current: str,
) -> list[app_commands.Choice[str]]:
    normalized_current = normalize_command_name(current)
    names = [
        f"/{command.qualified_name}"
        for command in commands
    ] + [
        context_menu.name
        for context_menu in context_menus
    ]
    matches = [
        name
        for name in sorted(names, key=str.casefold)
        if normalized_current in normalize_command_name(name)
    ]
    return [
        app_commands.Choice(name=name, value=name)
        for name in matches[:MAX_COMMAND_CHOICES]
    ]


async def setup(bot: BotCore):
    @bot.setup.command(
        name="help",
        description="Show bot information and commands",
        perm_requirement=0,
        defer=False,
        eph=True,
    )
    async def help(interaction: discord.Interaction, command: str | None = None):
        commands, context_menus = all_help_commands(bot)
        if command is not None:
            matched_command = find_help_command(commands, context_menus, command)
            if matched_command is None:
                await bot.discord.send(
                    f"No command found for `{discord.utils.escape_markdown(command)}`.",
                    response=True,
                    ephemeral=True,
                )
                return
            await bot.discord.send(
                view=CommandSignatureBox(matched_command),
                response=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await bot.discord.send(
            view=HelpBox(commands, context_menus),
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @help.autocomplete("command")
    async def help_command_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        commands, context_menus = all_help_commands(bot)
        return command_choices(commands, context_menus, current)
