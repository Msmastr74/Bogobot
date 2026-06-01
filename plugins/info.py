import inspect
import re
import types
from typing import Any, Union, get_args, get_origin

import discord
from discord import app_commands

from bogobot_core import BotCore
from utils.ai import AIParam, action
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
COMMAND_NAME_CHARACTER_RE = re.compile(r"[^A-Za-z]+")


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

    def _signature(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> str:
        if isinstance(command, app_commands.ContextMenu):
            return self._context_menu_signature(command)

        parts = [f"/{command.qualified_name}"]
        callback_parameters = self._callback_parameter_map(command)
        for parameter in command.parameters:
            callback_parameter = callback_parameters.get(parameter.name)
            parts.append(self._parameter_signature(parameter.display_name, callback_parameter))
        return " ".join(parts)

    def _context_menu_signature(self, command: app_commands.ContextMenu) -> str:
        parameters = [
            self._parameter_signature(parameter.name, parameter)
            for parameter in self._callback_parameters(command)
        ]
        return " ".join([command.name, *parameters])

    def _description(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> str:
        if isinstance(command, app_commands.ContextMenu):
            return "Context menu command."
        return command.description

    def _callback_parameter_map(
        self,
        command: app_commands.Command,
    ) -> dict[str, inspect.Parameter]:
        return {
            parameter.name: parameter
            for parameter in self._callback_parameters(command)
        }

    def _callback_parameters(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> list[inspect.Parameter]:
        signature = inspect.signature(command.callback)
        parameters = list(signature.parameters.values())
        return [
            parameter
            for parameter in parameters
            if parameter.name != "interaction"
        ]

    def _parameter_signature(
        self,
        name: str,
        parameter: inspect.Parameter | None,
    ) -> str:
        if parameter is None:
            return f"{name}: unknown"

        signature = f"{name}: {self._annotation_text(parameter.annotation)}"
        if parameter.default is not inspect.Signature.empty:
            signature = f"{signature} = {parameter.default!r}"
        return signature

    def _annotation_text(self, annotation: object) -> str:
        if annotation is inspect.Signature.empty:
            return "unknown"
        if isinstance(annotation, app_commands.Transformer):
            annotation = inspect.signature(annotation.transform).return_annotation

        origin = get_origin(annotation)
        if origin in (Union, types.UnionType):
            return " | ".join(
                self._annotation_text(argument)
                for argument in get_args(annotation)
            )
        if annotation is None or annotation is types.NoneType:
            return "None"
        if isinstance(annotation, str):
            return annotation

        return self._stringify_type(annotation)

    def _stringify_type(self, annotation: Any) -> str:
        if getattr(annotation, "_name", None):
            return annotation._name
        if get_origin(annotation) is not None or hasattr(annotation, "__args__"):
            return str(annotation).replace("typing.", "")
        return getattr(annotation, "__name__", str(annotation))


def normalize_command_name(name: str) -> str:
    return COMMAND_NAME_CHARACTER_RE.sub("", name)


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
    @action(
        "help",
        "Show bot information or help for one command.",
        params={
            "command": AIParam(
                "Optional command name normalized to A-Za-z only, such as help, ping, "
                "bogochoice, or manage monitor.",
                type=str | None,
                required=False,
                default=None,
            ),
        },
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
