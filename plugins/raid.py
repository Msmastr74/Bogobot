import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import discord

from bogobot_core import BotCore
from utils import groups
from utils import security_roles


RaidAction = Literal["config", "status", "on", "off"]
RaidMode = Literal["quiet", "fixed", "manual"]

DEFAULT_MODE: RaidMode = "quiet"
DEFAULT_WINDOW_SECONDS = 90.0
DEFAULT_QUIET_SECONDS = 300.0
DEFAULT_FIXED_SECONDS = 600.0
DEFAULT_EARLY_MESSAGE_WINDOW_SECONDS = 600.0
DEFAULT_TRIGGER_SCORE = 14
DEFAULT_TRIGGER_JOIN_COUNT = 8

ACCOUNT_AGE_1_DAY = 24 * 60 * 60
ACCOUNT_AGE_7_DAYS = 7 * ACCOUNT_AGE_1_DAY
ACCOUNT_AGE_30_DAYS = 30 * ACCOUNT_AGE_1_DAY
CREATION_CLUSTER_SECONDS = 60 * 60


@dataclass
class RaidConfig:
    enabled: bool = False
    alert_channel_id: int | None = None
    mode: RaidMode = DEFAULT_MODE
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    quiet_seconds: float = DEFAULT_QUIET_SECONDS
    fixed_seconds: float = DEFAULT_FIXED_SECONDS
    early_message_window_seconds: float = DEFAULT_EARLY_MESSAGE_WINDOW_SECONDS
    trigger_score: int = DEFAULT_TRIGGER_SCORE
    trigger_join_count: int = DEFAULT_TRIGGER_JOIN_COUNT


@dataclass
class JoinEvent:
    guild_id: int
    user_id: int
    timestamp: float
    created_at: datetime
    member: discord.Member


@dataclass
class EarlyMessageEvent:
    guild_id: int
    user_id: int
    timestamp: float


@dataclass
class RaidScore:
    score: int
    join_count: int
    early_message_count: int
    cluster_user_ids: set[int] = field(default_factory=set)


@dataclass
class GuildRaidState:
    active: bool = False
    active_since: float | None = None
    expires_at: float | None = None
    last_suspicious_at: float | None = None
    recent_quarantined_ids: list[int] = field(default_factory=list)


class RaidStatusView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        config: RaidConfig,
        state: GuildRaidState,
        score: RaidScore,
    ) -> None:
        super().__init__(timeout=None)
        alert_channel = (
            f"<#{config.alert_channel_id}>"
            if config.alert_channel_id is not None else
            "Not configured"
        )
        expires_text = (
            f"<t:{int(time.time() + max(0.0, state.expires_at - time.monotonic()))}:R>"
            if state.expires_at is not None else
            "Manual"
        )
        quarantined = (
            ", ".join(f"<@{uid}>" for uid in state.recent_quarantined_ids[-10:]) or
            "None"
        )
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Raid Protection"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "\n".join([
                    f"Enabled: `{config.enabled}`",
                    f"Active: `{state.active}`",
                    f"Mode: `{config.mode}`",
                    f"Alert channel: {alert_channel}",
                    f"Current score: `{score.score}`",
                    f"Recent joins: `{score.join_count}`",
                    f"Early messages: `{score.early_message_count}`",
                    f"Expires: {expires_text if state.active else 'Not active'}",
                ])
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"Recent quarantines: {quarantined}"),
        ))


