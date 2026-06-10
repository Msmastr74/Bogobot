#!/usr/bin/env python3
"""Temporary raw-JSON migration helper for the account-row storage format.

Migrates:
- old account maps into {"version": 2, "accounts": [...]} rows
- old per-account local records into scoped user rows
- bogotree.json server state into guild account rows
- cbogo.json server state into guild account rows
- optionally config verification/raid server config into guild account rows

This script intentionally does not import Bogobot runtime modules.
"""

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from typing import Any


GLOBAL_SCOPE = "global"
PERMISSIONS_KEY = "perms"
LOCAL_ACCOUNTS_KEY = "local"
DEFAULT_CAPABILITIES = {
    "commands": 0,
    "user": 0,
}

BOGOTREE_STATE_KEY = "bogotree_state"
CBOGO_STATE_KEY = "cbogo_state"
SECURITY_ROLES_KEY = "security_roles"
RAID_PROTECTION_KEY = "raid_protection"


AccountKey = tuple[str, str, str]
AccountRecord = dict[str, Any]


@dataclass
class MigrationStats:
    global_users: int = 0
    local_users: int = 0
    role_accounts: int = 0
    guild_accounts: int = 0
    bogotree_states: int = 0
    cbogo_states: int = 0
    security_roles: int = 0
    raid_configs: int = 0


def load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return None


def write_json(path: str, value: Any, *, backup: bool) -> None:
    if backup and os.path.exists(path):
        backup_path = next_backup_path(path)
        shutil.copy2(path, backup_path)
        print(f"Backed up {path} -> {backup_path}")

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=4)
        file.write("\n")
    os.replace(tmp_path, path)


def write_accounts_json(path: str, value: dict[str, Any], *, backup: bool) -> None:
    if backup and os.path.exists(path):
        backup_path = next_backup_path(path)
        shutil.copy2(path, backup_path)
        print(f"Backed up {path} -> {backup_path}")

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    rows = value.get("accounts")
    if not isinstance(rows, list):
        rows = []

    lines = [
        "{",
        '    "version": 2,',
        '    "accounts": [',
    ]
    for index, row in enumerate(rows):
        suffix = "," if index < len(rows) - 1 else ""
        lines.append(
            "        " +
            json.dumps(row, separators=(",", ":")) +
            suffix
        )
    lines.extend([
        "    ]",
        "}",
        "",
    ])

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines))
    os.replace(tmp_path, path)


def next_backup_path(path: str) -> str:
    base = f"{path}.bak"
    if not os.path.exists(base):
        return base
    index = 1
    while True:
        candidate = f"{base}.{index}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def normalize_scope(value: object) -> str:
    scope = str(value).strip() if value is not None else GLOBAL_SCOPE
    return scope or GLOBAL_SCOPE


def normalize_account_type(value: object) -> str | None:
    return value if value in ("user", "role", "guild") else None


def normalize_permissions(value: object, *, default_user: bool = False) -> dict[str, Any] | None:
    capabilities: dict[str, int] = {}
    if isinstance(value, dict):
        raw_capabilities = value.get("capabilities")
        if isinstance(raw_capabilities, dict):
            for raw_capability, raw_depth in raw_capabilities.items():
                capability = str(raw_capability).strip()
                if not capability:
                    continue
                try:
                    capabilities[capability] = int(raw_depth)
                except (TypeError, ValueError):
                    continue

    if default_user:
        for capability, depth in DEFAULT_CAPABILITIES.items():
            capabilities.setdefault(capability, depth)

    if not capabilities:
        return None
    return {"capabilities": capabilities}


def normalize_record(value: object, *, default_user: bool = False) -> AccountRecord:
    if not isinstance(value, dict):
        value = {}

    record = dict(value)
    record.pop("perm_level", None)
    record.pop(LOCAL_ACCOUNTS_KEY, None)

    permissions = normalize_permissions(record.get(PERMISSIONS_KEY), default_user=default_user)
    if permissions is None:
        record.pop(PERMISSIONS_KEY, None)
    else:
        record[PERMISSIONS_KEY] = permissions
    return record


