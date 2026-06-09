import asyncio
import json
import os
from typing import Any, Sequence

from pydantic import Field

from utils.schemas import Schema

AccountRecord = dict[str, Any]
PERMISSIONS_KEY = "perms"
LOCAL_ACCOUNTS_KEY = "local"
DEFAULT_COMMAND_CAPABILITY = "commands.*"
DEFAULT_USER_CAPABILITY = "user.*"


def default_capabilities() -> dict[str, int]:
    return {
        DEFAULT_COMMAND_CAPABILITY: 0,
        DEFAULT_USER_CAPABILITY: 0,
    }


class CapabilityRegistry:
    def __init__(self) -> None:
        self.capabilities: set[str] = set()

    def register(self, *capabilities: str) -> None:
        self.capabilities.update(capabilities)

    def __contains__(self, capability: str) -> bool:
        return any(
            AccountPermissions._matches(registered_capability, capability)
            for registered_capability in self.capabilities
        )

    def __iter__(self):
        return iter(sorted(self.capabilities))


class AccountPermissions(Schema):
    capabilities: dict[str, int] = Field(default_factory=dict)

    def depth(self, capability: str) -> int:
        matches = [
            depth
            for scope, depth in self.capabilities.items()
            if self._matches(scope, capability)
        ]
        return max(matches, default=-1)

    def can_use(self, capability: str) -> bool:
        return self.depth(capability) >= 0

    def can_grant(self, capability: str, *, depth: int = 0) -> bool:
        return self.depth(capability) > depth

    def can_revoke(self, capability: str, *, depth: int = 0) -> bool:
        return self.depth(capability) > depth

    def grant(self, capability: str, *, depth: int = 0) -> None:
        self.capabilities[capability] = int(depth)

    def revoke(self, capability: str) -> None:
        self.capabilities.pop(capability, None)

    @staticmethod
    def _matches(scope: str, capability: str) -> bool:
        if scope == "*":
            return True
        return AccountPermissions._match_parts(
            AccountPermissions._capability_parts(scope),
            AccountPermissions._capability_parts(capability),
        )

    @staticmethod
    def _capability_parts(capability: str) -> tuple[str, ...]:
        if capability == "*":
            return ("[all]",)
        if capability.endswith(".*"):
            return (*capability[:-2].split("."), "[all]")
        return tuple(capability.split("."))

    @staticmethod
    def _match_parts(scope: tuple[str, ...], capability: tuple[str, ...]) -> bool:
        if not scope or not capability:
            return not scope and not capability

        scope_head, *scope_tail = scope
        capability_head, *capability_tail = capability

        if scope_head == "[all]" or capability_head == "[all]":
            if scope_head == "[all]" and capability_head == "[all]":
                return True
            if scope_head == "[all]":
                return (
                    AccountPermissions._match_parts(tuple(scope_tail), capability) or
                    AccountPermissions._match_parts(scope, tuple(capability_tail)) or
                    AccountPermissions._match_parts(tuple(scope_tail), tuple(capability_tail))
                )
            return (
                AccountPermissions._match_parts(scope, tuple(capability_tail)) or
                AccountPermissions._match_parts(tuple(scope_tail), capability) or
                AccountPermissions._match_parts(tuple(scope_tail), tuple(capability_tail))
            )

        if scope_head == "[any]" or capability_head == "[any]":
            if len(scope) == 1 or len(capability) == 1:
                return True
            return AccountPermissions._match_parts(tuple(scope_tail), tuple(capability_tail))

        if scope_head == capability_head:
            return AccountPermissions._match_parts(tuple(scope_tail), tuple(capability_tail))

        return False


class AccountInheritance:
    def capabilities(
        self,
        global_value: dict[str, int],
        local_value: dict[str, int],
    ) -> dict[str, int]:
        return {**global_value, **local_value}