class RaidConfigView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        protector: "RaidProtector",
        guild: discord.Guild,
    ) -> None:
        super().__init__(timeout=300)
        self.protector = protector
        self.guild = guild
        config = protector.config

        self.verified_role_select = discord.ui.RoleSelect(
            placeholder="Choose verified role",
            min_values=1,
            max_values=1,
        )
        self.verified_role_select.callback = self.set_verified_role

        self.quarantine_role_select = discord.ui.RoleSelect(
            placeholder="Choose quarantine role",
            min_values=1,
            max_values=1,
        )
        self.quarantine_role_select.callback = self.set_quarantine_role

        mode_buttons: list[discord.ui.Button] = []
        for mode in ("quiet", "fixed", "manual"):
            button = discord.ui.Button(
                label=mode.capitalize(),
                style=(
                    discord.ButtonStyle.primary
                    if config.mode == mode else
                    discord.ButtonStyle.secondary
                ),
            )
            button.callback = self._set_mode_callback(mode)
            mode_buttons.append(button)

        self.channel_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder=(
                f"Alert channel: #{channel.name}"
                if (
                    config.alert_channel_id is not None and
                    isinstance(channel := protector.bot.get_channel(config.alert_channel_id), discord.TextChannel)
                ) else
                "Choose alert channel"
            ),
        )
        self.channel_select.callback = self.set_alert_channel

        self.numbers_button = discord.ui.Button(
            label="Edit settings",
            style=discord.ButtonStyle.secondary,
        )
        self.numbers_button.callback = self.edit_numbers

        alert_channel_text = (
            f"<#{config.alert_channel_id}>"
            if config.alert_channel_id is not None else
            "Not configured"
        )
        verified_role_text = self._role_text(security_roles.verified_role_id(protector.bot))
        quarantine_role_text = self._role_text(security_roles.quarantine_role_id(protector.bot))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Raid Protection Config"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "\n".join([
                    "### Roles",
                    f"Verified: {verified_role_text}",
                    f"Quarantine: {quarantine_role_text}",
                ])
            ),
            discord.ui.ActionRow(self.verified_role_select),
            discord.ui.ActionRow(self.quarantine_role_select),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "\n".join([
                    "### Settings",
                    f"Enabled: `{config.enabled}`",
                    f"Mode: `{config.mode}`",
                    f"Alert channel: {alert_channel_text}",
                    f"Window seconds: `{config.window_seconds:g}`",
                    f"Early message window: `{config.early_message_window_seconds:g}`",
                    f"Quiet seconds: `{config.quiet_seconds:g}`",
                    f"Fixed seconds: `{config.fixed_seconds:g}`",
                    f"Trigger score: `{config.trigger_score}`",
                    f"Trigger join count: `{config.trigger_join_count}`",
                ])
            ),
            discord.ui.ActionRow(*mode_buttons),
            discord.ui.ActionRow(self.channel_select),
            discord.ui.ActionRow(self.numbers_button),
        ))

    def _role_text(self, role_id: int | None) -> str:
        if role_id is None:
            return "None"
        role = self.guild.get_role(role_id)
        return role.mention if role is not None else f"Missing role `{role_id}`"

    async def set_verified_role(self, interaction: discord.Interaction) -> None:
        selected = self.verified_role_select.values[0]
        if selected.guild.id != self.guild.id:
            await interaction.response.send_message(
                "`Verified role` must be in this server.",
                ephemeral=True,
            )
            return
        await security_roles.set_verified_role(self.protector.bot, selected)
        await interaction.response.edit_message(view=RaidConfigView(
            protector=self.protector,
            guild=self.guild,
        ))

    async def set_quarantine_role(self, interaction: discord.Interaction) -> None:
        selected = self.quarantine_role_select.values[0]
        if selected.guild.id != self.guild.id:
            await interaction.response.send_message(
                "`Quarantine role` must be in this server.",
                ephemeral=True,
            )
            return
        await security_roles.set_quarantine_role(self.protector.bot, selected)
        await interaction.response.edit_message(view=RaidConfigView(
            protector=self.protector,
            guild=self.guild,
        ))

    def _set_mode_callback(self, mode: RaidMode):
        async def callback(interaction: discord.Interaction) -> None:
            await self.set_mode(interaction, mode)

        return callback

    async def set_mode(self, interaction: discord.Interaction, mode: RaidMode) -> None:
        self.protector.config.mode = mode
        await self.protector.save_config()
        await interaction.response.edit_message(view=RaidConfigView(
            protector=self.protector,
            guild=self.guild,
        ))

    async def set_alert_channel(self, interaction: discord.Interaction) -> None:
        selected = self.channel_select.values[0]
        if not isinstance(selected, discord.TextChannel):
            await interaction.response.send_message(
                "`Alert channel` must be a text channel in this server.",
                ephemeral=True,
            )
            return
        if selected.guild is None or selected.guild.id != self.guild.id:
            await interaction.response.send_message(
                "`Alert channel` must be a text channel in this server.",
                ephemeral=True,
            )
            return
        self.protector.config.alert_channel_id = selected.id
        await self.protector.save_config()
        await interaction.response.edit_message(view=RaidConfigView(
            protector=self.protector,
            guild=self.guild,
        ))

    async def edit_numbers(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RaidNumbersModal(
            protector=self.protector,
            guild=self.guild,
        ))

