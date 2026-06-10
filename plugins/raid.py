import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, cast

import discord

from bogobot_core import BotCore
from utils import groups
from utils import security_roles


RaidAction = Literal["config", "status", "on", "off", "activate", "deactivate"]
RaidMode = Literal["quiet", "fixed", "manual"]

DEFAULT_MODE: RaidMode = "quiet"
DEFAULT_WINDOW_SECONDS = 600.0
DEFAULT_QUIET_SECONDS = 900.0
DEFAULT_FIXED_SECONDS = 900.0
DEFAULT_EARLY_MESSAGE_WINDOW_SECONDS = 600.0
DEFAULT_TRIGGER_SCORE = 16
DEFAULT_TRIGGER_JOIN_COUNT = 10
EARLY_MESSAGE_SCORE = 2

ACCOUNT_AGE_1_DAY = 24 * 60 * 60
ACCOUNT_AGE_7_DAYS = 7 * ACCOUNT_AGE_1_DAY
ACCOUNT_AGE_30_DAYS = 30 * ACCOUNT_AGE_1_DAY
CREATION_CLUSTER_SECONDS = 60 * 60
RAID_CONFIG_ACCOUNT_KEY = "raid_protection"
QUARANTINE_ACCOUNT_KEY = "raid_quarantine"
UNQUARANTINE_CUSTOM_ID_PREFIX = "bogobot:raid:unquarantine"
RAID_MANAGE_CAPABILITY = "raid.manage"
RAID_UNQUARANTINE_CAPABILITY = "raid.unquarantine"
RAID_EXEMPT_CAPABILITY = "raid.exempt"


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