class Account:
    def __init__(
        self,
        manager: "AccountManager",
        uid: int | str,
        *,
        guild_id: int | None = None,
    ) -> None:
        self.manager = manager
        self.uid = str(uid)
        self.guild_id = guild_id
        self._record: AccountRecord | None = None

    @property
    def lock(self) -> asyncio.Lock:
        return self.manager.lock

    @property
    def permissions(self) -> AccountPermissions:
        if self._record is not None:
            value = self.record.get(PERMISSIONS_KEY)
            perms = self.manager._normalize_permissions(value)
            self.record[PERMISSIONS_KEY] = perms
            return perms
        return self.manager._permissions(self.uid)

    def local(self, guild_id: int | None) -> "Account":
        if guild_id is None:
            return self.copy()
        return self.manager._local_account(self.uid, guild_id)

    def copy(self) -> "Account":
        return Account.from_record(self.manager, self.uid, self.manager._copy_record(self.uid))

    @classmethod
    def from_record(
        cls,
        manager: "AccountManager",
        uid: int | str,
        record: AccountRecord,
        *,
        guild_id: int | None = None,
    ) -> "Account":
        account = cls(manager, uid, guild_id=guild_id)
        account._record = record
        return account

    @property
    def record(self) -> AccountRecord:
        if self._record is not None:
            return self._record
        return self.manager._record_or_fake(self.uid)

    def __getitem__(self, key: str) -> Any:
        return self.record[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.record.get(key, default)

    async def write(self, key: str, value: Any) -> None:
        async with self.manager.lock:
            record = self.manager._writable_record_locked(self.uid, self.guild_id)
            if key == PERMISSIONS_KEY:
                record[PERMISSIONS_KEY] = self.manager._normalize_permissions(value)
            else:
                record[key] = value
            self.manager._save_sync()


class AccountManager:
    def __init__(
        self,
        *,
        path: str,
    ) -> None:
        self.path = path
        self.accounts: dict[str, AccountRecord] = {}
        self.capabilities = CapabilityRegistry()
        self.inheritance = AccountInheritance()
        self._lock = asyncio.Lock()
        self._ensure_file()
        self._load_sync()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def __getitem__(self, uid: int | str) -> Account:
        return Account(self, uid)

    def _ensure_file(self) -> None:
        if os.path.exists(self.path):
            return

        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({}, f)

    def _load_sync(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw_accounts = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw_accounts = {}

        self.accounts = self._normalize_accounts(raw_accounts)

    def _normalize_accounts(self, raw_accounts: object) -> dict[str, AccountRecord]:
        if not isinstance(raw_accounts, dict):
            return {}

        accounts: dict[str, AccountRecord] = {}
        for raw_uid, raw_account in raw_accounts.items():
            uid = str(raw_uid)
            if not isinstance(raw_account, dict):
                continue

            account: AccountRecord = dict(raw_account)
            account.pop("perm_level", None)
            permissions = self._normalize_permissions(raw_account.get(PERMISSIONS_KEY))
            if permissions.capabilities:
                permissions.capabilities.setdefault(DEFAULT_COMMAND_CAPABILITY, 0)
                permissions.capabilities.setdefault(DEFAULT_USER_CAPABILITY, 0)
            account[PERMISSIONS_KEY] = permissions
            account[LOCAL_ACCOUNTS_KEY] = self._normalize_local_accounts(raw_account.get(LOCAL_ACCOUNTS_KEY))
            accounts[uid] = account

        return accounts

    def _normalize_local_accounts(self, value: object) -> dict[str, AccountRecord]:
        if not isinstance(value, dict):
            return {}

        local_accounts: dict[str, AccountRecord] = {}
        for raw_guild_id, raw_account in value.items():
            if not isinstance(raw_account, dict):
                continue
            local_account = dict(raw_account)
            if PERMISSIONS_KEY in raw_account:
                local_account[PERMISSIONS_KEY] = self._normalize_permissions(raw_account.get(PERMISSIONS_KEY))
            local_accounts[str(raw_guild_id)] = local_account
        return local_accounts

    def _normalize_permissions(self, value: object) -> AccountPermissions:
        if isinstance(value, AccountPermissions):
            return value
        if isinstance(value, dict):
            return AccountPermissions.model_validate(value)
        return AccountPermissions()

    async def save(self) -> None:
        async with self._lock:
            self._save_sync()

    def _save_sync(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._json_accounts(), f, indent=4)
        os.replace(tmp_path, self.path)

    def _json_accounts(self) -> dict[str, AccountRecord]:
        accounts: dict[str, AccountRecord] = {}
        for uid, account in self.accounts.items():
            json_account = dict(account)
            perms = self._normalize_permissions(json_account.get(PERMISSIONS_KEY))
            json_account[PERMISSIONS_KEY] = perms.model_dump(mode="json")
            raw_local = json_account.get(LOCAL_ACCOUNTS_KEY)
            if isinstance(raw_local, dict):
                local_accounts: dict[str, AccountRecord] = {}
                for guild_id, local_account in raw_local.items():
                    if not isinstance(local_account, dict):
                        continue
                    json_local_account = dict(local_account)
                    local_perms = self._normalize_permissions(json_local_account.get(PERMISSIONS_KEY))
                    json_local_account[PERMISSIONS_KEY] = local_perms.model_dump(mode="json")
                    local_accounts[str(guild_id)] = json_local_account
                json_account[LOCAL_ACCOUNTS_KEY] = local_accounts
            accounts[uid] = json_account
        return accounts

    async def ensure_account(self, uid: int | str, *, save: bool = False) -> bool:
        async with self._lock:
            created = self._ensure_account_locked(str(uid))
            if created and save:
                self._save_sync()
            return created

    async def ensure_accounts(self, uids: Sequence[int | str]) -> int:
        async with self._lock:
            added = 0
            for uid in uids:
                if self._ensure_account_locked(str(uid)):
                    added += 1
            return added

    async def has_account(self, uid: int | str) -> bool:
        async with self._lock:
            return str(uid) in self.accounts

    async def items(self) -> list[tuple[str, AccountRecord]]:
        async with self._lock:
            return [
                (uid, dict(account))
                for uid, account in self.accounts.items()
            ]

    async def query(self, key: str) -> list[tuple[str, Any]]:
        async with self._lock:
            return [
                (uid, account[key])
                for uid, account in self.accounts.items()
                if key in account
            ]

    async def query_local(self, guild_id: int | str, key: str) -> list[tuple[str, Any]]:
        guild_key = str(guild_id)
        async with self._lock:
            results: list[tuple[str, Any]] = []
            for uid, account in self.accounts.items():
                raw_local = account.get(LOCAL_ACCOUNTS_KEY)
                if not isinstance(raw_local, dict):
                    continue
                raw_local_account = raw_local.get(guild_key)
                if not isinstance(raw_local_account, dict) or key not in raw_local_account:
                    continue
                results.append((uid, raw_local_account[key]))
            return results

    def _ensure_account_locked(
        self,
        uid: str,
    ) -> bool:
        if uid in self.accounts:
            return False

        self.accounts[uid] = {
            PERMISSIONS_KEY: AccountPermissions(capabilities=default_capabilities()),
            LOCAL_ACCOUNTS_KEY: {},
        }
        return True

    def _account_locked(self, uid: str) -> AccountRecord:
        self._ensure_account_locked(uid)
        return self.accounts[uid]

    def _writable_record_locked(
        self,
        uid: str,
        guild_id: int | None,
    ) -> AccountRecord:
        account = self._account_locked(uid)
        if guild_id is None:
            return account

        raw_local = account.get(LOCAL_ACCOUNTS_KEY)
        local_accounts = raw_local if isinstance(raw_local, dict) else {}
        raw_local_account = local_accounts.get(str(guild_id))
        local_account = raw_local_account if isinstance(raw_local_account, dict) else {}
        local_accounts[str(guild_id)] = local_account
        account[LOCAL_ACCOUNTS_KEY] = local_accounts
        return local_account

    def _record_or_fake(self, uid: str) -> AccountRecord:
        return self.accounts.get(uid, {
            PERMISSIONS_KEY: AccountPermissions(capabilities=default_capabilities()),
            LOCAL_ACCOUNTS_KEY: {},
        })

    def _permissions(self, uid: str) -> AccountPermissions:
        record = self._record_or_fake(uid)
        perms = self._normalize_permissions(record.get(PERMISSIONS_KEY))
        if uid in self.accounts:
            record[PERMISSIONS_KEY] = perms
        return perms

    def _copy_record(self, uid: str) -> AccountRecord:
        record = self._record_or_fake(uid)
        copied = dict(record)
        copied[PERMISSIONS_KEY] = self._normalize_permissions(copied.get(PERMISSIONS_KEY)).model_copy(deep=True)
        raw_local = copied.get(LOCAL_ACCOUNTS_KEY)
        if isinstance(raw_local, dict):
            copied[LOCAL_ACCOUNTS_KEY] = {
                str(guild_id): dict(local_account)
                for guild_id, local_account in raw_local.items()
                if isinstance(local_account, dict)
            }
        return copied

    def _local_account(self, uid: str, guild_id: int) -> Account:
        global_record = self._copy_record(uid)
        raw_local_accounts = self._record_or_fake(uid).get(LOCAL_ACCOUNTS_KEY)
        local_accounts = raw_local_accounts if isinstance(raw_local_accounts, dict) else {}
        raw_local_record = local_accounts.get(str(guild_id))
        local_record = raw_local_record if isinstance(raw_local_record, dict) else {}

        merged = dict(global_record)
        merged.update(local_record)
        merged[PERMISSIONS_KEY] = self._inherit_permissions(
            self._normalize_permissions(global_record.get(PERMISSIONS_KEY)),
            self._normalize_permissions(local_record.get(PERMISSIONS_KEY)),
        )
        return Account.from_record(self, uid, merged, guild_id=guild_id)

    def _inherit_permissions(
        self,
        global_permissions: AccountPermissions,
        local_permissions: AccountPermissions,
    ) -> AccountPermissions:
        values: dict[str, Any] = {}
        for attr in AccountPermissions.model_fields:
            global_value = getattr(global_permissions, attr)
            local_value = getattr(local_permissions, attr)
            inherit = getattr(self.inheritance, attr, None)
            if inherit is not None:
                values[attr] = inherit(global_value, local_value)
            elif attr in local_permissions.model_fields_set:
                values[attr] = local_value
            else:
                values[attr] = global_value
        return AccountPermissions.model_validate(values)
