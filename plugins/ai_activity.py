import asyncio
import re
from typing import TYPE_CHECKING, Any, Optional, Required, TypedDict, cast

import discord

from utils.scheduler import ChannelScheduler, Schedule
from utils.type import T

if TYPE_CHECKING:
    from bogobot_core import BotCore

from plugins.ai import (
    ContextRequestExecutor,
    MAX_REPLY_CHARS,
    MessageInteraction,
    ai_enabled,
    ai_on_break,
    capture_interaction_output,
    chunk_text,
)
from utils.ai import ai as ai_core
from datetime import datetime, timedelta, timezone
from utils import groups


async def trigger_ai_activity(
    bot: "BotCore",
    channel: 'discord.abc.MessageableChannel',
    purpose: str,
) -> list[discord.Message]:
    if bot.user is None or not ai_enabled(bot) or ai_on_break():
        return []

    bot_user = bot.get_user(bot.user.id)
    if bot_user is None:
        return []

    channel_id = getattr(channel, "id", None)
    if not isinstance(channel_id, int):
        return []

    requested_context = await ContextRequestExecutor(bot).execute(channel, purpose)
    matches = await ai_core.ai_activity(
        purpose,
        channel_id=channel_id,
        requested_context=requested_context,
    )
    if not matches:
        return []

    sent_messages: list[discord.Message] = []
    followup_only = False
    for match in matches:
        if match.reply is not None:
            reply = ai_core.visual_reply(match.reply)
            if reply is None:
                continue
            chunks = chunk_text(reply, MAX_REPLY_CHARS)
            if len(chunks) < 1:
                continue

            sent_message: discord.Message | None = None
            for chunk in chunks:
                raw_message = await cast_channel(channel).send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if isinstance(raw_message, discord.Message):
                    sent_message = raw_message
                    sent_messages.append(raw_message)
            if sent_message is not None:
                followup_only = True
                ai_core.context.record_message(
                    "assistant",
                    match.reply,
                    sent_message,
                    channel_id=channel_id,
                )
            continue

        if match.action is None:
            continue

        interaction = MessageInteraction(
            bot,
            channel,
            match.command_name,
            user=bot_user,
            guild=getattr(channel, "guild", None),
            followup_only=followup_only,
        )
        async with capture_interaction_output(interaction) as output_messages:
            await bot.setup._run_command(
                interaction,
                match.action,
                (),
                match.kwargs or {},
                perm_requirement=match.context.get("perm_requirement", 0),
                eph=False,
                defer=False,
            )
        if output_messages:
            followup_only = True
            sent_messages.extend(output_messages)
        ai_core.context.record_message(
            "assistant",
            ai_core.context.format_command_call(match.command_name, match.kwargs),
            output_messages[-1] if output_messages else None,
            channel_id=channel_id,
        )

    return sent_messages


def cast_channel(channel: 'discord.abc.MessageableChannel') -> Any:
    return channel

class AISchedule(TypedDict, total=False):
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    weekday: Optional[int]
    hour: Optional[int]
    minute: Required[int]
    purpose: Required[str]

def get_next_leap_year(start_year: int) -> int:
    year = start_year
    while not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        year += 1
    return year

def default_to(value: T | None, default: T) -> T:
    return value if value is not None else default

