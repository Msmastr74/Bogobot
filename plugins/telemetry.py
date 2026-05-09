from collections import Counter, defaultdict, deque
import asyncio
import contextlib
from dataclasses import dataclass
import heapq
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

import discord

class CommandTelemetryBase(TypedDict):
    interaction_id: int
    command: str
    user_id: int
    username: str
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
    total: int
    commands: Counter[str]

if TYPE_CHECKING:
    from main import BotCore


async def setup(bot: "BotCore"):
    import groups

    manage = groups.manage(bot)
    telemetry_path = Path(bot.config.get("telemetry_path", "telemetry.jsonl"))
    recent_limit = max(1, int(bot.config.get("telemetry_recent_limit", 200)))
    flush_interval = max(0.1, float(bot.config.get("telemetry_flush_interval", 2)))
    recent_actions: deque["CommandTelemetryEnd"] = deque(maxlen=recent_limit)
    active: dict[tuple[int, str], "CommandTelemetryEvent"] = {}
    pending_lines: list[str] = []
    flush_task: asyncio.Task[None] | None = None
    username_by_user: dict[int, str] = {}
    total_by_user: Counter[int] = Counter()
    commands_by_user: defaultdict[int, Counter[str]] = defaultdict(Counter)
    users_by_command: defaultdict[str, Counter[int]] = defaultdict(Counter)

    def is_public_action(action: "CommandTelemetryEnd") -> bool:
        return action["status"] == "ok" and not action["command"].startswith(
            manage.group.name + " "
        )

    def add_usage(action: "CommandTelemetryEnd") -> None:
        if not is_public_action(action):
            return

        user_id = action["user_id"]
        command = action["command"]
        username_by_user[user_id] = action["username"]
        total_by_user[user_id] += 1
        commands_by_user[user_id][command] += 1
        users_by_command[command][user_id] += 1

    def parse_action(item) -> "CommandTelemetryEnd | None":
        if not isinstance(item, dict):
            return None

        if item.get("phase") != "end":
            return None

        try:
            action: CommandTelemetryEnd = {
                "interaction_id": int(item["interaction_id"]),
                "command": str(item["command"]),
                "user_id": int(item["user_id"]),
                "username": str(item["username"]),
                "channel_id": int(item["channel_id"]) if item.get("channel_id") is not None else None,
                "time": int(item["time"]),
                "phase": "end",
                "status": item["status"],
                "duration_ms": float(item["duration_ms"]),
                "error": str(item["error"]) if item.get("error") is not None else None,
            }
        except (KeyError, TypeError, ValueError):
            return None

        if action["status"] not in ("ok", "unauthorized", "error"):
            return None

        return action

    def load_actions() -> None:
        if not telemetry_path.exists():
            return

        try:
            with telemetry_path.open("r", encoding="utf-8") as f:
                for line in f:
                    with contextlib.suppress(json.JSONDecodeError):
                        action = parse_action(json.loads(line))
                        if action is not None:
                            recent_actions.append(action)
                            add_usage(action)
        except OSError:
            bot.logger.warning(f"Could not read telemetry file: {telemetry_path}")

    def append_lines(lines: list[str]) -> None:
        if telemetry_path.parent != Path("."):
            telemetry_path.parent.mkdir(parents=True, exist_ok=True)

        with telemetry_path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line)
                f.write("\n")

    async def flush_pending() -> None:
        nonlocal pending_lines

        if not pending_lines:
            return

        lines = pending_lines
        pending_lines = []

        try:
            await asyncio.to_thread(append_lines, lines)
        except OSError as e:
            pending_lines = lines + pending_lines
            bot.logger.warning(f"Could not save telemetry file: {e}")

    def schedule_flush() -> None:
        nonlocal flush_task

        if flush_task is not None and not flush_task.done():
            return

        async def delayed_flush():
            nonlocal flush_task

            await asyncio.sleep(flush_interval)
            await flush_pending()
            flush_task = None

        flush_task = asyncio.create_task(delayed_flush())

    def save_action(action: "CommandTelemetryEnd") -> None:
        pending_lines.append(json.dumps(action, separators=(",", ":")))
        schedule_flush()

    def ranked_usage(commands: list[str] | None) -> list[UserUsage]:
        cache_key = () if commands is None else tuple(dict.fromkeys(commands))

        if not cache_key:
            user_totals = total_by_user
        elif len(cache_key) == 1:
            user_totals = users_by_command.get(cache_key[0], Counter())
        else:
            user_totals: Counter[int] = Counter()

            for command in cache_key:
                user_totals.update(users_by_command.get(command, Counter()))

        top_users = heapq.nlargest(10, user_totals.items(), key=lambda item: item[1])
        ranked = [
            UserUsage(
                user_id=user_id,
                name=username_by_user.get(user_id, str(user_id)),
                total=total,
                commands=commands_by_user[user_id] if not cache_key else Counter({
                    command: commands_by_user[user_id][command]
                    for command in cache_key
                    if commands_by_user[user_id][command]
                }),
            )
            for user_id, total in top_users
        ]
        return ranked

    valid_public_commands: set[str] = set()
    
    @bot.init_callback
    async def init():
        for command in bot.tree.get_commands():
            if isinstance(command, discord.app_commands.Group):
                continue
            if command.qualified_name.startswith(manage.group.name + " "):
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
            add_usage(event)
            save_action(event)
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
        recent = list(recent_actions)[-action_count:]

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
                
                if status == "ok":
                    status_icon = "✅"
                elif status == "unauthorized":
                    status_icon = "🔒"
                elif status == "error":
                    status_icon = "⚠️"
                else:
                    status_icon = status

                line = (
                    f"{timestamp} {status_icon} `/{command}` "
                    f"{duration}ms {user} in {channel}"
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
    async def usage(interaction: discord.Interaction, commands: str | None = None):
        requested_commands: list[str] | None = None

        if commands is not None and commands.strip():
            requested_commands = [
                item.strip().removeprefix("/")
                for item in commands.split(",")
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

        ranked = ranked_usage(requested_commands)

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