class RaidNumbersModal(discord.ui.Modal, title="Raid Protection Numbers"):
    def __init__(
        self,
        *,
        protector: "RaidProtector",
        guild: discord.Guild,
    ) -> None:
        super().__init__()
        self.protector = protector
        self.guild = guild
        config = protector.config

        self.windows = discord.ui.TextInput(
            label="Windows",
            required=True,
            default=(
                f"window_seconds={config.window_seconds:g} "
                f"early_message_window_seconds={config.early_message_window_seconds:g}"
            ),
            placeholder="window_seconds=90 early_message_window_seconds=600",
            max_length=120,
        )
        self.expiry = discord.ui.TextInput(
            label="Expiry",
            required=True,
            default=(
                f"quiet_seconds={config.quiet_seconds:g} "
                f"fixed_seconds={config.fixed_seconds:g}"
            ),
            placeholder="quiet_seconds=300 fixed_seconds=600",
            max_length=100,
        )
        self.triggers = discord.ui.TextInput(
            label="Triggers",
            required=True,
            default=(
                f"trigger_score={config.trigger_score} "
                f"trigger_join_count={config.trigger_join_count}"
            ),
            placeholder="trigger_score=14 trigger_join_count=8",
            max_length=100,
        )
        for item in (
            self.windows,
            self.expiry,
            self.triggers,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            window_values = self._parse_assignments(
                self.windows.value,
                ("window_seconds", "early_message_window_seconds"),
            )
            expiry_values = self._parse_assignments(
                self.expiry.value,
                ("quiet_seconds", "fixed_seconds"),
            )
            trigger_values = self._parse_assignments(
                self.triggers.value,
                ("trigger_score", "trigger_join_count"),
            )
            self.protector.config.window_seconds = self._positive_float(
                window_values["window_seconds"],
                "window_seconds",
            )
            self.protector.config.early_message_window_seconds = self._positive_float(
                window_values["early_message_window_seconds"],
                "early_message_window_seconds",
            )
            self.protector.config.quiet_seconds = self._positive_float(
                expiry_values["quiet_seconds"],
                "quiet_seconds",
            )
            self.protector.config.fixed_seconds = self._positive_float(
                expiry_values["fixed_seconds"],
                "fixed_seconds",
            )
            self.protector.config.trigger_score = self._positive_int(
                trigger_values["trigger_score"],
                "trigger_score",
            )
            self.protector.config.trigger_join_count = self._positive_int(
                trigger_values["trigger_join_count"],
                "trigger_join_count",
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await self.protector.save_config()
        await interaction.response.send_message(
            view=RaidConfigView(
                protector=self.protector,
                guild=self.guild,
            ),
            ephemeral=True,
        )

    def _parse_assignments(
        self,
        value: str,
        keys: tuple[str, str],
    ) -> dict[str, str]:
        text = value.replace(",", " ").strip()
        parts = [part for part in text.split() if part]
        if len(parts) == len(keys) and all("=" not in part for part in parts):
            return dict(zip(keys, parts))

        parsed: dict[str, str] = {}
        for part in parts:
            if "=" not in part:
                raise ValueError(f"`{part}` must use `key=value` format.")
            key, raw = part.split("=", 1)
            if key not in keys:
                raise ValueError(f"`{key}` is not valid here.")
            parsed[key] = raw
        missing = [key for key in keys if key not in parsed]
        if missing:
            raise ValueError(f"Missing `{', '.join(missing)}`.")
        return parsed

    def _positive_float(self, value: str, label: str) -> float:
        number = float(value)
        if number <= 0:
            raise ValueError(f"`{label}` must be greater than 0.")
        return number

    def _positive_int(self, value: str, label: str) -> int:
        number = int(value)
        if number <= 0:
            raise ValueError(f"`{label}` must be greater than 0.")
        return number


class RaidProtector:
    def __init__(self, bot: BotCore) -> None:
        self.bot = bot
        self.logger = bot.logger.getChild("Raid")
        self.config = self._load_config()
        self.join_events: list[JoinEvent] = []
        self.early_message_events: list[EarlyMessageEvent] = []
        self.guild_states: dict[int, GuildRaidState] = {}

    def _load_config(self) -> RaidConfig:
        raw = self.bot.config.get("raid_protection", {})
        config = raw if isinstance(raw, dict) else {}
        return RaidConfig(
            enabled=bool(config.get("enabled", False)),
            alert_channel_id=(
                int(config["alert_channel_id"])
                if config.get("alert_channel_id") is not None else
                None
            ),
            mode=self._mode(config.get("mode", DEFAULT_MODE)),
            window_seconds=float(config.get("window_seconds", DEFAULT_WINDOW_SECONDS)),
            quiet_seconds=float(config.get("quiet_seconds", DEFAULT_QUIET_SECONDS)),
            fixed_seconds=float(config.get("fixed_seconds", DEFAULT_FIXED_SECONDS)),
            early_message_window_seconds=float(config.get(
                "early_message_window_seconds",
                DEFAULT_EARLY_MESSAGE_WINDOW_SECONDS,
            )),
            trigger_score=int(config.get("trigger_score", DEFAULT_TRIGGER_SCORE)),
            trigger_join_count=int(config.get("trigger_join_count", DEFAULT_TRIGGER_JOIN_COUNT)),
        )

    def _mode(self, value: object) -> RaidMode:
        return value if value in ("quiet", "fixed", "manual") else DEFAULT_MODE

    async def save_config(self) -> None:
        self.bot.config["raid_protection"] = {
            "enabled": self.config.enabled,
            "alert_channel_id": self.config.alert_channel_id,
            "mode": self.config.mode,
            "window_seconds": self.config.window_seconds,
            "quiet_seconds": self.config.quiet_seconds,
            "fixed_seconds": self.config.fixed_seconds,
            "early_message_window_seconds": self.config.early_message_window_seconds,
            "trigger_score": self.config.trigger_score,
            "trigger_join_count": self.config.trigger_join_count,
        }
        await self.bot.save_config()

    async def configure(
        self,
        *,
        alert_channel: discord.TextChannel | None,
    ) -> None:
        if alert_channel is not None:
            self.config.alert_channel_id = alert_channel.id
        await self.save_config()

    async def manual_on(self, guild_id: int) -> None:
        state = self.state_for(guild_id)
        self._activate(state, "manual activation")
        await self.alert(guild_id, "Raid mode manually enabled.", self.score(guild_id), [])

    async def manual_off(self, guild_id: int) -> None:
        state = self.state_for(guild_id)
        state.active = False
        state.expires_at = None
        await self.alert(guild_id, "Raid mode manually disabled.", self.score(guild_id), [])

    async def on_member_join(self, member: discord.Member | discord.User) -> None:
        if not isinstance(member, discord.Member):
            return
        if self.should_skip_member(member):
            return

        now = time.monotonic()
        self.join_events.append(JoinEvent(
            guild_id=member.guild.id,
            user_id=member.id,
            timestamp=now,
            created_at=member.created_at,
            member=member,
        ))
        self.trim(now)

        if not self.config.enabled or self.config.alert_channel_id is None:
            return

        state = self.state_for(member.guild.id)
        await self.maybe_expire(member.guild.id, now)

        if state.active:
            self._activate(state, "active suspicious join")
            quarantined = await self.quarantine_members([member], "raid mode active")
            self.record_quarantines(state, quarantined)
            if quarantined:
                await self.alert(
                    member.guild.id,
                    "Quarantined new join while raid mode is active.",
                    self.score(member.guild.id),
                    quarantined,
                )
            return

        score = self.score(member.guild.id)
        if self.is_triggered(score):
            state.last_suspicious_at = now
            self._activate(state, "automatic trigger")
            candidates = [
                event.member
                for event in self.window_join_events(member.guild.id, now)
            ]
            quarantined = await self.quarantine_members(candidates, "raid protection triggered")
            self.record_quarantines(state, quarantined)
            await self.alert(
                member.guild.id,
                "Raid protection triggered.",
                score,
                quarantined,
            )

    async def on_message(self, message: discord.Message) -> None:
        if not isinstance(message.author, discord.Member):
            return
        member = message.author
        if self.should_skip_member(member):
            return

        now = time.monotonic()
        recent_join = self.recent_join_for(member.guild.id, member.id, now)
        if recent_join is None:
            return

        self.early_message_events.append(EarlyMessageEvent(
            guild_id=member.guild.id,
            user_id=member.id,
            timestamp=now,
        ))
        self.trim(now)

        if not self.config.enabled or self.config.alert_channel_id is None:
            return

        state = self.state_for(member.guild.id)
        await self.maybe_expire(member.guild.id, now)
        score = self.score(member.guild.id)
        if state.active:
            self._activate(state, "active suspicious message")
            return

        if self.is_triggered(score):
            state.last_suspicious_at = now
            self._activate(state, "early message trigger")
            candidates = [
                event.member
                for event in self.window_join_events(member.guild.id, now)
            ]
            quarantined = await self.quarantine_members(candidates, "raid protection triggered")
            self.record_quarantines(state, quarantined)
            await self.alert(
                member.guild.id,
                "Raid protection triggered by early messages.",
                score,
                quarantined,
            )

    def state_for(self, guild_id: int) -> GuildRaidState:
        return self.guild_states.setdefault(guild_id, GuildRaidState())

    def should_skip_member(self, member: discord.Member) -> bool:
        return (
            member.bot or
            self.bot.is_authorized(member.id, 1) or
            security_roles.has_role_id(member, security_roles.verified_role_id(self.bot)) or
            security_roles.has_role_id(member, security_roles.quarantine_role_id(self.bot))
        )

    def trim(self, now: float) -> None:
        keep_since = now - max(
            self.config.window_seconds,
            self.config.early_message_window_seconds,
        )
        self.join_events = [
            event for event in self.join_events
            if event.timestamp >= keep_since
        ]
        self.early_message_events = [
            event for event in self.early_message_events
            if event.timestamp >= keep_since
        ]

    def window_join_events(self, guild_id: int, now: float) -> list[JoinEvent]:
        window_start = now - self.config.window_seconds
        return [
            event for event in self.join_events
            if event.guild_id == guild_id and event.timestamp >= window_start
        ]

    def window_message_events(self, guild_id: int, now: float) -> list[EarlyMessageEvent]:
        window_start = now - self.config.window_seconds
        return [
            event for event in self.early_message_events
            if event.guild_id == guild_id and event.timestamp >= window_start
        ]

    def recent_join_for(self, guild_id: int, user_id: int, now: float) -> JoinEvent | None:
        window_start = now - self.config.early_message_window_seconds
        for event in reversed(self.join_events):
            if (
                event.guild_id == guild_id and
                event.user_id == user_id and
                event.timestamp >= window_start
            ):
                return event
        return None

    def score(self, guild_id: int) -> RaidScore:
        now = time.monotonic()
        self.trim(now)
        joins = self.window_join_events(guild_id, now)
        messages = self.window_message_events(guild_id, now)
        cluster_user_ids = self.creation_cluster_user_ids(joins)
        total = len(messages) * 2
        for event in joins:
            total += 1 + self.account_age_score(event.created_at)
            if event.user_id in cluster_user_ids:
                total += 2
        return RaidScore(
            score=total,
            join_count=len(joins),
            early_message_count=len(messages),
            cluster_user_ids=cluster_user_ids,
        )

    def is_triggered(self, score: RaidScore) -> bool:
        return score.join_count > 0 and (
            score.score >= self.config.trigger_score or
            score.join_count >= self.config.trigger_join_count
        )

    def account_age_score(self, created_at: datetime) -> int:
        age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
        age_seconds = age.total_seconds()
        if age_seconds < ACCOUNT_AGE_1_DAY:
            return 3
        if age_seconds < ACCOUNT_AGE_7_DAYS:
            return 2
        if age_seconds < ACCOUNT_AGE_30_DAYS:
            return 1
        return 0

    def creation_cluster_user_ids(self, joins: list[JoinEvent]) -> set[int]:
        cluster_user_ids: set[int] = set()
        by_created = sorted(
            (
                event.created_at.astimezone(timezone.utc).timestamp(),
                event.user_id,
            )
            for event in joins
        )
        for index, (created_timestamp, _user_id) in enumerate(by_created):
            nearby = [
                uid for timestamp, uid in by_created
                if abs(timestamp - created_timestamp) <= CREATION_CLUSTER_SECONDS
            ]
            if len(nearby) >= 3:
                cluster_user_ids.update(nearby)
        return cluster_user_ids

    def _activate(self, state: GuildRaidState, _reason: str) -> None:
        now = time.monotonic()
        state.active = True
        state.active_since = state.active_since or now
        state.last_suspicious_at = now
        if self.config.mode == "fixed":
            state.expires_at = now + self.config.fixed_seconds
        elif self.config.mode == "quiet":
            state.expires_at = now + self.config.quiet_seconds
        else:
            state.expires_at = None

    async def maybe_expire(self, guild_id: int, now: float) -> None:
        state = self.state_for(guild_id)
        if not state.active or state.expires_at is None:
            return
        if state.expires_at > now:
            return
        state.active = False
        state.expires_at = None
        await self.alert(guild_id, "Raid mode automatically expired.", self.score(guild_id), [])

    async def quarantine_members(
        self,
        members: list[discord.Member],
        reason: str,
    ) -> list[discord.Member]:
        quarantined: list[discord.Member] = []
        for member in members:
            if self.should_skip_member(member):
                continue
            role = security_roles.quarantine_role(self.bot, member.guild)
            if role is None:
                self.logger.warning(
                    f"Raid quarantine skipped for guild {member.guild.id}: quarantine role is not configured."
                )
                return quarantined
            try:
                await member.add_roles(role, reason=reason)
            except discord.Forbidden:
                self.logger.warning(f"Missing permissions to quarantine {member} ({member.id}).")
            except discord.HTTPException:
                self.logger.exception(f"Failed to quarantine {member} ({member.id}).")
            else:
                quarantined.append(member)
        return quarantined

    def record_quarantines(
        self,
        state: GuildRaidState,
        members: list[discord.Member],
    ) -> None:
        for member in members:
            if member.id not in state.recent_quarantined_ids:
                state.recent_quarantined_ids.append(member.id)
        state.recent_quarantined_ids = state.recent_quarantined_ids[-25:]

    async def alert(
        self,
        guild_id: int,
        message: str,
        score: RaidScore,
        quarantined: list[discord.Member],
    ) -> None:
        channel_id = self.config.alert_channel_id
        if channel_id is None:
            self.logger.warning(f"Raid alert skipped for guild {guild_id}: alert channel is not configured.")
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            self.logger.warning(f"Raid alert skipped for guild {guild_id}: alert channel {channel_id} is unavailable.")
            return
        mentions = ", ".join(member.mention for member in quarantined) or "None"
        try:
            await channel.send(view=RaidAlertView(
                title=message,
                config=self.config,
                score=score,
                quarantined=mentions,
            ))
        except discord.HTTPException:
            self.logger.exception(f"Failed to send raid alert to channel {channel_id}.")


class RaidAlertView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        config: RaidConfig,
        score: RaidScore,
        quarantined: str,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## {title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "\n".join([
                    f"Mode: `{config.mode}`",
                    f"Score: `{score.score}`",
                    f"Recent joins: `{score.join_count}`",
                    f"Early messages: `{score.early_message_count}`",
                    f"Creation clusters: `{len(score.cluster_user_ids)}` users",
                    f"Quarantined: {quarantined}",
                ])
            ),
        ))


