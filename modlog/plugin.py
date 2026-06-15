from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands

from bogobot_core import BotCore
from modlog.audit_log import ModlogEvent, known_actions, normalize_entry, retrieve_and_scan
from modlog.database import ModlogDatabase
from modlog.lifecycle import (
    member_ban_event,
    member_join_event,
    member_remove_event,
    member_unban_event,
    member_update_events,
    message_event,
)
from modlog.undo import ModlogUndoResult, undo_event


MODLOG_CONFIG_KEY = "modlog"
DEFAULT_MODLOG_DATABASE_PATH = "modlog.sqlite3"
MAX_EVENT_LINES = 10
MAX_ACTION_CHOICES = 25
MAX_EVENTS_PER_PAGE = 10
MODLOG_UNDO_CAPABILITY = "modlog.undo"
AUDIT_LOG_RESCAN_OVERLAP = timedelta(minutes=10)
EVENT_LINK_WINDOW_SECONDS = 600
DETAIL_VALUE_LIMIT = 1000
EVENT_MATCH_ACTIONS: dict[str, tuple[str, ...]] = {
    "ban": ("ban", "member_remove"),
    "unban": ("unban",),
    "kick": ("kick", "member_remove"),
    "member_join": ("member_join",),
    "member_remove": ("member_remove", "kick", "ban"),
    "member_update": ("member_update",),
    "member_role_update": ("member_role_update",),
    "message_delete": ("message_delete", "message_bulk_delete"),
    "message_bulk_delete": ("message_delete", "message_bulk_delete"),
    "message_update": ("message_update",),
}


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


def _short_value(value: object, *, limit: int = DETAIL_VALUE_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return discord.utils.escape_markdown(text)
    return discord.utils.escape_markdown(text[:limit]) + "... truncated"


def _format_gateway_capture(event: ModlogEvent) -> list[str]:
    lines: list[str] = []
    message = event.raw.get("message")
    if isinstance(message, dict):
        lines.append("")
        lines.append("### Captured Message")
        lines.append(f"Message ID: `{message.get('id')}`")
        lines.append(f"Channel: <#{message.get('channel_id')}> (`{message.get('channel_id')}`)")
        content = message.get("content")
        if content:
            lines.append(f"Content: {_short_value(content)}")
        attachments = message.get("attachments")
        if isinstance(attachments, list) and attachments:
            lines.append(f"Attachments: `{len(attachments)}`")

    before_message = event.raw.get("before_message")
    if isinstance(before_message, dict) and before_message.get("content") != (message or {}).get("content"):
        lines.append("")
        lines.append("### Previous Message")
        lines.append(f"Content: {_short_value(before_message.get('content'))}")

    member = event.raw.get("member")
    if isinstance(member, dict):
        lines.append("")
        lines.append("### Captured Member")
        lines.append(f"User: <@{member.get('id')}> (`{member.get('id')}`)")
        if member.get("joined_at"):
            lines.append(f"Joined: `{member.get('joined_at')}`")
        roles = member.get("roles")
        if isinstance(roles, list) and roles:
            role_ids = [
                role.get("id")
                for role in roles
                if isinstance(role, dict) and isinstance(role.get("id"), int)
            ]
            lines.append("Roles: " + ", ".join(f"<@&{role_id}> (`{role_id}`)" for role_id in role_ids[:MAX_EVENT_LINES]))
    return lines


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
    if event.related_event_ids:
        lines.append("Related: " + ", ".join(f"`{event_id}`" for event_id in event.related_event_ids[:MAX_EVENT_LINES]))
    if event.reason:
        lines.append(f"Reason: {discord.utils.escape_markdown(event.reason)}")

    lines.extend(_format_gateway_capture(event))

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
            allowed_mentions=discord.AllowedMentions.none()
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
            allowed_mentions=discord.AllowedMentions.none()
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
        refresh_button = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
        )
        previous_button.callback = self.previous_page
        next_button.callback = self.next_page
        refresh_button.callback = self.refresh_page

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(previous_button, next_button, refresh_button))
        self.add_item(container)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def next_page(self, interaction: discord.Interaction) -> None:
        if self.has_next:
            self.page += 1
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def refresh_page(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())


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

    def related_actions(event: ModlogEvent) -> tuple[str, ...]:
        return EVENT_MATCH_ACTIONS.get(event.action, (event.action,))

    def record_event(event: ModlogEvent, *, replace: bool = True) -> bool:
        related = database.related_events(
            event,
            seconds=EVENT_LINK_WINDOW_SECONDS,
            actions=related_actions(event),
        )
        return database.write_event_with_links(event, related=related, replace=replace)

    @bot.connect_callback
    async def scan_since_last_connect() -> None:
        for guild in bot.guilds:
            if not await can_scan_audit_logs(bot, guild):
                logger.debug("Skipping audit log scan for guild %s: missing View Audit Log", guild.id)
                continue

            after_id = database.max_event_id(guild_id=guild.id)
            after = (
                discord.utils.snowflake_time(after_id) - AUDIT_LOG_RESCAN_OVERLAP
                if after_id is not None else
                None
            )
            try:
                scan = await retrieve_and_scan(
                    guild,
                    limit=None,
                    after=after,
                    oldest_first=True,
                )
            except discord.HTTPException:
                logger.exception("Failed scanning audit logs for guild %s", guild.id)
                continue
            except Exception:
                logger.exception("Unexpected error scanning audit logs for guild %s", guild.id)
                continue

            written = sum(1 for event in scan.events if record_event(event, replace=True))
            if written or scan.stats.scanned:
                logger.info(
                    "Scanned audit logs for guild %s: %s new event(s), %s written",
                    guild.id,
                    scan.stats.scanned,
                    written,
                )

    @bot.audit_log_entry_callback
    async def record_audit_log_entry(entry: discord.AuditLogEntry) -> None:
        record_event(normalize_entry(entry))

    @bot.message_delete_callback
    async def record_message_delete(message: discord.Message) -> None:
        event = message_event(action="message_delete", message=message)
        if event is not None:
            record_event(event)

    @bot.bulk_message_delete_callback
    async def record_bulk_message_delete(messages: list[discord.Message]) -> None:
        for message in messages:
            event = message_event(action="message_bulk_delete", message=message, bulk=True)
            if event is not None:
                record_event(event)

    @bot.message_edit_callback
    async def record_message_edit(before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content and before.attachments == after.attachments and before.embeds == after.embeds:
            return
        event = message_event(action="message_update", message=after, before=before)
        if event is not None:
            record_event(event)

    @bot.member_join_callback
    async def record_member_join(member: discord.Member | discord.User) -> None:
        event = member_join_event(member)
        if event is not None:
            record_event(event)

    @bot.member_remove_callback
    async def record_member_remove(member: discord.Member | discord.User) -> None:
        event = member_remove_event(member)
        if event is not None:
            record_event(event)

    @bot.member_update_callback
    async def record_member_update(before: discord.Member, after: discord.Member) -> None:
        for event in member_update_events(before, after):
            record_event(event)

    @bot.member_ban_callback
    async def record_member_ban(guild: discord.Guild, user: discord.User | discord.Member) -> None:
        record_event(member_ban_event(guild, user))

    @bot.member_unban_callback
    async def record_member_unban(guild: discord.Guild, user: discord.User) -> None:
        record_event(member_unban_event(guild, user))

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
