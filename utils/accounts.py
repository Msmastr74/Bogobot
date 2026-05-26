import asyncio
import json
import os
from typing import Any, Literal, Sequence


AccountRecord = dict[str, Any]


class Account:
    def __init__(
        self,
        manager: "AccountManager",
        uid: int | str,
    ) -> None:
        self.manager = manager
        self.uid = str(uid)

    @property
    def lock(self) -> asyncio.Lock:
        return self.manager.lock

    def __getitem__(self, key: str) -> Any:
        return self.manager._record_or_fake(self.uid)[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.manager._record_or_fake(self.uid).get(key, default)

    async def write(self, key: str, value: Any) -> None:
        async with self.manager.lock:
            record = self.manager._account_locked(self.uid)
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

            try:
                perm_level = max(0, int(raw_account.get("perm_level", 0)))
            except (TypeError, ValueError):
                perm_level = 0

            account: AccountRecord = dict(raw_account)
            account["perm_level"] = perm_level
            annotations = account.pop("annotations", None)
            if isinstance(annotations, dict):
                for key, value in annotations.items():
                    account.setdefault(str(key), value)

            accounts[uid] = account

        return accounts

    async def save(self) -> None:
        async with self._lock:
            self._save_sync()

    def _save_sync(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.accounts, f, indent=4)
        os.replace(tmp_path, self.path)

    async def ensure_account(
        self,
        uid: int | str,
        *,
        perm_level: int = 0,
        save: bool = False,
    ) -> bool:
        async with self._lock:
            created = self._ensure_account_locked(str(uid), perm_level=perm_level)
            if created and save:
                self._save_sync()
            return created

    async def ensure_accounts(
        self,
        uids: Sequence[int | str],
        *,
        perm_level: int = 0,
    ) -> int:
        async with self._lock:
            added = 0
            for uid in uids:
                if self._ensure_account_locked(str(uid), perm_level=perm_level):
                    added += 1
            return added

    async def normalize_owner(self, owner_uid: int | str) -> None:
        owner_uid_str = str(owner_uid)
        async with self._lock:
            for uid, account in self.accounts.items():
                if account["perm_level"] == 4 and uid != owner_uid_str:
                    account["perm_level"] = 3

            if owner_uid_str in self.accounts:
                self.accounts[owner_uid_str]["perm_level"] = 4

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

    def _ensure_account_locked(
        self,
        uid: str,
        *,
        perm_level: int = 0,
    ) -> bool:
        if uid in self.accounts:
            return False

        self.accounts[uid] = {"perm_level": max(0, int(perm_level))}
        return True

    async def set_permission_level(self, uid: int | str, level: int) -> None:
        async with self._lock:
            account = self._account_locked(str(uid))
            account["perm_level"] = max(0, int(level))
            self._save_sync()

    async def permission_level(self, uid: int | str) -> int:
        async with self._lock:
            account = self._record_or_fake(str(uid))
            return int(account.get("perm_level", 0))

    async def set_permission_level_if_overranked(
        self,
        *,
        actor_uid: int | str,
        target_uid: int | str,
        new_level: int,
    ) -> tuple[
        Literal['same', 'actor_not_over_current', 'actor_not_over_new', 'ok'],
        int, int
    ]:
        async with self._lock:
            target = self.accounts.get(str(target_uid))
            actor = self.accounts.get(str(actor_uid))

            current_level = int(self._record_or_fake(str(target_uid)).get("perm_level", 0))
            actor_level = int(actor.get("perm_level", 0)) if actor is not None else 0
            new_level = max(0, int(new_level))

            if current_level == new_level:
                return "same", current_level, actor_level
            if actor_level <= current_level:
                return "actor_not_over_current", current_level, actor_level
            if actor_level <= new_level:
                return "actor_not_over_new", current_level, actor_level

            target = self._account_locked(str(target_uid))
            target["perm_level"] = new_level
            self._save_sync()
            return "ok", current_level, actor_level

    def authorization_level(self, uid: int | str) -> int:
        account = self.accounts.get(str(uid))
        if account is None:
            return 0

        return int(account.get("perm_level", 0))

    def is_authorized(self, uid: int | str, perm_requirement: int) -> bool:
        return self.authorization_level(uid) >= perm_requirement

    def _account_locked(self, uid: str) -> AccountRecord:
        self._ensure_account_locked(uid)
        return self.accounts[uid]

    def _record_or_fake(self, uid: str) -> AccountRecord:
        return self.accounts.get(uid, {"perm_level": 0})
