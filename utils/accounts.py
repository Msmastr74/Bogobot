import asyncio
import json
import os
from collections.abc import Callable
from typing import Any, ClassVar, Literal, Sequence

from pydantic import Field

from utils.schemas import Schema

AccountRecord = dict[str, Any]
AccountType = Literal["user", "role", "guild"]
AccountScope = str
AccountKey = tuple[AccountScope, AccountType, str]
PERMISSIONS_KEY = "perms"
LOCAL_ACCOUNTS_KEY = "local"
GLOBAL_SCOPE = "global"
GLOBAL_GUILD_ACCOUNT_ID = 0
DEFAULT_COMMAND_CAPABILITY = "commands"
DEFAULT_USER_CAPABILITY = "user"
BANNED_CAPABILITY = "banned"
CAPABILITY_OPERATIONS = {"use", "grant"}
CapabilityOperation = Literal["use", "grant"]


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
        if AccountPermissions.has_preset_segment(capability):
            return True
        return any(self._matches(registered_capability, capability) for registered_capability in self.capabilities)

    @staticmethod
    def _matches(registered_capability: str, capability: str) -> bool:
        registered_base, _ = AccountPermissions._split_operation(registered_capability)
        capability_base, _ = AccountPermissions._split_operation(capability)
        return (
            AccountPermissions._matches_base(registered_base, capability_base) or
            AccountPermissions._matches_base(capability_base, registered_base)
        )

    def __iter__(self):
        return iter(sorted(self.capabilities))


