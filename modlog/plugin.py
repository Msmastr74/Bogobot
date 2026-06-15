from pathlib import Path
from typing import Literal

import discord
from discord import app_commands

from bogobot_core import BotCore
from modlog.audit_log import AuditEvent, known_actions, normalize_entry, retrieve_and_scan
from modlog.database import AuditLogDatabase


MODLOG_CONFIG_KEY = "modlog"
DEFAULT_MODLOG_DATABASE_PATH = "modlog.sqlite3"
MAX_EVENT_LINES = 10
MAX_ACTION_CHOICES = 25


def database_path(bot: BotCore) -> Path:
    config = bot.config.get(MODLOG_CONFIG_KEY)
    if isinstance(config, dict):
        path = config.get("database_path")
        if isinstance(path, str) and path:
            return Path(path)
    return Path(DEFAULT_MODLOG_DATABASE_PATH)


def audit_action_from_name(name: str | None) -> discord.AuditLogAction | None:
    if name is None or not name.strip():
        return None
    normalized = name.strip()
    for action in known_actions():
        if action.name == normalized:
            return action
    return None


def action_names() -> tuple[str, ...]:
    return tuple(action.name for action in known_actions())


async def can_scan_audit_logs(bot: BotCore, guild: discord.Guild) -> bool:
    bot_member = guild.me
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    if bot_member is None and bot.user is not None:
        try:
            bot_member = await guild.fetch_member(bot.user.id)
        except discord.HTTPException:
            bot_member = None
    return bot_member is not None and bot_member.guild_permissions.view_audit_log


def entity_text(entity) -> str:
    if entity is None:
        return "Unknown"
    if entity.id is None:
        return entity.type
    return f"{entity.type} `{entity.id}`"


def format_event_line(event: AuditEvent) -> str:
    return (
        f"`{event.id}` <t:{int(event.created_at.timestamp())}:R> "
        f"`{event.action}` {entity_text(event.actor)} -> {entity_text(event.target)}"
    )


def format_event_details(event: AuditEvent) -> str:
    lines = [
        f"ID: `{event.id}`",
        f"Action: `{event.action}`",
        f"Guild: `{event.guild_id}`",
        f"Created: <t:{int(event.created_at.timestamp())}:F>",
        f"Imported: <t:{int(event.imported_at.timestamp())}:F>",
        f"Actor: {entity_text(event.actor)}",
        f"Target: {entity_text(event.target)}",
    ]
    if event.reason:
        lines.append(f"Reason: {discord.utils.escape_markdown(event.reason)}")

    if event.changes:
        lines.append("")
        lines.append("### Changes")
        for change in event.changes[:MAX_EVENT_LINES]:
            old = discord.utils.escape_markdown(repr(change.old))
            new = discord.utils.escape_markdown(repr(change.new))
            lines.append(f"`{change.key}`: `{old}` -> `{new}`")
        if len(event.changes) > MAX_EVENT_LINES:
            lines.append(f"-# {len(event.changes) - MAX_EVENT_LINES} more changes")

    if event.reverse_actions:
        lines.append("")
        lines.append("### Reverse Actions")
        for reverse in event.reverse_actions:
            state = "possible" if reverse.possible else "not possible"
            reason = f": {discord.utils.escape_markdown(reverse.reason)}" if reverse.reason else ""
            lines.append(f"`{reverse.kind}` - {state}{reason}")

    return "\n".join(lines)


class ModlogEventsView(discord.ui.LayoutView):
    def __init__(self, *, title: str, events: list[AuditEvent], footer: str | None = None) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.Separator(),
        )
        if events:
            container.add_item(discord.ui.TextDisplay("\n".join(format_event_line(event) for event in events)))
        else:
            container.add_item(discord.ui.TextDisplay("No matching events."))
        if footer is not None:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        self.add_item(container)


class ModlogEventView(discord.ui.LayoutView):
    def __init__(self, event: AuditEvent) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Modlog Event"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(format_event_details(event)),
        ))


async def action_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [
        name
        for name in action_names()
        if current in name.lower()
    ]
    return [
        app_commands.Choice(name=name, value=name)
        for name in matches[:MAX_ACTION_CHOICES]
    ]