def merge_capabilities(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    for capability, raw_depth in new.items():
        try:
            depth = int(raw_depth)
        except (TypeError, ValueError):
            continue
        try:
            old_depth = int(merged.get(capability, depth))
        except (TypeError, ValueError):
            old_depth = depth
        merged[capability] = max(old_depth, depth)
    return merged


def merge_record(existing: AccountRecord, incoming: AccountRecord) -> AccountRecord:
    merged = dict(existing)
    existing_perms = existing.get(PERMISSIONS_KEY)
    incoming_perms = incoming.get(PERMISSIONS_KEY)

    for key, value in incoming.items():
        if key != PERMISSIONS_KEY:
            merged[key] = value

    if isinstance(existing_perms, dict) or isinstance(incoming_perms, dict):
        existing_caps = (
            existing_perms.get("capabilities", {})
            if isinstance(existing_perms, dict) else
            {}
        )
        incoming_caps = (
            incoming_perms.get("capabilities", {})
            if isinstance(incoming_perms, dict) else
            {}
        )
        capabilities = merge_capabilities(
            existing_caps if isinstance(existing_caps, dict) else {},
            incoming_caps if isinstance(incoming_caps, dict) else {},
        )
        if capabilities:
            merged[PERMISSIONS_KEY] = {"capabilities": capabilities}
    return merged


def put_account(
    accounts: dict[AccountKey, AccountRecord],
    scope: str,
    account_type: str,
    account_id: int | str,
    record: AccountRecord,
) -> None:
    key = (scope, account_type, str(account_id))
    accounts[key] = merge_record(accounts.get(key, {}), record)


def normalize_account_rows(raw_accounts: Any, stats: MigrationStats) -> dict[AccountKey, AccountRecord]:
    accounts: dict[AccountKey, AccountRecord] = {}
    if isinstance(raw_accounts, dict) and isinstance(raw_accounts.get("accounts"), list):
        for row in raw_accounts["accounts"]:
            if not isinstance(row, dict):
                continue
            account_type = normalize_account_type(row.get("type"))
            if account_type is None:
                continue
            account_id = str(row.get("id", "")).strip()
            if not account_id:
                continue
            scope = normalize_scope(row.get("scope"))
            data = normalize_record(
                row.get("data"),
                default_user=account_type == "user" and scope == GLOBAL_SCOPE,
            )
            put_account(accounts, scope, account_type, account_id, data)
        return accounts

    if not isinstance(raw_accounts, dict):
        return accounts

    for raw_uid, raw_account in raw_accounts.items():
        uid = str(raw_uid).strip()
        if not uid or not isinstance(raw_account, dict):
            continue

        global_record = normalize_record(raw_account, default_user=True)
        put_account(accounts, GLOBAL_SCOPE, "user", uid, global_record)

        raw_local = raw_account.get(LOCAL_ACCOUNTS_KEY)
        if isinstance(raw_local, dict):
            for raw_guild_id, raw_local_record in raw_local.items():
                guild_id = str(raw_guild_id).strip()
                if not guild_id or not isinstance(raw_local_record, dict):
                    continue
                local_record = normalize_record(raw_local_record)
                put_account(accounts, guild_id, "user", uid, local_record)

    return accounts


def migrate_game_state(
    accounts: dict[AccountKey, AccountRecord],
    *,
    path: str,
    destination_key: str,
    default_guild: str | None,
) -> int:
    data = load_json(path)
    if not isinstance(data, dict):
        return 0

    raw_servers = data.get("servers")
    servers = raw_servers if isinstance(raw_servers, dict) else {}
    if not servers and "state" in data and default_guild is not None:
        servers = {default_guild: data}

    migrated = 0
    for raw_guild_id, raw_server in servers.items():
        if not isinstance(raw_server, dict):
            continue
        state = raw_server.get("state")
        if state is None:
            continue
        put_account(
            accounts,
            str(raw_guild_id),
            "guild",
            str(raw_guild_id),
            {destination_key: state},
        )
        migrated += 1
    return migrated


def migrate_config_server_maps(
    accounts: dict[AccountKey, AccountRecord],
    *,
    path: str,
) -> tuple[int, int]:
    data = load_json(path)
    if not isinstance(data, dict):
        return 0, 0

    security_count = migrate_server_map(
        accounts,
        data.get("verification"),
        destination_key=SECURITY_ROLES_KEY,
    )
    raid_count = migrate_server_map(
        accounts,
        data.get("raid_protection"),
        destination_key=RAID_PROTECTION_KEY,
    )
    return security_count, raid_count


def migrate_server_map(
    accounts: dict[AccountKey, AccountRecord],
    value: object,
    *,
    destination_key: str,
) -> int:
    if not isinstance(value, dict):
        return 0
    raw_servers = value.get("servers")
    servers = raw_servers if isinstance(raw_servers, dict) else {}

    migrated = 0
    for raw_guild_id, raw_server in servers.items():
        if not isinstance(raw_server, dict):
            continue
        guild_id = str(raw_guild_id).strip()
        if not guild_id:
            continue
        put_account(
            accounts,
            guild_id,
            "guild",
            guild_id,
            {destination_key: dict(raw_server)},
        )
        migrated += 1
    return migrated


def json_accounts(accounts: dict[AccountKey, AccountRecord], stats: MigrationStats) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for scope, account_type, account_id in sorted(accounts):
        data = strip_empty_values(accounts[(scope, account_type, account_id)])
        if not data:
            continue
        if account_type == "user" and scope == GLOBAL_SCOPE:
            stats.global_users += 1
        elif account_type == "user":
            stats.local_users += 1
        elif account_type == "role":
            stats.role_accounts += 1
        elif account_type == "guild":
            stats.guild_accounts += 1
        rows.append({
            "scope": scope,
            "type": account_type,
            "id": account_id,
            "data": data,
        })
    return {"version": 2, "accounts": rows}


def strip_empty_values(record: AccountRecord) -> AccountRecord:
    stripped: AccountRecord = {}
    for key, value in record.items():
        if key == PERMISSIONS_KEY and isinstance(value, dict):
            capabilities = value.get("capabilities")
            if isinstance(capabilities, dict) and capabilities:
                stripped[key] = {"capabilities": capabilities}
        elif value not in ({}, [], None):
            stripped[key] = value
    return stripped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", default="accounts.json")
    parser.add_argument("--bogotree", default="bogotree.json")
    parser.add_argument("--cbogo", default="cbogo.json")
    parser.add_argument(
        "--default-guild",
        default=None,
        help="Guild ID used for original root-level bogotree/cbogo state files without a servers map.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional config/local_config JSON to migrate verification and raid_protection server maps.",
    )
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print migration stats without writing the accounts file.",
    )
    args = parser.parse_args()

    stats = MigrationStats()
    accounts = normalize_account_rows(load_json(args.accounts), stats)

    stats.bogotree_states = migrate_game_state(
        accounts,
        path=args.bogotree,
        destination_key=BOGOTREE_STATE_KEY,
        default_guild=args.default_guild,
    )
    stats.cbogo_states = migrate_game_state(
        accounts,
        path=args.cbogo,
        destination_key=CBOGO_STATE_KEY,
        default_guild=args.default_guild,
    )
    if args.config is not None:
        stats.security_roles, stats.raid_configs = migrate_config_server_maps(
            accounts,
            path=args.config,
        )

    migrated = json_accounts(accounts, stats)
    print(json.dumps(stats.__dict__, indent=4))
    print(f"Prepared {len(migrated['accounts'])} account rows.")
    if args.dry_run:
        print("Dry run: did not write accounts file.")
        return

    write_accounts_json(args.accounts, migrated, backup=not args.no_backup)
    print(f"Wrote migrated accounts to {args.accounts}")


if __name__ == "__main__":
    main()
