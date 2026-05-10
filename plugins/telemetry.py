from collections import Counter, defaultdict
import asyncio
import contextlib
from dataclasses import dataclass
import heapq
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict
import itertools
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
    from utils import groups

    manage = groups.manage(bot)
    telemetry_path = Path(bot.config.get("telemetry_path", "telemetry.jsonl"))
    flush_interval = max(0.1, float(bot.config.get("telemetry_flush_interval", 2)))
    active: dict[tuple[int, str], "CommandTelemetryEvent"] = {}
    pending_lines: list[str] = []
    flush_task: asyncio.Task[None] | None = None
    telemetry_lock = asyncio.Lock()
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

    async def flush_pending_locked() -> None:
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

    async def flush_pending() -> None:
        async with telemetry_lock:
            await flush_pending_locked()

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

    def read_recent_from_file(
        requested: set[str],
        action_count: int,
    ) -> list["CommandTelemetryEnd"]:
        if not telemetry_path.exists():
            return []

        recent: list[CommandTelemetryEnd] = []
        leftover = b""

        with telemetry_path.open("rb") as f:
            position = f.seek(0, 2)

            while position > 0 and len(recent) < action_count:
                read_size = min(8192, position)
                position -= read_size
                f.seek(position)

                data = f.read(read_size) + leftover
                lines = data.split(b"\n")
                leftover = lines[0]

                for raw_line in reversed(lines[1:]):
                    if not raw_line:
                        continue

                    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                        action = parse_action(json.loads(raw_line.decode("utf-8")))
                        if action is None:
                            continue
                        if requested and action["command"] not in requested:
                            continue

                        recent.append(action)

                        if len(recent) >= action_count:
                            break

            if leftover and len(recent) < action_count:
                with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                    action = parse_action(json.loads(leftover.decode("utf-8")))
                    if action is not None and (not requested or action["command"] in requested):
                        recent.append(action)

        recent.reverse()
        return recent

    async def read_recent_actions(
        requested: set[str],
        action_count: int,
    ) -> list["CommandTelemetryEnd"]:
        async with telemetry_lock:
            await flush_pending_locked()
            return await asyncio.to_thread(
                read_recent_from_file,
                requested,
                action_count,
            )

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

    def parse_commands(commands: str | None) -> list[str] | None:
        if commands is None or not commands.strip():
            return None

        return [
            " ".join(item.strip().removeprefix("/").lstrip().split())
            for item in commands.split(",")
            if item.strip()
        ]

    def invalid_commands(
        requested_commands: list[str] | None,
        valid_commands: set[str],
    ) -> list[str]:
        if requested_commands is None:
            return []

        return [
            item
            for item in requested_commands
            if item not in valid_commands
        ]

    def autocomplete_commands(
        current: str,
        valid_commands: set[str],
    ) -> list[discord.app_commands.Choice[str]]:
        parts = current.split(",")
        raw_current = parts[-1].strip()
        use_slash = raw_current.startswith("/")
        previous = [
            " ".join(part.strip().removeprefix("/").lstrip().split())
            for part in parts[:-1]
            if part.strip()
        ]
        partial = " ".join(raw_current.removeprefix("/").lstrip().split()).lower()
        already_selected = set(previous)

        choices = []
        for command in sorted(valid_commands, key=str.casefold):
            if command in already_selected:
                continue
            if partial and not command.lower().startswith(partial):
                continue

            value = ", ".join([*previous, command])
            display_commands = [
                f"/{item}" if use_slash else item
                for item in [*previous, command]
            ]
            choices.append(
                discord.app_commands.Choice(
                    name=", ".join(display_commands),
                    value=value,
                )
            )

            if len(choices) >= 25:
                break

        return choices

    all_valid_commands: set[str] = set()
    valid_public_commands: set[str] = set()
    
    @bot.init_callback
    async def init():
        for command in itertools.chain(
            bot.tree.get_commands(), bot.tree.walk_commands()
        ):
            if isinstance(command, discord.app_commands.Group):
                continue
            all_valid_commands.add(command.qualified_name)
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
            add_usage(event)
            save_action(event)
            return

        raise ValueError(f"Invalid telemetry event phase: {event['phase']}")

    @manage.command(
        name="telemetry",
        description="Show recent bot command activity",
        eph=True,
    )
    async def telemetry(
        interaction: discord.Interaction,
        commands: str | None = None,
        action_count: int = 20,
    ):
        if action_count < 1:
            await bot.discord.send("Action count must be at least 1.", response=True)
            return

        requested_commands = parse_commands(commands)
        invalid = invalid_commands(requested_commands, all_valid_commands)

        if invalid:
            valid_list = ", ".join(sorted(all_valid_commands, key=str.casefold))
            plural = "s" if len(invalid) > 1 else ""
            await bot.discord.send(
                f"Unknown command{plural}: {', '.join(invalid)}\nValid commands: {valid_list}",
                response=True, ephemeral=True
            )
            return

        requested = set(requested_commands or [])
        recent = await read_recent_actions(requested, action_count)

        if not recent:
            body = "No telemetry for that query." if requested else "No telemetry yet."
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

    @telemetry.autocomplete("commands")
    async def telemetry_commands_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[discord.app_commands.Choice[str]]:
        return autocomplete_commands(current, all_valid_commands)

    @bot.setup.command(
        name="usage",
        description="Show command usage totals",
        perm_requirement=0,
        defer=False,
    )
    async def usage(interaction: discord.Interaction, commands: str | None = None):
        requested_commands = parse_commands(commands)
        invalid = invalid_commands(requested_commands, valid_public_commands)

        if invalid:
            valid_list = ", ".join(sorted(valid_public_commands, key=str.casefold))
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

    @usage.autocomplete("commands")
    async def usage_commands_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[discord.app_commands.Choice[str]]:
        return autocomplete_commands(current, valid_public_commands)