async def setup(bot: BotCore) -> None:
    database = AuditLogDatabase(database_path(bot))
    logger = bot.logger.getChild("Modlog")
    modlog = bot.setup.group("modlog", "Moderation log commands")

    @bot.connect_callback
    async def scan_since_last_connect() -> None:
        for guild in bot.guilds:
            if not await can_scan_audit_logs(bot, guild):
                logger.debug("Skipping audit log scan for guild %s: missing View Audit Log", guild.id)
                continue

            after_id = database.max_event_id(guild_id=guild.id)
            try:
                scan = await retrieve_and_scan(
                    guild,
                    limit=None,
                    after=discord.Object(id=after_id) if after_id is not None else None,
                    oldest_first=True,
                    database=database,
                )
            except discord.HTTPException:
                logger.exception("Failed scanning audit logs for guild %s", guild.id)
                continue
            except Exception:
                logger.exception("Unexpected error scanning audit logs for guild %s", guild.id)
                continue

            written = database.write_events(scan.events)
            if written or scan.stats.scanned:
                logger.info(
                    "Scanned audit logs for guild %s: %s new event(s), %s written",
                    guild.id,
                    scan.stats.scanned,
                    written,
                )

    @bot.audit_log_entry_callback
    async def record_audit_log_entry(entry: discord.AuditLogEntry) -> None:
        database.write_event(normalize_entry(entry))

    @modlog.command(
        name="search",
        description="Search imported moderation log events",
        capabilities=["modlog.view"],
        eph=True,
        defer=False,
    )
    async def search(
        interaction: discord.Interaction,
        action: str | None = None,
        actor: discord.User | None = None,
        target: discord.User | None = None,
        limit: app_commands.Range[int, 1, 50] = 10,
        order: Literal["newest", "oldest"] = "newest",
    ) -> None:
        if interaction.guild is None:
            await bot.discord.send("Modlog search can only run in a server.", response=True, ephemeral=True)
            return

        if action is not None and audit_action_from_name(action) is None:
            await bot.discord.send(f"Unknown audit action `{discord.utils.escape_markdown(action)}`.", response=True, ephemeral=True)
            return

        events = database.query_events(
            guild_id=interaction.guild.id,
            action=action,
            actor_id=actor.id if actor is not None else None,
            target_id=target.id if target is not None else None,
            limit=limit,
            order="asc" if order == "oldest" else "desc",
        )
        await bot.discord.send(
            view=ModlogEventsView(
                title="Modlog Search",
                events=events,
                footer=f"{len(events)} event(s)",
            ),
            response=True,
            ephemeral=True,
        )

    @modlog.command(
        name="user",
        description="Show imported moderation log events for a user",
        capabilities=["modlog.view"],
        eph=True,
        defer=False,
    )
    async def user(
        interaction: discord.Interaction,
        target: discord.User,
        limit: app_commands.Range[int, 1, 50] = 10,
    ) -> None:
        if interaction.guild is None:
            await bot.discord.send("Modlog user lookup can only run in a server.", response=True, ephemeral=True)
            return

        actor_events = database.query_events(
            guild_id=interaction.guild.id,
            actor_id=target.id,
            limit=limit,
        )
        target_events = database.query_events(
            guild_id=interaction.guild.id,
            target_id=target.id,
            limit=limit,
        )
        events = sorted(
            {event.id: event for event in (*actor_events, *target_events)}.values(),
            key=lambda event: event.id,
            reverse=True,
        )[:limit]
        await bot.discord.send(
            view=ModlogEventsView(
                title=f"Modlog Events for {target.mention}",
                events=events,
                footer=f"{len(events)} event(s)",
            ),
            response=True,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @modlog.command(
        name="event",
        description="Show one imported moderation log event",
        capabilities=["modlog.view"],
        eph=True,
        defer=False,
    )
    async def event(interaction: discord.Interaction, id: str) -> None:
        try:
            event_id = int(id)
        except ValueError:
            await bot.discord.send("Event ID must be a Discord snowflake.", response=True, ephemeral=True)
            return

        stored_event = database.read_event(event_id)
        if stored_event is None:
            await bot.discord.send(f"No event found for `{event_id}`.", response=True, ephemeral=True)
            return
        if interaction.guild_id is not None and stored_event.guild_id != interaction.guild_id:
            await bot.discord.send("That event belongs to a different server.", response=True, ephemeral=True)
            return

        await bot.discord.send(
            view=ModlogEventView(stored_event),
            response=True,
            ephemeral=True,
        )

    @modlog.command(
        name="undo",
        description="Show whether a moderation log event can be undone",
        capabilities=["modlog.undo"],
        eph=True,
        defer=False,
    )
    async def undo(interaction: discord.Interaction, id: str) -> None:
        try:
            event_id = int(id)
        except ValueError:
            await bot.discord.send("Event ID must be a Discord snowflake.", response=True, ephemeral=True)
            return

        stored_event = database.read_event(event_id)
        if stored_event is None:
            await bot.discord.send(f"No event found for `{event_id}`.", response=True, ephemeral=True)
            return

        await bot.discord.send(
            view=ModlogEventView(stored_event),
            response=True,
            ephemeral=True,
        )

    @search.autocomplete("action")
    async def search_action_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await action_autocomplete(interaction, current)
