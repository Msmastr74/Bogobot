import asyncio
import datetime
import re
from typing import TYPE_CHECKING, Any, cast

import discord

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


def interaction_messageable_channel(
    interaction: discord.Interaction,
) -> 'discord.abc.MessageableChannel | None':
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        return None
    return cast('discord.abc.MessageableChannel', channel)


async def setup(bot: "BotCore"):
    activity = bot.setup.group("ai_activity", "AI activity controls")
    scheduled_tasks: set[asyncio.Task[None]] = set()

    @activity.command(
        name="schedule",
        description="Schedule an AI activity trigger",
        eph=True,
        defer=False,
    )
    async def schedule(interaction: discord.Interaction, when: str, purpose: str):
        channel = interaction_messageable_channel(interaction)
        if channel is None:
            await bot.discord.send("This command needs a channel.", response=True, ephemeral=True)
            return

        target_time = parse_activity_time(when)
        now = discord.utils.utcnow()
        if target_time is None:
            await bot.discord.send(
                "Use a Discord timestamp, Unix timestamp, ISO datetime, or minutes like `30m`.",
                response=True,
                ephemeral=True,
            )
            return
        if target_time < now or target_time > now + datetime.timedelta(hours=48):
            await bot.discord.send(
                "The scheduled time must be between now and 48 hours from now.",
                response=True,
                ephemeral=True,
            )
            return

        purpose = purpose.strip()
        if not purpose:
            await bot.discord.send("Purpose is required.", response=True, ephemeral=True)
            return

        task = asyncio.create_task(run_scheduled_activity(channel, target_time, purpose))
        scheduled_tasks.add(task)
        task.add_done_callback(scheduled_tasks.discard)
        await bot.discord.send(
            f"Scheduled AI activity for <t:{int(target_time.timestamp())}:F>.",
            response=True,
            ephemeral=True,
        )

    @activity.command(
        name="trigger",
        description="Trigger AI activity now",
        eph=True,
        defer=False,
    )
    async def trigger(interaction: discord.Interaction, purpose: str):
        channel = interaction_messageable_channel(interaction)
        if channel is None:
            await bot.discord.send("This command needs a channel.", response=True, ephemeral=True)
            return

        purpose = purpose.strip()
        if not purpose:
            await bot.discord.send("Purpose is required.", response=True, ephemeral=True)
            return

        await bot.discord.send("Triggering AI activity.", response=True, ephemeral=True)
        await trigger_ai_activity(bot, channel, user_activity_purpose(purpose))

    async def run_scheduled_activity(
        channel: 'discord.abc.MessageableChannel',
        target_time: datetime.datetime,
        purpose: str,
    ) -> None:
        delay = max(0.0, (target_time - discord.utils.utcnow()).total_seconds())
        await asyncio.sleep(delay)
        await trigger_ai_activity(bot, channel, user_activity_purpose(purpose))

    @bot.close_callback
    async def close():
        for task in list(scheduled_tasks):
            task.cancel()


def user_activity_purpose(purpose: str) -> str:
    return f"User-scheduled AI activity. Purpose: {purpose}"


def parse_activity_time(value: str) -> datetime.datetime | None:
    value = value.strip()
    now = discord.utils.utcnow()

    relative_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhd])", value, re.IGNORECASE)
    if relative_match is not None:
        amount = float(relative_match[1])
        unit = relative_match[2].casefold()
        if unit == "m":
            return now + datetime.timedelta(minutes=amount)
        if unit == "h":
            return now + datetime.timedelta(hours=amount)
        return now + datetime.timedelta(days=amount)

    timestamp_match = re.fullmatch(r"<t:(\d{1,12})(?::[tTdDfFRsS])?>", value)
    if timestamp_match is not None:
        return datetime.datetime.fromtimestamp(int(timestamp_match[1]), datetime.timezone.utc)

    if re.fullmatch(r"\d{1,12}", value):
        return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)

    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)