def calculate_next_time(schedule: AISchedule, start_from: datetime, *, max_years: int = 100) -> datetime | None:
    current = start_from.replace(microsecond=0)

    calc_hr = schedule.get("hour")
    calc_min = schedule.get("minute")

    if calc_hr is None:
        calc_hr = current.hour
        if schedule.get("minute") is not None and calc_min <= current.minute:
            calc_hr += 1
            if calc_hr == 24:
                current = current + timedelta(days=1)
                calc_hr = 0

    try:
        candidate = current.replace(hour=calc_hr, minute=calc_min, second=0)
    except ValueError:
        return None

    if candidate <= current:
        current = (current + timedelta(days=1)).replace(hour=calc_hr, minute=calc_min, second=0)
    else:
        current = candidate

    loops = int(365.2425 * max_years)
    if (target_year := schedule.get("year")) is not None:
        if current.year > target_year:
            return None
        if current.year < target_year:
            current = current.replace(year=target_year, month=1, day=1)
        loops = 366
    elif schedule.get("month") == 2 and schedule.get("day") == 29:
        leap_year = get_next_leap_year(current.year)
        if current.year == leap_year and current.month > 2:
            leap_year = get_next_leap_year(current.year + 1)
        
        current = current.replace(year=leap_year, month=2, day=29)
        loops = max_years
    for _ in range(loops):
        if (m := schedule.get("month")) is not None and current.month != m:
            if current.month > m:
                current = current.replace(year=current.year + 1, month=m, day=1)
            else:
                current = current.replace(month=m, day=1)

        day_match = (d := schedule.get("day")) is None or current.day == d
        wkday_match = (w := schedule.get("weekday")) is None or current.weekday() == w
        
        if not (day_match and wkday_match):
            try:
                if schedule.get("month") == 2 and schedule.get("day") == 29:
                    current = current.replace(year=get_next_leap_year(current.year + 1), month=2, day=29)
                else:
                    current += timedelta(days=1)
            except (OverflowError, ValueError):
                return None
            continue
        return current
    return None

class PendingSchedule(TypedDict):
    channel_id: int
    next_event: datetime
    purpose: str

class AIScheduler(ChannelScheduler[AISchedule]):
    def __init__(self, bot: "BotCore"):
        self.bot = bot
        super().__init__(
            bot,
            schedules=self._load_schedules(),
            save_schedules=self._save_schedules,
            logger=bot.logger.getChild("AIScheduler"),
        )
        self._worker_task = None
    
    def start(self):
        if self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(self._worker())

    def stop(self):
        if not self._worker_task:
            return
        self._worker_task.cancel()
        self._worker_task = None

    async def _save_schedules(self, data: dict[str, Any]):
        self.bot.config["ai_schedules"] = data
        await self.bot.save_config()
    
    def _load_schedules(self) -> dict[str, Any]:
        schedule_data = self.bot.config.get("ai_schedules")

        if isinstance(schedule_data, dict):
            return schedule_data
        
        return {}
    
    async def _worker(self):
        try:
            start_time = discord.utils.utcnow()
            while True:
                pending_tasks: list[PendingSchedule] = []
                channels = await self.get_channels()
                for channel_id, schedules in channels.items():
                    for schedule in schedules:
                        next_event = calculate_next_time(schedule["payload"], start_time)
                        if next_event is not None:
                            pending_tasks.append({
                                "channel_id": channel_id,
                                "next_event": next_event,
                                "purpose": schedule["payload"]["purpose"]
                            })
                        else:
                            await self.remove_schedule(channel_id, schedule["id"])
                pending_tasks.sort(key=lambda x: x["next_event"])
                
                end_time = start_time + timedelta(minutes=1)
                for task in pending_tasks:
                    if task["next_event"] > end_time:
                        break
                    await asyncio.sleep(max(0, (task["next_event"] - discord.utils.utcnow()).total_seconds()))
                    channel = self.bot.get_channel(task["channel_id"])
                    if channel is None or  not hasattr(channel, "send"):
                        continue
                    channel = cast('discord.abc.MessageableChannel', channel)
                    asyncio.create_task(self._run_trigger(channel, task["purpose"]))
                await asyncio.sleep(max(0, (end_time - discord.utils.utcnow()).total_seconds()))
                start_time = end_time
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.logger:
                self.logger.exception("Error occured in AIScheduler worker task.")

    async def _run_trigger(
        self,
        channel: discord.abc.MessageableChannel,
        purpose: str,
    ) -> None:
        try:
            await trigger_ai_activity(self.bot, channel, purpose)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.logger:
                self.logger.exception("Error occurred while running scheduled AI activity.")

