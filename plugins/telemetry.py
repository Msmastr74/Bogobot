from collections import Counter
from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING, Literal, TypedDict

import discord

class CommandTelemetryBase(TypedDict):
    interaction_id: int
    command: str
    user_id: int
    user_name: str
    channel_id: int | None
    time: int

class CommandTelemetryStart(CommandTelemetryBase):
    phase: Literal["start"]

class CommandTelemetryEnd(CommandTelemetryBase):
    phase: Literal["end"]
    status: Literal["ok", "unauthorized", "error"]
    duration_ms: float
    error: str | None

CommandTelemetryEvent = CommandTelemetryStart | CommandTelemetryEnd

@dataclass
class UserUsage:
    user_id: int
    name: str
    total: int = 0
    commands: Counter[str] = field(default_factory=Counter)

if TYPE_CHECKING:
    from main import BotCore


async def setup(bot: "BotCore"):
    import groups

    manage = groups.manage(bot)
    telemetry_path = Path(bot.config.get("telemetry_path", "telemetry.json"))
    recent_actions: deque["CommandTelemetryEnd"] = deque(maxlen=200)
    saved_actions: list["CommandTelemetryEnd"] = []
    active: dict[tuple[int, str], "CommandTelemetryEvent"] = {}

    def load_actions() -> None:
        if not telemetry_path.exists():
            return

        try:
            data = json.loads(telemetry_path.read_text())
        except (OSError, json.JSONDecodeError):
            bot.logger.warning(f"Could not read telemetry file: {telemetry_path}")
            return

        if not isinstance(data, list):
            return

        for item in data:
            if not isinstance(item, dict):
                continue

            if item.get("phase") != "end":
                continue

            try:
                action: CommandTelemetryEnd = {
                    "interaction_id": int(item["interaction_id"]),
                    "command": str(item["command"]),
                    "user_id": int(item["user_id"]),
                    "user_name": str(item["user_name"]),
                    "channel_id": int(item["channel_id"]) if item.get("channel_id") is not None else None,
                    "time": int(item["time"]),
                    "phase": "end",
                    "status": item["status"],
                    "duration_ms": float(item["duration_ms"]),
                    "error": str(item["error"]) if item.get("error") is not None else None,
                }
            except (KeyError, TypeError, ValueError):
                continue

            if action["status"] not in ("ok", "unauthorized", "error"):
                continue

            saved_actions.append(action)
            recent_actions.append(action)

    def save_actions() -> None:
        try:
            if telemetry_path.parent != Path("."):
                telemetry_path.parent.mkdir(parents=True, exist_ok=True)

            tmp_path = telemetry_path.with_suffix(f"{telemetry_path.suffix}.tmp")
            tmp_path.write_text(json.dumps(saved_actions))
            tmp_path.replace(telemetry_path)
        except OSError as e:
            bot.logger.warning(f"Could not save telemetry file: {e}")

    def public_actions() -> list["CommandTelemetryEnd"]:
        return [
            action
            for action in saved_actions
            if action["status"] == "ok" and not action["command"].startswith(
                manage.group.name + " "
            )
        ]

    valid_public_commands: set[str] = set()
    
    @bot.init_callback
    async def init():
        for command in bot.tree.walk_commands():
            if isinstance(command, discord.app_commands.Group):
                continue
            if manage.group in (command.parent, command.root_parent):
                continue
            valid_public_commands.add(command.qualified_name)

    load_actions()

    @bot.command_telemetry_callback
    def record_command(event: "CommandTelemetryEvent"):
        key = (event["interaction_id"], event["command"])

        if event["phase"] == "start":
            active[key] = event
            return
        
        if event["phase"] == "end":
            active.pop(key, None)
            recent_actions.append(event)
            saved_actions.append(event)
            save_actions()
            return

        raise ValueError(f"Invalid telemetry event phase: {event['phase']}")

    @manage.command(
        name="telemetry",
        description="Show recent bot command activity",
        eph=True,
    )
    async def telemetry(interaction: discord.Interaction, action_count: int = 20):
        if action_count < 1:
            await bot.discord.send("Action count must be at least 1.", response=True)
            return
        recent = list(recent_actions)[-action_count:][::-1]

        if not recent:
            body = "No telemetry yet."
        else:
            lines = []
            for item in recent:
                timestamp = f"<t:{item['time']}:T>"
                command = item["command"]
                status = item["status"]
                duration = item["duration_ms"]
                channel_id = item["channel_id"]
                user = f"<@{item['user_id']}>"
                channel = f"<#{channel_id}>" if channel_id is not None else "DM"

                line = (
                    f"{timestamp} {status} `{duration}ms` "
                    f"`/{command}` {user} in {channel}"
                )

                error = item["error"]
                if error:
                    line += f" | error={error}"

                lines.append(line)

            body = "\n".join(lines)

        await bot.discord.send_embed(
            title="Recent Command Telemetry",
            contents=body[:4000],
            color=discord.Color.dark_teal(),
            allowed_mentions=discord.AllowedMentions.none(),
            response=True
        )

    @bot.setup.command(
        name="usage",
        description="Show command usage totals",
        perm_requirement=0,
        defer=False,
    )
    async def usage(interaction: discord.Interaction, command: str | None = None):
        requested_commands: list[str] | None = None

        if command is not None and command.strip():
            requested_commands = [
                item.strip().removeprefix("/")
                for item in command.split(",")
                if item.strip()
            ]

            valid = valid_public_commands
            invalid = [
                item for item in requested_commands if item not in valid
            ]

            if invalid:
                valid_list = ", ".join(sorted(valid))
                plural = "s" if len(invalid) > 1 else ""
                await bot.discord.send(
                    f"Unknown command{plural}: {', '.join(invalid)}\nValid commands: {valid_list}",
                    response=True, ephemeral=True
                )
                return
        await interaction.response.defer()

        actions = public_actions()
        if requested_commands is not None:
            requested = set(requested_commands)
            actions = [
                action
                for action in actions
                if action["command"] in requested
            ]

        users: dict[int, UserUsage] = {}
        for action in actions:
            user = users.setdefault(
                action["user_id"],
                UserUsage(user_id=action["user_id"], name=action["user_name"]),
            )
            user.name = action["user_name"]
            user.total += 1
            user.commands[action["command"]] += 1

        ranked = sorted(
            users.values(),
            key=lambda item: item.total,
            reverse=True,
        )[:10]

        if not ranked:
            body = "No usage data for that query."
        else:
            lines = []
            single_command = requested_commands is not None and len(requested_commands) == 1
            total_width = max(len(str(user.total)) for user in ranked)

            for index, user in enumerate(ranked, start=1):
                total = f"`{str(user.total).ljust(total_width)}`"
                mention = f"<@{user.user_id}>"

                if single_command:
                    lines.append(f"{index}. {total} {mention}")
                else:
                    top_command, top_total = user.commands.most_common(1)[0]
                    lines.append(
                        f"{index}. {total} {mention} - top: `/{top_command}` ({top_total})"
                    )

            body = "\n".join(lines)

        title = "Usage"
        if requested_commands:
            title = f"Usage: {', '.join('/' + command for command in requested_commands)}"

        await bot.discord.send_embed(
            title=title,
            contents=body[:4000],
            color=discord.Color.blurple(),
            allowed_mentions=discord.AllowedMentions.none(),
            response=True
        )
