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

TELEMETRY_EMBED_LIMIT = 4000
TELEMETRY_LINE_LIMIT = 30

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

@dataclass(frozen=True)
class TelemetryCursor:
    # Exclusive byte offset. The reader scans bytes before this offset.
    # None means "start at EOF".
    offset: int | None

    # Logical index among matching telemetry entries, counted from newest.
    # Used only for display.
    index_from_end: int = 0

@dataclass
class TelemetryPage:
    lines: list[str]
    cursor: TelemetryCursor
    end_index_from_end: int
    next_cursor: TelemetryCursor | None

    @property
    def start_index(self) -> int:
        return self.cursor.index_from_end

    @property
    def end_index(self) -> int:
        return self.end_index_from_end

if TYPE_CHECKING:
    from main import BotCore


async def setup(bot: "BotCore"):
    from utils import groups

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

    def is_public_action(action: "CommandTelemetryEnd") -> bool:
        return action["status"] == "ok" and is_public_command(action["command"])
    
    def is_public_command(command: str) -> bool:
        for entry in hidden_commands:
            if command == entry or command.startswith(entry + " "):
                return False
        return True

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
        cursor: TelemetryCursor = TelemetryCursor(offset=None, index_from_end=0),
    ) -> TelemetryPage:
        if not telemetry_path.exists():
            return TelemetryPage(
                lines=[],
                cursor=cursor,
                end_index_from_end=cursor.index_from_end,
                next_cursor=None,
            )

        output_lines: list[str] = []
        current_len = 0
        current_index = cursor.index_from_end
        next_cursor: TelemetryCursor | None = None
        leftover = b""

        def action_matches(action: "CommandTelemetryEnd") -> bool:
            return not requested or action["command"] in requested

        def try_add_line(line: str) -> bool:
            """
            Returns True if the page is full and this line should be deferred
            to the next page.
            """
            nonlocal current_len

            line = line[:TELEMETRY_EMBED_LIMIT]
            line_len = len(line) + (1 if output_lines else 0)

            if len(output_lines) >= TELEMETRY_LINE_LIMIT:
                return True

            if output_lines and current_len + line_len > TELEMETRY_EMBED_LIMIT:
                return True

            output_lines.append(line)
            current_len += line_len
            return False

        with telemetry_path.open("rb") as f:
            eof = f.seek(0, 2)
            position = eof if cursor.offset is None else min(cursor.offset, eof)

            while position > 0 and next_cursor is None:
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
                for _line_start, line_end, raw_line in reversed(line_offsets):
                    if not raw_line:
                        continue

                    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                        action = parse_action(json.loads(raw_line))
                        if action is None or not action_matches(action):
                            continue

                        line = format_telemetry_line(action)

                        if try_add_line(line):
                            # We did not consume this action. Resume before/at this
                            # line next time so it appears on the next older page.
                            next_cursor = TelemetryCursor(
                                offset=line_end,
                                index_from_end=current_index,
                            )
                            break

                        current_index += 1

            # Handle the first line of the file, if any.
            if leftover and next_cursor is None:
                with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                    action = parse_action(json.loads(leftover))
                    if action is not None and action_matches(action):
                        line = format_telemetry_line(action)

                        if try_add_line(line):
                            next_cursor = TelemetryCursor(
                                offset=len(leftover),
                                index_from_end=current_index,
                            )
                        else:
                            current_index += 1

        return TelemetryPage(
            # Keep newest-first. If you prefer chronological order per page,
            # change this to list(reversed(output_lines)).
            lines=output_lines,
            cursor=cursor,
            end_index_from_end=current_index,
            next_cursor=next_cursor,
        )
    
    async def read_telemetry_page(
        requested: set[str],
        cursor: TelemetryCursor,
    ) -> TelemetryPage:
        async with telemetry_lock:
            await flush_pending_locked()
            return await asyncio.to_thread(
                read_recent_from_file,
                requested,
                cursor,
            )
    
    def telemetry_title(
        requested_commands: list[str] | None,
        page: TelemetryPage,
    ) -> str:
        title = "Recent Command Telemetry"

        if requested_commands:
            title = f"Telemetry: {', '.join('/' + command for command in requested_commands)}"

        if page.start_index > 0:
            title = f"{title} - Older {page.start_index}+"

        return title

    def telemetry_embed(
        page: TelemetryPage,
        requested_commands: list[str] | None,
        requested: set[str],
    ) -> discord.Embed:
        if page.lines:
            body = "\n".join(page.lines)
        elif page.start_index > 0:
            body = "No older telemetry."
        else:
            body = "No telemetry for that query." if requested else "No telemetry yet."

        embed = discord.Embed(
            title=telemetry_title(requested_commands, page),
            description=body,
            color=discord.Color.dark_teal(),
        )

        if page.lines:
            embed.set_footer(
                text=f"Showing entries {page.start_index + 1}-{page.end_index} from newest"
            )

        return embed
    
    class TelemetryView(discord.ui.View):
        def __init__(
            self,
            *,
            requested: set[str],
            requested_commands: list[str] | None,
            owner_id: int,
        ):
            super().__init__(timeout=300)
            self.requested = requested
            self.requested_commands = requested_commands
            self.owner_id = owner_id

            self.cursor = TelemetryCursor(offset=None, index_from_end=0)
            self.previous_cursors: list[TelemetryCursor] = []

            self.current_page = TelemetryPage(
                lines=[],
                cursor=self.cursor,
                end_index_from_end=0,
                next_cursor=None,
            )

        async def load(self) -> discord.Embed:
            self.current_page = await read_telemetry_page(
                self.requested,
                self.cursor,
            )
            self._sync_buttons()
            return telemetry_embed(
                self.current_page,
                self.requested_commands,
                self.requested,
            )

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.user.id == self.owner_id:
                return True

            await interaction.response.send_message(
                "This telemetry view is not yours.",
                ephemeral=True,
            )
            return False

        def _sync_buttons(self) -> None:
            self.newer.disabled = not self.previous_cursors
            self.older.disabled = self.current_page.next_cursor is None
            self.refresh.disabled = self.cursor.offset is not None

        @discord.ui.button(label="Newer", style=discord.ButtonStyle.secondary)
        async def newer(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ):
            if self.previous_cursors:
                self.cursor = self.previous_cursors.pop()

            await interaction.response.edit_message(
                embed=await self.load(),
                view=self,
            )

        @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
        async def refresh(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ):
            self.cursor = TelemetryCursor(offset=None, index_from_end=0)
            self.previous_cursors.clear()

            await interaction.response.edit_message(
                embed=await self.load(),
                view=self,
            )

        @discord.ui.button(label="Older", style=discord.ButtonStyle.secondary)
        async def older(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ):
            if self.current_page.next_cursor is not None:
                self.previous_cursors.append(self.cursor)
                self.cursor = self.current_page.next_cursor

            await interaction.response.edit_message(
                embed=await self.load(),
                view=self,
            )
    
    def status_icon(status: str) -> str:
        if status == "ok":
            return "✅"
        if status == "unauthorized":
            return "🔒"
        if status == "error":
            return "⚠️"
        return status

    def format_telemetry_line(item: "CommandTelemetryEnd") -> str:
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
            if is_public_command(command.qualified_name):
                valid_public_commands.add(command.qualified_name)
            all_valid_commands.add(command.qualified_name)

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
        view = TelemetryView(
            requested=requested,
            requested_commands=requested_commands,
            owner_id=interaction.user.id,
        )
        embed = await view.load()

        await bot.discord.send(
            embed=embed,
            view=view,
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
            description=body[:4000],
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