@dataclass
class QuarantineResult:
    member: discord.Member
    removed_roles: list[discord.Role]
    reason: str
    timestamp: int


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
                    f"Automation enabled: `{config.enabled}`",
                    f"Raid mode active: `{state.active}`",
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
        config = protector.config_for(guild.id)

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
        verified_role_text = self._role_text(security_roles.verified_role_id(protector.bot, guild))
        quarantine_role_text = self._role_text(security_roles.quarantine_role_id(protector.bot, guild))
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
                    f"Automation enabled: `{config.enabled}`",
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
        role_error = security_roles.manageable_role_error(
            self.guild,
            selected,
            "Verified role",
        )
        if role_error is not None:
            await interaction.response.send_message(role_error, ephemeral=True)
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
        role_error = security_roles.manageable_role_error(
            self.guild,
            selected,
            "Quarantine role",
        )
        if role_error is not None:
            await interaction.response.send_message(role_error, ephemeral=True)
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
        self.protector.config_for(self.guild.id).mode = mode
        await self.protector.save_config(self.guild.id)
        await interaction.response.edit_message(view=RaidConfigView(
            protector=self.protector,
            guild=self.guild,
        ))

    async def set_alert_channel(self, interaction: discord.Interaction) -> None:
        selected = self.channel_select.values[0]
        try:
            selected = await selected.fetch()
        except Exception as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
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
        self.protector.config_for(self.guild.id).alert_channel_id = selected.id
        await self.protector.save_config(self.guild.id)
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
        config = protector.config_for(guild.id)

        self.windows = discord.ui.TextInput(
            label="Windows",
            required=True,
            default=(
                f"window_seconds={config.window_seconds:g} "
                f"early_message_window_seconds={config.early_message_window_seconds:g}"
            ),
            placeholder="window_seconds=600 early_message_window_seconds=600",
            max_length=120,
        )
        self.expiry = discord.ui.TextInput(
            label="Expiry",
            required=True,
            default=(
                f"quiet_seconds={config.quiet_seconds:g} "
                f"fixed_seconds={config.fixed_seconds:g}"
            ),
            placeholder="quiet_seconds=900 fixed_seconds=900",
            max_length=100,
        )
        self.triggers = discord.ui.TextInput(
            label="Triggers",
            required=True,
            default=(
                f"trigger_score={config.trigger_score} "
                f"trigger_join_count={config.trigger_join_count}"
            ),
            placeholder="trigger_score=16 trigger_join_count=10",
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
            config = self.protector.config_for(self.guild.id)
            config.window_seconds = self._positive_float(
                window_values["window_seconds"],
                "window_seconds",
            )
            config.early_message_window_seconds = self._positive_float(
                window_values["early_message_window_seconds"],
                "early_message_window_seconds",
            )
            config.quiet_seconds = self._positive_float(
                expiry_values["quiet_seconds"],
                "quiet_seconds",
            )
            config.fixed_seconds = self._positive_float(
                expiry_values["fixed_seconds"],
                "fixed_seconds",
            )
            config.trigger_score = self._positive_int(
                trigger_values["trigger_score"],
                "trigger_score",
            )
            config.trigger_join_count = self._positive_int(
                trigger_values["trigger_join_count"],
                "trigger_join_count",
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await self.protector.save_config(self.guild.id)
        
        if interaction.message:
            await interaction.message.edit(
                view=RaidConfigView(
                    protector=self.protector,
                    guild=self.guild,
                ),
            )
        await interaction.response.send_message("Updated raid settings.")

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
        self.configs = self._load_configs()
        self.join_events: list[JoinEvent] = []
        self.early_message_events: list[EarlyMessageEvent] = []
        self.guild_states: dict[int, GuildRaidState] = {}

    def _load_configs(self) -> dict[int, RaidConfig]:
        configs: dict[int, RaidConfig] = {}
        for (scope, account_type, account_id), account in self.bot.accounts.accounts.items():
            if scope == "global" or account_type != "guild":
                continue
            raw_config = account.get(RAID_CONFIG_ACCOUNT_KEY)
            if not isinstance(raw_config, dict):
                continue
            try:
                guild_id = int(scope)
            except (TypeError, ValueError):
                continue
            configs[guild_id] = self._load_config(raw_config)
        return configs

    def _float_config(self, config: dict[str, object], key: str, default: float) -> float:
        value = config.get(key)
        if not isinstance(value, (int, float, str)):
            return default
        try:
            return float(value)
        except ValueError:
            return default

    def _int_config(self, config: dict[str, object], key: str, default: int) -> int:
        value = config.get(key)
        if not isinstance(value, (int, str)):
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _load_config(self, config: dict[str, object]) -> RaidConfig:
        alert_channel_id = self._int_config(config, "alert_channel_id", 0)
        return RaidConfig(
            enabled=bool(config.get("enabled", False)),
            alert_channel_id=(
                alert_channel_id
                if config.get("alert_channel_id") is not None else
                None
            ),
            mode=self._mode(config.get("mode", DEFAULT_MODE)),
            window_seconds=self._float_config(config, "window_seconds", DEFAULT_WINDOW_SECONDS),
            quiet_seconds=self._float_config(config, "quiet_seconds", DEFAULT_QUIET_SECONDS),
            fixed_seconds=self._float_config(config, "fixed_seconds", DEFAULT_FIXED_SECONDS),
            early_message_window_seconds=self._float_config(config,
                "early_message_window_seconds",
                DEFAULT_EARLY_MESSAGE_WINDOW_SECONDS,
            ),
            trigger_score=self._int_config(config, "trigger_score", DEFAULT_TRIGGER_SCORE),
            trigger_join_count=self._int_config(config, "trigger_join_count", DEFAULT_TRIGGER_JOIN_COUNT),
        )

    def _mode(self, value: object) -> RaidMode:
        return value if value in ("quiet", "fixed", "manual") else DEFAULT_MODE

    def config_for(self, guild_id: int) -> RaidConfig:
        config = self.configs.get(guild_id)
        if config is not None:
            return config

        raw_config = self.bot.accounts.guild(guild_id).get(RAID_CONFIG_ACCOUNT_KEY)
        config = self._load_config(raw_config) if raw_config else RaidConfig()
        self.configs[guild_id] = config
        return config

    async def save_config(self, guild_id: int) -> None:
        config = self.config_for(guild_id)
        await self.bot.accounts.guild(guild_id).write(RAID_CONFIG_ACCOUNT_KEY, {
            "enabled": config.enabled,
            "alert_channel_id": config.alert_channel_id,
            "mode": config.mode,
            "window_seconds": config.window_seconds,
            "quiet_seconds": config.quiet_seconds,
            "fixed_seconds": config.fixed_seconds,
            "early_message_window_seconds": config.early_message_window_seconds,
            "trigger_score": config.trigger_score,
            "trigger_join_count": config.trigger_join_count,
        })

    async def configure(
        self,
        *,
        guild_id: int,
        alert_channel: discord.TextChannel | None,
    ) -> None:
        if alert_channel is not None:
            self.config_for(guild_id).alert_channel_id = alert_channel.id
        await self.save_config(guild_id)

    async def manual_activate(self, guild_id: int) -> None:
        state = self.state_for(guild_id)
        self._activate(guild_id, state, "manual activation")
        await self.alert(guild_id, "Raid mode manually activated.", self.score(guild_id), [])

    async def manual_deactivate(self, guild_id: int) -> None:
        state = self.state_for(guild_id)
        state.active = False
        state.expires_at = None
        await self.alert(guild_id, "Raid mode manually deactivated.", self.score(guild_id), [])

    async def on_member_join(self, member: discord.Member | discord.User) -> None:
        if not isinstance(member, discord.Member):
            return
        if self.should_skip_join_record(member):
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

        state = self.state_for(member.guild.id)
        await self.maybe_expire(member.guild.id, now)

        if state.active:
            self._activate(member.guild.id, state, "active suspicious join")
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

        config = self.config_for(member.guild.id)
        if not config.enabled or config.alert_channel_id is None:
            return

        score = self.score(member.guild.id)
        if self.is_triggered(member.guild.id, score):
            state.last_suspicious_at = now
            self._activate(member.guild.id, state, "automatic trigger")
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
        if self.should_skip_message_member(member):
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

        state = self.state_for(member.guild.id)
        await self.maybe_expire(member.guild.id, now)
        if state.active:
            self._activate(member.guild.id, state, "active suspicious message")
            return

        config = self.config_for(member.guild.id)
        if not config.enabled or config.alert_channel_id is None:
            return

        score = self.score(member.guild.id)
        if self.is_triggered(member.guild.id, score):
            state.last_suspicious_at = now
            self._activate(member.guild.id, state, "early message trigger")
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
            self.bot.accounts[member.id].local(member.guild.id).permissions.can_use(RAID_EXEMPT_CAPABILITY) or
            security_roles.has_role_id(member, security_roles.verified_role_id(self.bot, member.guild)) or
            security_roles.has_role_id(member, security_roles.quarantine_role_id(self.bot, member.guild))
        )

    def should_skip_join_record(self, member: discord.Member) -> bool:
        return (
            member.bot or
            self.bot.accounts[member.id].local(member.guild.id).permissions.can_use(RAID_EXEMPT_CAPABILITY) or
            security_roles.has_role_id(member, security_roles.quarantine_role_id(self.bot, member.guild))
        )

    def should_skip_message_member(self, member: discord.Member) -> bool:
        return (
            member.bot or
            self.bot.accounts[member.id].local(member.guild.id).permissions.can_use(RAID_EXEMPT_CAPABILITY) or
            security_roles.has_role_id(member, security_roles.quarantine_role_id(self.bot, member.guild))
        )

    def trim(self, now: float) -> None:
        window_seconds = max(
            [
                DEFAULT_WINDOW_SECONDS,
                DEFAULT_EARLY_MESSAGE_WINDOW_SECONDS,
                *[
                    max(config.window_seconds, config.early_message_window_seconds)
                    for config in self.configs.values()
                ],
            ]
        )
        keep_since = now - window_seconds
        self.join_events = [
            event for event in self.join_events
            if event.timestamp >= keep_since
        ]
        self.early_message_events = [
            event for event in self.early_message_events
            if event.timestamp >= keep_since
        ]

    def window_join_events(self, guild_id: int, now: float) -> list[JoinEvent]:
        window_start = now - self.config_for(guild_id).window_seconds
        return [
            event for event in self.join_events
            if event.guild_id == guild_id and event.timestamp >= window_start
        ]

    def window_message_events(self, guild_id: int, now: float) -> list[EarlyMessageEvent]:
        window_start = now - self.config_for(guild_id).window_seconds
        return [
            event for event in self.early_message_events
            if event.guild_id == guild_id and event.timestamp >= window_start
        ]

    def recent_join_for(self, guild_id: int, user_id: int, now: float) -> JoinEvent | None:
        window_start = now - self.config_for(guild_id).early_message_window_seconds
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
        total = len(messages) * EARLY_MESSAGE_SCORE
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

    def is_triggered(self, guild_id: int, score: RaidScore) -> bool:
        config = self.config_for(guild_id)
        return score.join_count > 0 and (
            score.score >= config.trigger_score or
            score.join_count >= config.trigger_join_count
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

    def _activate(self, guild_id: int, state: GuildRaidState, _reason: str) -> None:
        config = self.config_for(guild_id)
        now = time.monotonic()
        state.active = True
        state.active_since = state.active_since or now
        state.last_suspicious_at = now
        if config.mode == "fixed":
            state.expires_at = now + config.fixed_seconds
        elif config.mode == "quiet":
            state.expires_at = now + config.quiet_seconds
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
    ) -> list[QuarantineResult]:
        quarantined: list[QuarantineResult] = []
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
                removed_roles = await self.remove_quarantined_member_roles(
                    member,
                    quarantine_role=role,
                    reason=reason,
                )
                result = QuarantineResult(
                    member=member,
                    removed_roles=removed_roles,
                    reason=reason,
                    timestamp=int(time.time()),
                )
                await self.store_quarantine_result(result)
                if removed_roles:
                    self.logger.warning(
                        "Removed roles from quarantined member %s (%s): %s",
                        member,
                        member.id,
                        ", ".join(f"{removed.name} ({removed.id})" for removed in removed_roles),
                    )
            except discord.Forbidden:
                self.logger.warning(f"Missing permissions to quarantine {member} ({member.id}).")
            except discord.HTTPException:
                self.logger.exception(f"Failed to quarantine {member} ({member.id}).")
            else:
                quarantined.append(result)
                await self.alert_quarantine(result)
        return quarantined

    async def remove_quarantined_member_roles(
        self,
        member: discord.Member,
        *,
        quarantine_role: discord.Role,
        reason: str,
    ) -> list[discord.Role]:
        roles = self.removable_member_roles(member, quarantine_role=quarantine_role)
        if not roles:
            return []

        try:
            await member.remove_roles(*roles, reason=f"{reason}: quarantine role isolation")
            return roles
        except discord.Forbidden:
            self.logger.warning(
                "Missing permissions to remove roles from quarantined member %s (%s).",
                member,
                member.id,
            )
            return []
        except discord.HTTPException:
            self.logger.exception(
                "Bulk role removal failed for quarantined member %s (%s); retrying individually.",
                member,
                member.id,
            )

        removed_roles: list[discord.Role] = []
        for role in roles:
            try:
                await member.remove_roles(role, reason=f"{reason}: quarantine role isolation")
            except discord.Forbidden:
                self.logger.warning(
                    "Missing permissions to remove role %s (%s) from quarantined member %s (%s).",
                    role.name,
                    role.id,
                    member,
                    member.id,
                )
            except discord.HTTPException:
                self.logger.exception(
                    "Failed to remove role %s (%s) from quarantined member %s (%s).",
                    role.name,
                    role.id,
                    member,
                    member.id,
                )
            else:
                removed_roles.append(role)
        return removed_roles

    def removable_member_roles(
        self,
        member: discord.Member,
        *,
        quarantine_role: discord.Role,
    ) -> list[discord.Role]:
        bot_member = member.guild.me
        if bot_member is None:
            self.logger.warning(
                "Cannot remove roles from quarantined member %s (%s): bot member is unavailable.",
                member,
                member.id,
            )
            return []

        removable_roles: list[discord.Role] = []
        for role in member.roles:
            if (
                role.is_default() or
                role.managed or
                role.id == quarantine_role.id or
                role >= bot_member.top_role
            ):
                continue
            removable_roles.append(role)
        return removable_roles

    def record_quarantines(
        self,
        state: GuildRaidState,
        members: list[QuarantineResult],
    ) -> None:
        for result in members:
            if result.member.id not in state.recent_quarantined_ids:
                state.recent_quarantined_ids.append(result.member.id)
        state.recent_quarantined_ids = state.recent_quarantined_ids[-25:]

    async def store_quarantine_result(self, result: QuarantineResult) -> None:
        await self.bot.accounts[result.member.id].local(result.member.guild.id).write(
            QUARANTINE_ACCOUNT_KEY,
            {
                "removed_role_ids": [role.id for role in result.removed_roles],
                "reason": result.reason,
                "timestamp": result.timestamp,
            },
        )

    async def alert(
        self,
        guild_id: int,
        message: str,
        score: RaidScore,
        quarantined: list[QuarantineResult],
    ) -> None:
        config = self.config_for(guild_id)
        channel_id = config.alert_channel_id
        if channel_id is None:
            self.logger.warning(f"Raid alert skipped for guild {guild_id}: alert channel is not configured.")
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            self.logger.warning(f"Raid alert skipped for guild {guild_id}: alert channel {channel_id} is unavailable.")
            return
        mentions = ", ".join(result.member.mention for result in quarantined) or "None"
        try:
            await channel.send(view=RaidAlertView(
                title=message,
                config=config,
                score=score,
                quarantined=mentions,
            ))
        except discord.HTTPException:
            self.logger.exception(f"Failed to send raid alert to channel {channel_id}.")

    async def alert_quarantine(self, result: QuarantineResult) -> None:
        channel_id = self.config_for(result.member.guild.id).alert_channel_id
        if channel_id is None:
            self.logger.warning(
                "Raid quarantine alert skipped for guild %s: alert channel is not configured.",
                result.member.guild.id,
            )
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            self.logger.warning(
                "Raid quarantine alert skipped for guild %s: alert channel %s is unavailable.",
                result.member.guild.id,
                channel_id,
            )
            return
        try:
            await channel.send(view=RaidQuarantineView(result=result))
        except discord.HTTPException:
            self.logger.exception(
                "Failed to send raid quarantine alert for %s (%s) to channel %s.",
                result.member,
                result.member.id,
                channel_id,
            )

    def role_configuration_error(self, guild: discord.Guild) -> str | None:
        quarantine_role = security_roles.quarantine_role(self.bot, guild)
        if quarantine_role is None:
            return "Raid protection needs a configured quarantine role first: `/manage raid action:config`."
        return security_roles.manageable_role_error(
            guild,
            quarantine_role,
            "Quarantine role",
        )


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


def _role_mentions(roles: list[discord.Role]) -> str:
    if not roles:
        return "None"
    return ", ".join(role.mention for role in roles)


class RaidQuarantineView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        result: QuarantineResult,
    ) -> None:
        super().__init__(timeout=None)
        member = result.member
        account_created = int(member.created_at.timestamp())
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## User Quarantined"),
            discord.ui.Separator(),
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "\n".join([
                        f"User: {member.mention} `{member.id}`",
                        f"Account created: <t:{account_created}:F> `<t:{account_created}:R>`",
                        f"Reason: `{result.reason}`",
                        f"Quarantined at: <t:{result.timestamp}:F>",
                        f"Previous roles: {_role_mentions(result.removed_roles)}",
                    ])
                ),
                accessory=cast(discord.ui.Item[discord.ui.LayoutView], RaidUnquarantineButton(
                    guild_id=member.guild.id,
                    user_id=member.id,
                )),
            ),
        ))


class RaidUnquarantineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=rf"{UNQUARANTINE_CUSTOM_ID_PREFIX}:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
    ) -> None:
        super().__init__(discord.ui.Button(
            label="Unquarantine",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{UNQUARANTINE_CUSTOM_ID_PREFIX}:{guild_id}:{user_id}",
        ))
        self.guild_id = guild_id
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[discord.ui.View | discord.ui.LayoutView],
        match,
    ) -> "RaidUnquarantineButton":
        return cls(
            guild_id=int(match["guild_id"]),
            user_id=int(match["user_id"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if not isinstance(bot, BotCore):
            await interaction.response.send_message(
                "This button is not available right now.",
                ephemeral=True,
            )
            return
        if not bot.accounts[interaction.user.id].local(interaction.guild_id).permissions.can_use(
            RAID_UNQUARANTINE_CAPABILITY,
        ):
            await interaction.response.send_message(
                "You are not authorized to unquarantine users.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message(
                "This unquarantine button belongs to another server.",
                ephemeral=True,
            )
            return
        member = guild.get_member(self.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.user_id)
            except discord.NotFound:
                await self._clear_record(bot)
                await interaction.response.send_message(
                    "That member is no longer in this server. Cleared the stored quarantine record.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                await interaction.response.send_message(
                    "I could not fetch that member.",
                    ephemeral=True,
                )
                return

        raw_record = bot.accounts[self.user_id].local(self.guild_id).get(QUARANTINE_ACCOUNT_KEY)
        record = raw_record if isinstance(raw_record, dict) else {}
        raw_role_ids = record.get("removed_role_ids", [])
        role_ids = [
            int(role_id)
            for role_id in raw_role_ids
            if isinstance(role_id, int) or (isinstance(role_id, str) and role_id.isdigit())
        ]
        restorable_roles = [
            role
            for role_id in role_ids
            if (role := guild.get_role(role_id)) is not None and self._can_manage_role(guild, role)
        ]
        skipped_roles = [
            str(role_id)
            for role_id in role_ids
            if guild.get_role(role_id) is None or not self._can_manage_role(guild, guild.get_role(role_id))
        ]
        quarantine_role = security_roles.quarantine_role(bot, guild)
        try:
            if restorable_roles:
                await member.add_roles(
                    *restorable_roles,
                    reason=f"Raid unquarantine by {interaction.user} ({interaction.user.id})",
                )
            if quarantine_role is not None and any(role.id == quarantine_role.id for role in member.roles):
                await member.remove_roles(
                    quarantine_role,
                    reason=f"Raid unquarantine by {interaction.user} ({interaction.user.id})",
                )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to restore roles or remove quarantine.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Unquarantine failed while updating roles.",
                ephemeral=True,
            )
            return

        await self._clear_record(bot)
        restored = _role_mentions(restorable_roles)
        skipped = ", ".join(skipped_roles) or "None"
        await interaction.response.send_message(
            "\n".join([
                f"Unquarantined {member.mention}.",
                f"Restored roles: {restored}",
                f"Skipped missing/unmanageable role IDs: `{skipped}`",
            ]),
            ephemeral=True,
        )

    def _can_manage_role(self, guild: discord.Guild, role: discord.Role | None) -> bool:
        if role is None:
            return False
        bot_member = guild.me
        return (
            bot_member is not None and
            not role.is_default() and
            not role.managed and
            role < bot_member.top_role
        )

    async def _clear_record(self, bot: BotCore) -> None:
        async with bot.accounts.lock:
            local_account = bot.accounts._account_locked("user", str(self.user_id), self.guild_id)
            local_account.pop(QUARANTINE_ACCOUNT_KEY, None)
            bot.accounts._save_sync()


async def setup(bot: BotCore) -> None:
    protector = RaidProtector(bot)
    manage = groups.manage(bot)
    bot.accounts.capabilities.register(RAID_UNQUARANTINE_CAPABILITY)
    bot.accounts.capabilities.register(RAID_EXEMPT_CAPABILITY)
    bot.add_dynamic_items(RaidUnquarantineButton)

    @bot.member_join_callback
    async def on_member_join(member: discord.Member | discord.User) -> None:
        await protector.on_member_join(member)

    @bot.message_callback
    async def on_message(message: discord.Message) -> None:
        await protector.on_message(message)

    @manage.command(
        name="raid",
        description="Manage raid protection",
        capabilities=[RAID_MANAGE_CAPABILITY],
        defer=False,
    )
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
            await protector.configure(guild_id=guild.id, alert_channel=alert_channel)
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
                    config=protector.config_for(guild.id),
                    state=protector.state_for(guild.id),
                    score=protector.score(guild.id),
                ),
                response=True,
                ephemeral=True,
            )
            return

        if action == "on":
            config = protector.config_for(guild.id)
            if config.alert_channel_id is None:
                await bot.discord.send(
                    "Raid protection needs an alert channel first: `/manage raid action:config alert_channel:#channel`.",
                    response=True,
                    ephemeral=True,
                )
                return
            role_error = protector.role_configuration_error(guild)
            if role_error is not None:
                await bot.discord.send(
                    role_error,
                    response=True,
                    ephemeral=True,
                )
                return
            config.enabled = True
            if alert_channel is not None:
                if alert_channel.guild.id != guild.id:
                    await bot.discord.send(
                        "`alert_channel` must be in this server.",
                        response=True,
                        ephemeral=True,
                    )
                    return
                config.alert_channel_id = alert_channel.id
            await protector.save_config(guild.id)
            await bot.discord.send(
                "Raid protection automation enabled.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "off":
            protector.config_for(guild.id).enabled = False
            await protector.save_config(guild.id)
            await bot.discord.send(
                "Raid protection automation disabled.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "activate":
            if protector.config_for(guild.id).alert_channel_id is None:
                await bot.discord.send(
                    "Raid protection needs an alert channel first: `/manage raid action:config alert_channel:#channel`.",
                    response=True,
                    ephemeral=True,
                )
                return
            role_error = protector.role_configuration_error(guild)
            if role_error is not None:
                await bot.discord.send(
                    role_error,
                    response=True,
                    ephemeral=True,
                )
                return
            await protector.manual_activate(guild.id)
            await bot.discord.send(
                "Raid mode activated.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "deactivate":
            await protector.manual_deactivate(guild.id)
            await bot.discord.send(
                "Raid mode deactivated.",
                response=True,
                ephemeral=True,
            )
            return