class AccountPermissions(Schema):
    preset_resolver: ClassVar[Callable[[str], Sequence[str]] | None] = None

    capabilities: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def configure_presets(cls, resolver: Callable[[str], Sequence[str]] | None) -> None:
        cls.preset_resolver = resolver

    def depth(self, capability: str) -> int:
        capability_base, operation = self._split_operation(capability)
        return self.effective_depth(capability_base, operation=operation or "use")

    def effective_depth(self, capability: str, *, operation: CapabilityOperation = "use") -> int:
        if capability != BANNED_CAPABILITY and self.is_banned():
            return -1
        capability_base, explicit_operation = self._split_operation(capability)
        operation = explicit_operation or operation
        matches = [
            depth
            for scope, depth in self.capabilities.items()
            if self._matches(scope, capability_base, operation=operation)
        ]
        return max(matches, default=-1)

    def is_banned(self) -> bool:
        return any(self._is_banned_scope(scope) for scope in self.capabilities)

    def max_depth(self) -> int:
        return max(self.capabilities.values(), default=-1)

    def delegation_depth(self, capability: str) -> int:
        capability_base, _ = self._split_operation(capability)
        return max(
            self.effective_depth(capability_base, operation="use"),
            self.effective_depth(capability_base, operation="grant"),
        )

    def exact_delegation_depth(self, capability: str) -> int:
        capability_base, _ = self._split_operation(capability)
        return max(
            self.capabilities.get(capability_base, -1),
            self.capabilities.get(f"{capability_base}.use", -1),
            self.capabilities.get(f"{capability_base}.grant", -1),
        )

    def required_modification_depth(self, capability: str) -> int:
        capability_base, _ = self._split_operation(capability)
        depths = [
            self.exact_delegation_depth(expanded_base)
            for expanded_base in self._expand_capability_base(capability_base)
        ]
        return max(depths, default=-1)

    def can_use(self, capability: str) -> bool:
        capability_base, _ = self._split_operation(capability)
        return self.effective_depth(capability_base, operation="use") >= 0

    def can_grant(self, capability: str, *, depth: int = 0) -> bool:
        capability_base, _ = self._split_operation(capability)
        return self.effective_depth(capability_base, operation="grant") > depth

    def can_revoke(self, capability: str, *, depth: int = 0) -> bool:
        capability_base, _ = self._split_operation(capability)
        return self.effective_depth(capability_base, operation="grant") > depth

    def grant(self, capability: str, *, depth: int = 0) -> None:
        if self.is_reserved_capability(capability):
            raise ValueError(f"{capability} is reserved")
        self.capabilities[self._canonical_permission(capability)] = int(depth)

    def revoke(self, capability: str) -> None:
        if self.is_reserved_capability(capability):
            raise ValueError(f"{capability} is reserved")
        self.capabilities.pop(self._canonical_permission(capability), None)

    def ban(self, *, depth: int = 0) -> None:
        self.capabilities[BANNED_CAPABILITY] = int(depth)

    def unban(self) -> None:
        self.capabilities.pop(BANNED_CAPABILITY, None)

    def reserved_capabilities(self) -> dict[str, int]:
        return {
            capability: depth
            for capability, depth in self.capabilities.items()
            if self.is_reserved_capability(capability)
        }

    @classmethod
    def is_reserved_capability(cls, capability: str) -> bool:
        expanded_capabilities = cls.expand_capability(capability)
        if not expanded_capabilities:
            if cls.has_preset_segment(capability):
                return False
            expanded_capabilities = (capability,)
        return any(
            cls._split_operation(expanded_capability)[0] == BANNED_CAPABILITY
            for expanded_capability in expanded_capabilities
        )

    @classmethod
    def _is_banned_scope(cls, scope: str) -> bool:
        scope_base, scope_operation = cls._split_operation(scope)
        if scope_operation == "grant":
            return False
        return any(
            expanded_scope == BANNED_CAPABILITY
            for expanded_scope in cls._expand_capability_base(scope_base)
        )

    @staticmethod
    def _canonical_permission(capability: str) -> str:
        capability_base, operation = AccountPermissions._split_operation(capability)
        if operation is None:
            return capability_base
        return f"{capability_base}.{operation}"

    @staticmethod
    def _split_operation(capability: str) -> tuple[str, CapabilityOperation | None]:
        parts = capability.split(".")
        if parts and parts[-1] in CAPABILITY_OPERATIONS:
            return ".".join(parts[:-1]), parts[-1]  # type: ignore[return-value]
        return capability, None

    @classmethod
    def _matches(cls, scope: str, capability: str, *, operation: CapabilityOperation) -> bool:
        scope_base, scope_operation = cls._split_operation(scope)
        if scope_operation is not None and scope_operation != operation:
            return False
        capability_base, capability_operation = cls._split_operation(capability)
        if capability_operation is not None:
            operation = capability_operation

        expanded_scopes = cls._expand_capability_base(scope_base)
        expanded_capabilities = cls._expand_capability_base(capability_base)
        return any(
            cls._matches_expanded_capability(
                expanded_scope,
                expanded_capability,
                operation=operation,
            )
            for expanded_scope in expanded_scopes
            for expanded_capability in expanded_capabilities
        )

    @classmethod
    def _matches_expanded_capability(
        cls,
        scope: str,
        capability: str,
        *,
        operation: CapabilityOperation,
    ) -> bool:
        scope_base, scope_operation = cls._split_operation(scope)
        if scope_operation is not None and scope_operation != operation:
            return False
        capability_base, capability_operation = cls._split_operation(capability)
        if capability_operation is not None and capability_operation != operation:
            return False
        return cls._matches_expanded_base(scope_base, capability_base)

    @classmethod
    def _matches_base(cls, scope: str, capability: str) -> bool:
        expanded_scopes = cls._expand_capability_base(scope)
        expanded_capabilities = cls._expand_capability_base(capability)
        return any(
            cls._matches_expanded_base(expanded_scope, expanded_capability)
            for expanded_scope in expanded_scopes
            for expanded_capability in expanded_capabilities
        )

    @classmethod
    def expand_capability(cls, capability: str) -> tuple[str, ...]:
        capability_base, operation = cls._split_operation(capability)
        expanded_bases = cls._expand_capability_base(capability_base)
        if operation is None:
            return expanded_bases
        expanded_capabilities: list[str] = []
        for expanded_base in expanded_bases:
            base, expanded_operation = cls._split_operation(expanded_base)
            if expanded_operation is None:
                expanded_capabilities.append(f"{base}.{operation}")
            else:
                expanded_capabilities.append(f"{base}.{expanded_operation}")
        return tuple(expanded_capabilities)

    @classmethod
    def has_preset_segment(cls, capability: str) -> bool:
        return any(cls._preset_name(part) is not None for part in cls._capability_parts(capability))

    @classmethod
    def _matches_expanded_base(cls, scope: str, capability: str) -> bool:
        scope_parts = cls._capability_parts(scope)
        capability_parts = cls._capability_parts(capability)
        return (
            cls._match_parts(scope_parts, capability_parts) or
            cls._match_prefix_parts(scope_parts, capability_parts)
        )

    @classmethod
    def _expand_capability_base(
        cls,
        capability: str,
        seen: set[str] | None = None,
    ) -> tuple[str, ...]:
        seen = set() if seen is None else seen
        if capability in seen:
            return ()
        seen = {*seen, capability}
        expanded: list[tuple[str, ...]] = [()]
        for part in cls._capability_parts(capability):
            preset_name = cls._preset_name(part)
            if preset_name is None:
                expanded = [(*path, part) for path in expanded]
                continue

            preset_capabilities = (
                tuple(cls.preset_resolver(preset_name))
                if cls.preset_resolver is not None else
                ()
            )
            if not preset_capabilities:
                return ()

            next_expanded: list[tuple[str, ...]] = []
            for path in expanded:
                for preset_capability in preset_capabilities:
                    for preset_path in cls._expand_capability_base(preset_capability, seen):
                        next_expanded.append((*path, *cls._capability_parts(preset_path)))
            expanded = next_expanded

        return tuple(".".join(path) for path in expanded if path)

    @staticmethod
    def _preset_name(part: str) -> str | None:
        if len(part) < 3 or not part.startswith("(") or not part.endswith(")"):
            return None
        name = part[1:-1].strip()
        if not name:
            return None
        if name.startswith("server:"):
            local_name = name.removeprefix("server:")
            if not local_name:
                return None
            if all(char.isascii() and (char.isalnum() or char == "_") for char in local_name):
                return name
            return None
        if not all(char.isascii() and (char.isalnum() or char == "_") for char in name):
            return None
        return name

    @staticmethod
    def _capability_parts(capability: str) -> tuple[str, ...]:
        return tuple(capability.split("."))

    @staticmethod
    def _match_prefix_parts(scope: tuple[str, ...], capability: tuple[str, ...]) -> bool:
        if not scope:
            return True
        if not capability:
            return len(scope) == 1 and scope[0] == "[all]"

        scope_head, *scope_tail = scope
        capability_head, *capability_tail = capability

        if scope_head == "[all]":
            return True
        if scope_head == "[any]" or scope_head == capability_head:
            return AccountPermissions._match_prefix_parts(tuple(scope_tail), tuple(capability_tail))
        return False

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
        capabilities = dict(global_value)
        for capability, depth in local_value.items():
            capabilities[capability] = max(capabilities.get(capability, depth), depth)
        return capabilities


