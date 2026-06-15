from pathlib import Path

import discord
from discord import app_commands

from bogobot_core import BotCore
from modlog.audit_log import ModlogEvent, known_actions, normalize_entry, retrieve_and_scan
from modlog.database import ModlogDatabase
from modlog.undo import ModlogUndoResult, undo_event


MODLOG_CONFIG_KEY = "modlog"
DEFAULT_MODLOG_DATABASE_PATH = "modlog.sqlite3"
MAX_EVENT_LINES = 10
MAX_ACTION_CHOICES = 25
MAX_EVENTS_PER_PAGE = 10
MODLOG_UNDO_CAPABILITY = "modlog.undo"


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
    if entity.type in {"Member", "User", "ClientUser"}:
        return f"<@{entity.id}> (`{entity.id}`)"
    if entity.type == "Role":
        return f"<@&{entity.id}> (`{entity.id}`)"
    if entity.type in {
        "CategoryChannel",
        "ForumChannel",
        "StageChannel",
        "TextChannel",
        "Thread",
        "VoiceChannel",
    }:
        return f"<#{entity.id}> (`{entity.id}`)"
    return f"{entity.type} `{entity.id}`"


def format_event_line(event: ModlogEvent) -> str:
    return (
        f"`{event.id}` <t:{int(event.created_at.timestamp())}:R> "
        f"`{event.action}` {entity_text(event.actor)} -> {entity_text(event.target)}"
    )


def format_event_details(event: ModlogEvent) -> str:
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


class ModlogUndoResultView(discord.ui.LayoutView):
    def __init__(self, result: ModlogUndoResult) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## {result.title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(result.message),
        ))


class ModlogUndoButton(discord.ui.Button["ModlogEventView"]):
    def __init__(self) -> None:
        super().__init__(label="Undo", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        bot = interaction.client
        if view is None or not isinstance(bot, BotCore):
            await interaction.response.send_message(
                "Undo is not available right now.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Undo can only run in a server.",
                ephemeral=True,
            )
            return

        account = bot.accounts[interaction.user.id].local(interaction.guild.id)
        if not account.permissions.can_use(MODLOG_UNDO_CAPABILITY, registry=bot.accounts.capabilities):
            await interaction.response.send_message(
                f"Missing capability `{MODLOG_UNDO_CAPABILITY}`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await undo_event(interaction.guild, view.event)
        await interaction.followup.send(
            view=ModlogUndoResultView(result),
            ephemeral=True,
        )


class ModlogEventView(discord.ui.LayoutView):
    def __init__(self, event: ModlogEvent) -> None:
        super().__init__(timeout=None)
        self.event = event
        container = discord.ui.Container(
            discord.ui.TextDisplay("## Modlog Event"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(format_event_details(event)),
        )
        if any(reverse.possible for reverse in event.reverse_actions):
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(ModlogUndoButton()))
        self.add_item(container)


class ModlogEventButton(discord.ui.Button["ModlogView"]):
    def __init__(self, event_id: int) -> None:
        super().__init__(label="Details", style=discord.ButtonStyle.secondary)
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            await interaction.response.send_message(
                "This modlog entry is not available right now.",
                ephemeral=True,
            )
            return

        event = view.database.read_event(self.event_id)
        if event is None:
            await interaction.response.send_message(
                f"No event found for `{self.event_id}`.",
                ephemeral=True,
            )
            return
        if interaction.guild_id is not None and event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "That event belongs to a different server.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ModlogEventView(event),
            ephemeral=True,
        )


class ModlogView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        database: ModlogDatabase,
        guild_id: int,
        action: str | None,
        actor_id: int | None,
        target_id: int | None,
        page_size: int,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        self.database = database
        self.guild_id = guild_id
        self.action = action
        self.actor_id = actor_id
        self.target_id = target_id
        self.page_size = min(MAX_EVENTS_PER_PAGE, page_size)
        self.page = max(0, page)
        self.has_next = False
        self.render()

    def page_events(self) -> list[ModlogEvent]:
        events = self.database.query_events(
            guild_id=self.guild_id,
            action=self.action,
            actor_id=self.actor_id,
            target_id=self.target_id,
            limit=self.page_size + 1,
            offset=self.page * self.page_size,
        )
        self.has_next = len(events) > self.page_size
        return events[:self.page_size]

    def render(self) -> None:
        self.clear_items()
        events = self.page_events()
        container = discord.ui.Container(
            discord.ui.TextDisplay(f"## Modlog · Page {self.page + 1}"),
            discord.ui.Separator(),
        )

        if events:
            for event in events:
                container.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(format_event_line(event)),
                    accessory=ModlogEventButton(event.id),
                ))
        else:
            container.add_item(discord.ui.TextDisplay("No matching events."))

        previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=not self.has_next,
        )
        previous_button.callback = self.previous_page
        next_button.callback = self.next_page

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(previous_button, next_button))
        self.add_item(container)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        self.render()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        if self.has_next:
            self.page += 1
        self.render()
        await interaction.response.edit_message(view=self)


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
    database = ModlogDatabase(database_path(bot))
    logger = bot.logger.getChild("Modlog")
    bot.accounts.capabilities.register(MODLOG_UNDO_CAPABILITY)

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

    @bot.setup.command(
        name="modlog",
        description="Browse imported moderation log events",
        capabilities=["modlog.view"],
        eph=True,
        defer=False,
    )
    async def modlog(
        interaction: discord.Interaction,
        action: str | None = None,
        actor: discord.User | None = None,
        target: discord.User | None = None,
        limit: app_commands.Range[int, 1, 10] = MAX_EVENTS_PER_PAGE,
    ) -> None:
        if interaction.guild is None:
            await bot.discord.send("Modlog can only run in a server.", response=True, ephemeral=True)
            return

        if action is not None and audit_action_from_name(action) is None:
            await bot.discord.send(f"Unknown audit action `{discord.utils.escape_markdown(action)}`.", response=True, ephemeral=True)
            return

        await bot.discord.send(
            view=ModlogView(
                database=database,
                guild_id=interaction.guild.id,
                action=action,
                actor_id=actor.id if actor is not None else None,
                target_id=target.id if target is not None else None,
                page_size=int(limit),
            ),
            response=True,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @modlog.autocomplete("action")
    async def modlog_action_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await action_autocomplete(interaction, current)
