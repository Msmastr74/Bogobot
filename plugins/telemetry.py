from collections import Counter, defaultdict
import asyncio
import contextlib
from dataclasses import dataclass
import heapq
import json
from pathlib import Path
from typing import Literal, TypedDict, TypeAlias
import itertools
import discord
from utils.pagination import PageSection, PaginatedView, SectionRead
from bogobot_core import BotCore
from utils import groups
from utils.nl import action
from discord import app_commands

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

CommandTelemetryEvent: TypeAlias = CommandTelemetryStart | CommandTelemetryEnd

@dataclass
class UserUsage:
    user_id: int
    name: str
    total: int
    commands: Counter[str]

@dataclass(frozen=True)
class TelemetryState:
    # Byte offsets from the start of the append-only telemetry file.
    cursor: int
    snapshot_end: int


class UsageView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        body: str,
    ):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(body or "\u200d"),
            accent_colour=discord.Color.blurple(),
        ))

async def setup(bot: BotCore):
    manage = groups.manage(bot)
    accounts = groups.accounts(bot)
    hidden_commands: list[str] = [
        manage.group.name,
        accounts.group.name,
    ]

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

    def is_public_action(action: CommandTelemetryEnd) -> bool:
        return action["status"] == "ok" and is_public_command(action["command"])
    
    def is_public_command(command: str) -> bool:
        for entry in hidden_commands:
            if command == entry or command.startswith(entry + " "):
                return False
        return True

    def add_usage(action: CommandTelemetryEnd) -> None:
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

    async def flush_pending_locked(clear_flush_task: bool) -> None:
        nonlocal pending_lines, flush_task
        
        if clear_flush_task:
            flush_task = None

        if not pending_lines:
            return

        lines = pending_lines
        pending_lines = []

        try:
            await asyncio.to_thread(append_lines, lines)
        except OSError as e:
            pending_lines = lines + pending_lines
            bot.logger.warning(f"Could not save telemetry file: {e}")

    async def flush_pending(clear_flush_task: bool) -> None:
        nonlocal flush_task
        async with telemetry_lock:
            await flush_pending_locked(clear_flush_task)

    def schedule_flush() -> None:
        nonlocal flush_task

        if flush_task is not None and not flush_task.done():
            return

        async def delayed_flush():
            nonlocal flush_task

            await asyncio.sleep(flush_interval)
            await flush_pending(True)

        flush_task = asyncio.create_task(delayed_flush())

    def save_action(action: CommandTelemetryEnd) -> None:
        pending_lines.append(json.dumps(action, separators=(",", ":")))
        schedule_flush()

    def telemetry_eof_from_file() -> int:
        if not telemetry_path.exists():
            return 0
        with telemetry_path.open("rb") as f:
            return f.seek(0, 2)

    async def fresh_telemetry_state() -> TelemetryState:
        async with telemetry_lock:
            await flush_pending_locked(False)
            eof = await asyncio.to_thread(telemetry_eof_from_file)
        return TelemetryState(cursor=eof, snapshot_end=eof)

    def parse_telemetry_line(raw_line: bytes, requested: set[str]) -> str | None:
        def action_matches(action: CommandTelemetryEnd) -> bool:
            return not requested or action["command"] in requested

        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            action = parse_action(json.loads(raw_line))
            if action is not None and action_matches(action):
                return format_telemetry_line(action)
        return None

    def read_previous_telemetry_section_from_file(
        requested: set[str],
        state: TelemetryState,
    ) -> tuple[str, TelemetryState] | None:
        if not telemetry_path.exists():
            return None

        cursor = min(state.cursor, state.snapshot_end)
        if cursor <= 0:
            return None

        leftover = b""

        with telemetry_path.open("rb") as f:
            position = cursor

            while position > 0:
                read_size = min(8192, position)
                chunk_start = position - read_size

                position = chunk_start
                f.seek(chunk_start)

                data = f.read(read_size) + leftover
                raw_lines = data.split(b"\n")

                # raw_lines[0] may be the tail of a line that started in an
                # earlier chunk. Save it and complete it on the next iteration.
                leftover = raw_lines[0]

                # Store exact file offsets for every complete line in this chunk.
                line_offsets: list[tuple[int, int, bytes]] = []

                line_start = chunk_start + len(raw_lines[0]) + 1
                for raw_line in raw_lines[1:]:
                    line_end = line_start + len(raw_line)
                    line_offsets.append((line_start, line_end, raw_line))
                    line_start = line_end + 1

                # Process newest to oldest within this chunk.
                for line_start, _line_end, raw_line in reversed(line_offsets):
                    if not raw_line:
                        continue

                    line = parse_telemetry_line(raw_line, requested)
                    if line is not None:
                        return line, TelemetryState(
                            cursor=line_start,
                            snapshot_end=state.snapshot_end,
                        )

            # Handle the first line of the file, if any.
            if leftover:
                line = parse_telemetry_line(leftover, requested)
                if line is not None:
                    return line, TelemetryState(cursor=0, snapshot_end=state.snapshot_end)
        return None

    def read_next_telemetry_section_from_file(
        requested: set[str],
        state: TelemetryState,
    ) -> tuple[str, TelemetryState] | None:
        if not telemetry_path.exists() or state.cursor >= state.snapshot_end:
            return None

        with telemetry_path.open("rb") as f:
            f.seek(state.cursor)
            while f.tell() < state.snapshot_end:
                line_start = f.tell()
                raw_line = f.readline(state.snapshot_end - line_start)
                if not raw_line:
                    return None

                line_end = f.tell()
                raw_line = raw_line.rstrip(b"\n")
                if not raw_line:
                    continue

                line = parse_telemetry_line(raw_line, requested)
                if line is not None:
                    return line, TelemetryState(
                        cursor=line_end,
                        snapshot_end=state.snapshot_end,
                    )
        return None
    
    async def read_previous_telemetry_section(
        requested: set[str],
        state: TelemetryState,
    ) -> tuple[str, TelemetryState] | None:
        return await asyncio.to_thread(read_previous_telemetry_section_from_file, requested, state)

    async def read_next_telemetry_section(
        requested: set[str],
        state: TelemetryState,
    ) -> tuple[str, TelemetryState] | None:
        return await asyncio.to_thread(read_next_telemetry_section_from_file, requested, state)
    
    def telemetry_title(
        requested_commands: list[str] | None,
        state: TelemetryState,
    ) -> str:
        title = "Recent Command Telemetry"

        if requested_commands:
            title = f"Telemetry: {', '.join('/' + command for command in requested_commands)}"

        if state.cursor < state.snapshot_end:
            title = f"{title} - Older"

        return title
    
    class TelemetryView(PaginatedView[TelemetryState]):
        def __init__(
            self,
            *,
            initial_state: TelemetryState,
            requested: set[str],
            requested_commands: list[str] | None,
        ):
            super().__init__(
                initial_state=initial_state,
                timeout=300,
            )
            self.requested = requested
            self.requested_commands = requested_commands
            self.newer = discord.ui.Button(
                label="Newer",
                style=discord.ButtonStyle.secondary,
            )
            self.refresh = discord.ui.Button(
                label="Refresh",
                style=discord.ButtonStyle.primary,
            )
            self.older = discord.ui.Button(
                label="Older",
                style=discord.ButtonStyle.secondary,
            )
            self.newer.callback = self.newer_action
            self.refresh.callback = self.refresh_action
            self.older.callback = self.older_action
            self.controls = discord.ui.ActionRow(
                self.newer,
                self.refresh,
                self.older,
            )

        def page_allowed_mentions(self) -> discord.AllowedMentions | None:
            return discord.AllowedMentions.none()

        def empty_sections(self) -> list[PageSection]:
            body = (
                "No older telemetry."
                if self.state.cursor < self.state.snapshot_end
                else "No telemetry for that query." if self.requested else "No telemetry yet."
            )
            return [
                PageSection(
                    title=telemetry_title(self.requested_commands, self.state),
                    body=body,
                    accent_colour=discord.Color.dark_teal(),
                )
            ]

        def page_header(self, page) -> str | None:
            return f"## {telemetry_title(self.requested_commands, self.state)}"

        async def next_section(
            self,
            state: TelemetryState,
        ) -> SectionRead[TelemetryState] | None:
            result = await read_previous_telemetry_section(self.requested, state)
            if result is None:
                return None

            line, next_state = result
            return SectionRead(
                section=PageSection(
                    title=telemetry_title(self.requested_commands, state),
                    body=line,
                    accent_colour=discord.Color.dark_teal(),
                ),
                state=next_state,
            )

        async def previous_section(
            self,
            state: TelemetryState,
        ) -> SectionRead[TelemetryState] | None:
            if state.cursor >= state.snapshot_end:
                return None

            result = await read_next_telemetry_section(self.requested, state)
            if result is None:
                return None

            line, previous_state = result
            return SectionRead(
                section=PageSection(
                    title=telemetry_title(self.requested_commands, state),
                    body=line,
                    accent_colour=discord.Color.dark_teal(),
                ),
                state=previous_state,
            )

        def sync_controls(self) -> None:
            self.newer.disabled = self.previous_page_state is None
            self.older.disabled = self.next_page_state is None
            self.refresh.disabled = False

        def add_controls(self) -> None:
            self.add_item(self.controls)

        async def newer_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.show_previous_page(interaction)

        async def refresh_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.set_state(interaction, await fresh_telemetry_state())

        async def older_action(
            self,
            interaction: discord.Interaction,
        ) -> None:
            await self.show_next_page(interaction)
    
    def status_icon(status: str) -> str:
        if status == "ok":
            return "✅"
        if status == "unauthorized":
            return "🔒"
        if status == "error":
            return "⚠️"
        return status

    def format_telemetry_line(item: CommandTelemetryEnd) -> str:
        timestamp = f"<t:{item['time']}:T>"
        channel_id = item["channel_id"]
        channel = f"<#{channel_id}>" if channel_id is not None else "DM"
        line = (
            f"{timestamp} {status_icon(item['status'])} `/{item['command']}` "
            f"{item['duration_ms']}ms <@{item['user_id']}> in {channel}"
        )
        if item["error"]:
            line += f" | error={item['error']}"
        return line
    
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

    def usage_title(requested_commands: list[str] | None) -> str:
        if requested_commands:
            return f"Usage: {', '.join('/' + command for command in requested_commands)}"
        return "Usage"

    def usage_body(
        ranked: list[UserUsage],
        requested_commands: list[str] | None,
    ) -> str:
        if not ranked:
            return "No usage data for that query."

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

        return "\n".join(lines)

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
    ) -> list[app_commands.Choice[str]]:
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
                app_commands.Choice(
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
            if isinstance(command, app_commands.Group):
                continue
            if is_public_command(command.qualified_name):
                valid_public_commands.add(command.qualified_name)
            all_valid_commands.add(command.qualified_name)

    load_actions()

    @bot.command_telemetry_callback
    def record_command(event: CommandTelemetryEvent):
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
    ):
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
        initial_state = await fresh_telemetry_state()
        view = TelemetryView(
            initial_state=initial_state,
            requested=requested,
            requested_commands=requested_commands,
        )
        page = await view.load()

        await bot.discord.send(
            **page.as_send_kwargs(),
            view=view,
            response=True
        )

    @telemetry.autocomplete("commands")
    async def telemetry_commands_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return autocomplete_commands(current, all_valid_commands)

    @bot.setup.command(
        name="usage",
        description="Show command usage totals",
        perm_requirement=0,
        defer=False,
    )
    @action(
        "usage",
        "Show command usage totals.",
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

        await bot.discord.defer()

        ranked = ranked_usage(requested_commands)
        view = UsageView(
            title=usage_title(requested_commands),
            body=usage_body(ranked, requested_commands),
        )

        await bot.discord.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            response=True
        )

    @usage.autocomplete("commands")
    async def usage_commands_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return autocomplete_commands(current, valid_public_commands)