class Account:
    def __init__(
        self,
        manager: "AccountManager",
        uid: int | str,
        *,
        account_type: AccountType = "user",
        guild_id: int | None = None,
    ) -> None:
        self.manager = manager
        self.uid = str(uid)
        self.account_type: AccountType = account_type
        self.guild_id: int | None = guild_id
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
        return self.manager._permissions(self.account_type, self.uid, self.guild_id)

    def local(self, guild_id: int | None) -> "Account":
        if guild_id is None:
            return self.copy()
        return self.manager._local_account(self.account_type, self.uid, guild_id)

    def copy(self) -> "Account":
        return Account.from_record(
            self.manager,
            self.uid,
            self.manager._copy_record(self.account_type, self.uid, self.guild_id),
            account_type=self.account_type,
            guild_id=self.guild_id,
        )

    @classmethod
    def from_record(
        cls,
        manager: "AccountManager",
        uid: int | str,
        record: AccountRecord,
        *,
        account_type: AccountType = "user",
        guild_id: int | None = None,
    ) -> "Account":
        account = cls(manager, uid, account_type=account_type, guild_id=guild_id)
        account._record = record
        return account

    @property
    def record(self) -> AccountRecord:
        if self._record is not None:
            return self._record
        return self.manager._record_or_fake(self.account_type, self.uid, self.guild_id)

    def __getitem__(self, key: str) -> Any:
        return self.record[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.record.get(key, default)

    async def write(self, key: str, value: Any) -> None:
        async with self.manager.lock:
            record = self.manager._writable_record_locked(self.account_type, self.uid, self.guild_id)
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
        role_ids_for_user: Callable[[int, str], Sequence[int | str]] | None = None,
    ) -> None:
        self.path = path
        self.role_ids_for_user = role_ids_for_user
        self.accounts: dict[AccountKey, AccountRecord] = {}
        self.capabilities = CapabilityRegistry()
        self.inheritance = AccountInheritance()
        self._lock = asyncio.Lock()
        self._ensure_file()
        self._load_sync()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def __getitem__(self, uid: int | str) -> Account:
        return Account(self, uid, account_type="user")

    def role(self, role_id: int | str) -> Account:
        return Account(self, role_id, account_type="role")

    def guild(self, guild_id: int | str) -> Account:
        return Account(self, guild_id, account_type="guild")

    def _ensure_file(self) -> None:
        if os.path.exists(self.path):
            return

        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "accounts": []}, f)

    def _load_sync(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw_accounts = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw_accounts = {}

        self.accounts = self._normalize_accounts(raw_accounts)

    def _normalize_accounts(self, raw_accounts: object) -> dict[AccountKey, AccountRecord]:
        if isinstance(raw_accounts, dict) and isinstance(raw_accounts.get("accounts"), list):
            return self._normalize_account_rows(raw_accounts.get("accounts"))

        if not isinstance(raw_accounts, dict):
            return {}

        accounts: dict[AccountKey, AccountRecord] = {}
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
            raw_local_accounts = account.pop(LOCAL_ACCOUNTS_KEY, {})
            accounts[self._account_key("user", uid, None)] = account
            for guild_id, local_account in self._normalize_local_accounts(raw_local_accounts).items():
                accounts[self._account_key("user", uid, guild_id)] = local_account

        return accounts

    def _normalize_account_rows(self, raw_rows: object) -> dict[AccountKey, AccountRecord]:
        if not isinstance(raw_rows, list):
            return {}
        accounts: dict[AccountKey, AccountRecord] = {}
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            account_type = self._normalize_account_type(raw_row.get("type"))
            if account_type is None:
                continue
            account_id = str(raw_row.get("id", "")).strip()
            if not account_id:
                continue
            scope = self._normalize_scope(raw_row.get("scope"))
            raw_data = raw_row.get("data")
            if not isinstance(raw_data, dict):
                continue
            accounts[(scope, account_type, account_id)] = self._normalize_account_record(
                raw_data,
                default_user_capabilities=account_type == "user" and scope == GLOBAL_SCOPE,
            )
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

    def _normalize_account_record(
        self,
        value: object,
        *,
        default_user_capabilities: bool = False,
    ) -> AccountRecord:
        if not isinstance(value, dict):
            return {}
        record = dict(value)
        record.pop("perm_level", None)
        record.pop(LOCAL_ACCOUNTS_KEY, None)
        if PERMISSIONS_KEY in record or default_user_capabilities:
            permissions = self._normalize_permissions(record.get(PERMISSIONS_KEY))
            if default_user_capabilities:
                permissions.capabilities.setdefault(DEFAULT_COMMAND_CAPABILITY, 0)
                permissions.capabilities.setdefault(DEFAULT_USER_CAPABILITY, 0)
            record[PERMISSIONS_KEY] = permissions
        return record

    def _normalize_account_type(self, value: object) -> AccountType | None:
        if value in ("user", "role", "guild"):
            return value
        return None

    def _normalize_scope(self, value: object) -> AccountScope:
        scope = str(value).strip() if value is not None else GLOBAL_SCOPE
        return scope if scope else GLOBAL_SCOPE

    def _normalize_permissions(self, value: object) -> AccountPermissions:
        if isinstance(value, AccountPermissions):
            return value
        if isinstance(value, dict):
            return AccountPermissions.model_validate(value)
        return AccountPermissions()

    def _scope_key(self, guild_id: int | str | None) -> AccountScope:
        if guild_id is None:
            return GLOBAL_SCOPE
        return str(guild_id)

    def _account_key(
        self,
        account_type: AccountType,
        account_id: int | str,
        guild_id: int | str | None,
    ) -> AccountKey:
        return (self._scope_key(guild_id), account_type, str(account_id))

    def _default_record(
        self,
        account_type: AccountType,
        guild_id: int | None,
    ) -> AccountRecord:
        if account_type == "user" and guild_id is None:
            return {
                PERMISSIONS_KEY: AccountPermissions(capabilities=default_capabilities()),
            }
        return {}

    async def save(self) -> None:
        async with self._lock:
            self._save_sync()

    def _save_sync(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(self._json_accounts_text())
        os.replace(tmp_path, self.path)

    def _json_accounts_text(self) -> str:
        data = self._json_accounts()
        rows = data["accounts"]
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
        return "\n".join(lines)

    def _json_accounts(self) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for scope, account_type, account_id in sorted(self.accounts):
            account = self.accounts[(scope, account_type, account_id)]
            json_account = self._json_account_record(account)
            if not json_account:
                continue
            rows.append({
                "scope": scope,
                "type": account_type,
                "id": account_id,
                "data": json_account,
            })
        return {
            "version": 2,
            "accounts": rows,
        }

    def _json_account_record(self, account: AccountRecord) -> AccountRecord:
        json_account = dict(account)
        if PERMISSIONS_KEY in json_account:
            perms = self._normalize_permissions(json_account.get(PERMISSIONS_KEY))
            json_account[PERMISSIONS_KEY] = perms.model_dump(mode="json")
        json_account.pop(LOCAL_ACCOUNTS_KEY, None)
        return json_account

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
            return self._account_key("user", str(uid), None) in self.accounts

    async def items(self) -> list[tuple[str, AccountRecord]]:
        async with self._lock:
            return [
                (account_id, dict(account))
                for (scope, account_type, account_id), account in self.accounts.items()
                if scope == GLOBAL_SCOPE and account_type == "user"
            ]

    async def query(self, key: str) -> list[tuple[str, Any]]:
        async with self._lock:
            return [
                (account_id, account[key])
                for (scope, account_type, account_id), account in self.accounts.items()
                if scope == GLOBAL_SCOPE and account_type == "user"
                if key in account
            ]

    async def query_local(
        self,
        guild_id: int | str,
        key: str,
        *,
        account_type: AccountType = "user",
    ) -> list[tuple[str, Any]]:
        scope = self._scope_key(guild_id)
        async with self._lock:
            return [
                (account_id, account[key])
                for (record_scope, record_type, account_id), account in self.accounts.items()
                if record_scope == scope and record_type == account_type
                if key in account
            ]

    async def local_scope_ids(
        self,
        account_type: AccountType,
        account_id: int | str,
        *,
        with_permissions: bool = False,
    ) -> list[int]:
        async with self._lock:
            return self._local_scope_ids_locked(
                account_type,
                str(account_id),
                with_permissions=with_permissions,
            )

    def _local_scope_ids_locked(
        self,
        account_type: AccountType,
        account_id: str,
        *,
        with_permissions: bool = False,
    ) -> list[int]:
        scope_ids: list[int] = []
        for scope, record_type, record_id in self.accounts:
            if scope == GLOBAL_SCOPE or record_type != account_type or record_id != account_id:
                continue
            if with_permissions and PERMISSIONS_KEY not in self.accounts[(scope, record_type, record_id)]:
                continue
            try:
                scope_ids.append(int(scope))
            except ValueError:
                continue
        return sorted(scope_ids)

    def _ensure_account_locked(
        self,
        uid: str,
    ) -> bool:
        key = self._account_key("user", uid, None)
        if key in self.accounts:
            return False

        self.accounts[key] = {
            PERMISSIONS_KEY: AccountPermissions(capabilities=default_capabilities()),
        }
        return True

    def _account_locked(
        self,
        account_type: AccountType,
        account_id: str,
        guild_id: int | None,
    ) -> AccountRecord:
        if account_type == "user" and guild_id is None:
            self._ensure_account_locked(account_id)
        key = self._account_key(account_type, account_id, guild_id)
        if key not in self.accounts:
            self.accounts[key] = self._default_record(account_type, guild_id)
        return self.accounts[key]

    def _writable_record_locked(
        self,
        account_type: AccountType,
        account_id: str,
        guild_id: int | None,
    ) -> AccountRecord:
        return self._account_locked(account_type, account_id, guild_id)

    def _record_or_fake(
        self,
        account_type: AccountType,
        account_id: str,
        guild_id: int | None,
    ) -> AccountRecord:
        return self.accounts.get(
            self._account_key(account_type, account_id, guild_id),
            self._default_record(account_type, guild_id),
        )

    def _permissions(
        self,
        account_type: AccountType,
        account_id: str,
        guild_id: int | None,
    ) -> AccountPermissions:
        record = self._record_or_fake(account_type, account_id, guild_id)
        perms = self._normalize_permissions(record.get(PERMISSIONS_KEY))
        key = self._account_key(account_type, account_id, guild_id)
        if key in self.accounts:
            record[PERMISSIONS_KEY] = perms
        return perms

    def _copy_record(
        self,
        account_type: AccountType,
        account_id: str,
        guild_id: int | None,
    ) -> AccountRecord:
        record = self._record_or_fake(account_type, account_id, guild_id)
        copied = dict(record)
        if PERMISSIONS_KEY in copied:
            copied[PERMISSIONS_KEY] = self._normalize_permissions(copied.get(PERMISSIONS_KEY)).model_copy(deep=True)
        return copied

    def _role_permissions_for_user(self, account_id: str, guild_id: int) -> AccountPermissions:
        if self.role_ids_for_user is None:
            return AccountPermissions()
        if guild_id == GLOBAL_GUILD_ACCOUNT_ID:
            return AccountPermissions()

        permissions = AccountPermissions()
        for role_id in self.role_ids_for_user(guild_id, account_id):
            role_record = self._record_or_fake("role", str(role_id), guild_id)
            permissions = self._inherit_permissions(
                permissions,
                self._normalize_permissions(role_record.get(PERMISSIONS_KEY)),
            )
        return permissions

    def _local_account(self, account_type: AccountType, account_id: str, guild_id: int) -> Account:
        global_record = self._copy_record(account_type, account_id, None)
        local_record = self._copy_record(account_type, account_id, guild_id)

        merged = dict(global_record)
        merged.update(local_record)
        global_permissions = self._normalize_permissions(global_record.get(PERMISSIONS_KEY))
        local_permissions = self._normalize_permissions(local_record.get(PERMISSIONS_KEY))
        inherited_permissions = self._inherit_permissions(global_permissions, local_permissions)
        if account_type == "user":
            inherited_permissions = self._inherit_permissions(
                self._inherit_permissions(global_permissions, self._role_permissions_for_user(account_id, guild_id)),
                local_permissions,
            )
        merged[PERMISSIONS_KEY] = inherited_permissions
        return Account.from_record(
            self,
            account_id,
            merged,
            account_type=account_type,
            guild_id=guild_id,
        )

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