async def setup(bot: BotCore) -> None:
    protector = RaidProtector(bot)
    manage = groups.manage(bot)

    @bot.member_join_callback
    async def on_member_join(member: discord.Member | discord.User) -> None:
        await protector.on_member_join(member)

    @bot.message_callback
    async def on_message(message: discord.Message) -> None:
        await protector.on_message(message)

    @manage.command(name="raid", description="Manage raid protection", perm_requirement=2, defer=False)
    async def raid(
        interaction: discord.Interaction,
        action: RaidAction,
        alert_channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await bot.discord.send(
                "Raid protection can only be managed in a server.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "config":
            if alert_channel is not None and alert_channel.guild.id != guild.id:
                await bot.discord.send(
                    "`alert_channel` must be in this server.",
                    response=True,
                    ephemeral=True,
                )
                return
            await protector.configure(alert_channel=alert_channel)
            await interaction.response.send_message(
                view=RaidConfigView(
                    protector=protector,
                    guild=guild,
                ),
                ephemeral=True,
            )
            return

        if action == "status":
            await protector.maybe_expire(guild.id, time.monotonic())
            await bot.discord.send(
                view=RaidStatusView(
                    config=protector.config,
                    state=protector.state_for(guild.id),
                    score=protector.score(guild.id),
                ),
                response=True,
                ephemeral=True,
            )
            return

        if action == "on":
            if protector.config.alert_channel_id is None:
                await bot.discord.send(
                    "Raid protection needs an alert channel first: `/manage raid action:config alert_channel:#channel`.",
                    response=True,
                    ephemeral=True,
                )
                return
            if security_roles.quarantine_role(bot, guild) is None:
                await bot.discord.send(
                    "Raid protection needs a configured quarantine role first: `/manage raid action:config`.",
                    response=True,
                    ephemeral=True,
                )
                return
            protector.config.enabled = True
            if alert_channel is not None:
                if alert_channel.guild.id != guild.id:
                    await bot.discord.send(
                        "`alert_channel` must be in this server.",
                        response=True,
                        ephemeral=True,
                    )
                    return
                protector.config.alert_channel_id = alert_channel.id
            await protector.save_config()
            await protector.manual_on(guild.id)
            await bot.discord.send(
                "Raid mode enabled.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "off":
            state = protector.state_for(guild.id)
            protector.config.enabled = False
            state.active = False
            state.expires_at = None
            await protector.save_config()
            await protector.manual_off(guild.id)
            await bot.discord.send(
                "Raid protection disabled.",
                response=True,
                ephemeral=True,
            )
            return