def parse_activity_time_to_schedule(value: str, purpose: str) -> AISchedule | None:
    """
    Parses a string input and constructs a structured AISchedule layout.
    Supports: 
      - Absolute dates (e.g., "30m", "<t:123456789>", "2026-05-28T15:30:00Z")
      - Recurring patterns (e.g., "minute:30", "hour:12 minute:0", "weekday:4 hour:18 minute:30")
    """
    value = value.strip().casefold()
    now = discord.utils.utcnow()

    # 1. Handle Recurring Parameter Keywords (e.g., "hour:14 minute:30")
    # Matches strings containing patterns like key:value
    if ":" in value and not value.startswith("<t:"):
        schedule: AISchedule = { "minute": 0, "purpose": purpose }
        # Simple space-separated parameter scanner
        tokens = value.split()
        valid_keys = {"year", "month", "day", "weekday", "hour", "minute"}
        
        for token in tokens:
            if ":" not in token:
                continue
            k, v = token.split(":", 1)
            if k in valid_keys and v.isdigit():
                schedule[k] = int(v)

        if "minute" not in schedule:
            schedule["minute"] = 0

        return schedule


    # 2. Relative offset helper (e.g., "30m", "2h")
    relative_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhd])", value)
    if relative_match is not None:
        amount = float(relative_match[1])
        unit = relative_match[2]
        if unit == "m":
            target = now + timedelta(minutes=amount)
        elif unit == "h":
            target = now + timedelta(hours=amount)
        else:
            target = now + timedelta(days=amount)
        return datetime_to_absolute_schedule(target, purpose)

    # 3. Discord absolute timestamp format (e.g., <t:1779991200:F>)
    timestamp_match = re.fullmatch(r"<t:(\d{1,12})(?::[tTdDfFRsS])?>", value)
    if timestamp_match is not None:
        target = datetime.fromtimestamp(int(timestamp_match[1]), timezone.utc)
        return datetime_to_absolute_schedule(target, purpose)

    # 4. Raw Unix Epoch variant (e.g., 1779991200)
    if re.fullmatch(r"\d{1,12}", value):
        target = datetime.fromtimestamp(int(value), timezone.utc)
        return datetime_to_absolute_schedule(target, purpose)

    # 5. Standard ISO String structures
    try:
        parsed = datetime.fromisoformat(value.replace("z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        target = parsed.astimezone(timezone.utc)
        return datetime_to_absolute_schedule(target, purpose)
    except ValueError:
        return None

def datetime_to_absolute_schedule(dt: datetime, purpose: str) -> AISchedule:
    """Converts a specific, locked down datetime point into a fully bound AISchedule."""
    return {
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
        "purpose": purpose
    }


def interaction_messageable_channel(
    interaction: discord.Interaction,
) -> 'discord.abc.MessageableChannel | None':
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        return None
    return cast('discord.abc.MessageableChannel', channel)

async def setup(bot: "BotCore"):
    ai_activity = groups.ai_activity(bot)
    scheduler = AIScheduler(bot)

    @ai_activity.command(
        name="schedule",
        description="Schedule a recurring or one-off AI activity trigger"
    )
    async def schedule_cmd(interaction: discord.Interaction, when: str, purpose: str):
        channel = interaction_messageable_channel(interaction)
        if channel is None:
            await bot.discord.send("This command needs an accessible channel.", response=True, ephemeral=True)
            return
        
        purpose = purpose.strip()
        if not purpose:
            await bot.discord.send("Purpose is required.", response=True, ephemeral=True)
            return

        schedule_payload = parse_activity_time_to_schedule(when, purpose)
        if schedule_payload is None:
            await bot.discord.send(
                "Provide a absolute timestamp ('30m', Unix timestamp, ISO) "
                "or a structural recurring pattern like `hour:12 minute:30` (minute is required).",
                response=True,
                ephemeral=True,
            )
            return

        await scheduler.add_schedule(channel.id, { "payload": schedule_payload })
        
        first_run = calculate_next_time(schedule_payload, discord.utils.utcnow())
        
        if first_run is not None:
            await bot.discord.send(
                f"Successfully scheduled activity! Initial target runtime: <t:{int(first_run.timestamp())}:F>.",
                response=True,
                ephemeral=True,
            )
        else:
            await bot.discord.send(
                "Schedule added, but it represents an impossible date combination or a past year block.",
                response=True,
                ephemeral=True,
            )

    @ai_activity.command(
        name="trigger",
        description="Trigger AI activity now"
    )
    async def trigger_cmd(interaction: discord.Interaction, purpose: str):
        channel = interaction_messageable_channel(interaction)
        if channel is None:
            await bot.discord.send("This command needs an accessible channel.", response=True, ephemeral=True)
            return

        purpose = purpose.strip()
        if not purpose:
            await bot.discord.send("Purpose is required.", response=True, ephemeral=True)
            return

        await bot.discord.send("Triggering AI activity.", response=True, ephemeral=True)
        await trigger_ai_activity(bot, channel, purpose)

    @ai_activity.command(
        name="list",
        description="List all scheduled AI activities for this channel",
        perm_requirement=0
    )
    async def list_cmd(interaction: discord.Interaction):
        channel = interaction_messageable_channel(interaction)
        if channel is None:
            await bot.discord.send("This command needs an accessible channel.", response=True, ephemeral=True)
            return

        channels = await scheduler.get_channels()
        channel_schedules = channels.get(channel.id, [])

        view = AISchedulesView(channel.id, channel_schedules)
        await bot.discord.send(view=view, response=True, ephemeral=True)
    
    @ai_activity.command(
        name="remove",
        description="Remove a scheduled AI activity by its unique ID",
        eph=True,
        defer=False,
    )
    async def remove_cmd(interaction: discord.Interaction, id: str):
        channel = interaction_messageable_channel(interaction)
        if channel is None:
            await bot.discord.send("This command needs an accessible channel.", response=True, ephemeral=True)
            return

        target_id = id.strip()
        if not target_id:
            await bot.discord.send("A valid schedule ID is required.", response=True, ephemeral=True)
            return

        channels = await scheduler.get_channels()
        channel_schedules = channels.get(channel.id, [])
        
        exists = any(item["id"] == target_id for item in channel_schedules)

        if not exists:
            await bot.discord.send(
                f"❌ **Error:** No active schedule with ID `{target_id}` was found in <#{channel.id}>.\n"
                f"Use `/ai_activity list` to check active IDs for this channel.",
                response=True,
                ephemeral=True,
            )
            return

        await scheduler.remove_schedule(channel.id, target_id)

        await bot.discord.send(
            f"🗑️ **Schedule Removed Successfully**\n"
            f"The AI automation rule matching ID `{target_id}` has been deleted from <#{channel.id}> configuration registers.",
            response=True,
            ephemeral=True,
        )

    @bot.init_callback
    async def init():
        scheduler.start()
    
    @bot.close_callback
    async def close():
        scheduler.stop()


class AISchedulesView(discord.ui.LayoutView):
    def __init__(self, channel_id: int, schedules: list[Schedule[AISchedule]]) -> None:
        super().__init__(timeout=None)
        if not schedules:
            self.add_item(discord.ui.TextDisplay(
                "There are no scheduled AI activities configured for this channel."
            ))
            return

        self.add_item(discord.ui.TextDisplay(
            f"### Scheduled AI Activities for <#{channel_id}>:\n"
        ))

        now = discord.utils.utcnow()
        for idx, item in enumerate(schedules, start=1):
            schedule_id = item["id"]
            payload = item["payload"]
            
            # 1. Parse rules into a human-readable config row
            rule_parts = []
            for key in ["year", "month", "day", "weekday", "hour", "minute", "second"]:
                if (val := payload.get(key)) is not None:
                    if key == "weekday":
                        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                        val = days[val]
                    rule_parts.append(f"**{key}**: {val}")
            
            rule_summary = ", ".join(rule_parts) if rule_parts else "Every minute"
            purpose_text = payload.get("purpose", "No purpose declared")

            # 2. Calculate the dynamic next execution timestamp
            next_run = calculate_next_time(payload, now)
            next_run_str = f"<t:{int(next_run.timestamp())}:F>" if next_run else "*Never (Expired/Invalid)*"

            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(
                    f"**{idx}. ID:** `{schedule_id}`\n"
                    f"⚙️ **Config:** {rule_summary}\n"
                    f"🎯 **Context:** {purpose_text}\n"
                    f"⏰ **Next Run:** {next_run_str}\n"
                )
            ))
