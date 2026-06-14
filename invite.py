import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord


DEFAULT_SCOPES = ("bot", "applications.commands")
USER_INSTALL_SCOPES = ("applications.commands",)
DEFAULT_CONFIG_PATHS = (
    Path("local_config.json"),
    Path("config.json"),
)


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    return value if isinstance(value, dict) else {}


def config_client_id(paths: tuple[Path, ...]) -> int | None:
    for path in paths:
        config = read_config(path)
        raw_client_id = (
            config.get("bot_application_id") or
            config.get("application_id") or
            config.get("client_id")
        )
        if raw_client_id is not None:
            return int(raw_client_id)
    return None


def parse_scopes(value: str) -> tuple[str, ...]:
    return tuple(scope.strip() for scope in value.split(",") if scope.strip())


def default_permissions() -> discord.Permissions:
    permissions = discord.Permissions.none()
    permissions.view_audit_log = True
    permissions.manage_roles = True
    permissions.kick_members = True
    permissions.ban_members = True
    permissions.moderate_members = True
    permissions.view_channel = True
    permissions.send_messages = True
    permissions.send_messages_in_threads = True
    permissions.embed_links = True
    permissions.attach_files = True
    permissions.read_message_history = True
    permissions.add_reactions = True
    permissions.use_external_emojis = True
    return permissions


def with_integration_type(url: str, integration_type: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["integration_type"] = str(integration_type)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a Discord bot invite link.")
    parser.add_argument(
        "--client-id",
        type=int,
        default=None,
        help="Discord application/client ID. Defaults to bot_application_id from local_config.json/config.json.",
    )
    parser.add_argument(
        "--permissions",
        type=int,
        default=default_permissions().value,
        help=f"Discord permissions integer. Defaults to {default_permissions().value}.",
    )
    parser.add_argument(
        "--scopes",
        default=",".join(DEFAULT_SCOPES),
        help="Comma-separated OAuth scopes. Defaults to bot,applications.commands.",
    )
    args = parser.parse_args()

    client_id = args.client_id or config_client_id(DEFAULT_CONFIG_PATHS)
    if client_id is None:
        raise SystemExit("No client ID found. Pass --client-id or set bot_application_id in local_config.json.")

    guild_url = discord.utils.oauth_url(
        client_id,
        permissions=discord.Permissions(args.permissions),
        scopes=parse_scopes(args.scopes),
    )
    user_url = discord.utils.oauth_url(
        client_id,
        scopes=USER_INSTALL_SCOPES,
    )

    print("Guild install:")
    print(with_integration_type(guild_url, 0))
    print()
    print("User install:")
    print(with_integration_type(user_url, 1))


if __name__ == "__main__":
    main()
