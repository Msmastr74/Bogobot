from __future__ import annotations

import abc
import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class SandboxError(Exception):
    pass


class SandboxSetupError(SandboxError):
    pass


class SandboxTimeoutError(SandboxError):
    pass


class SandboxOutputLimitError(SandboxError):
    pass


class SandboxFilesystemLimitError(SandboxError):
    pass


@dataclass(frozen=True)
class Mount:
    host: Path
    guest: str


@dataclass(frozen=True)
class LanguageInvocation:
    wasm_path: Path
    args: tuple[str, ...] = ()
    stdin: bytes = b""
    mounts: tuple[Mount, ...] = ()
    watch_roots: tuple[Path, ...] = ()
    cleanup_roots: tuple[Path, ...] = ()


@dataclass
class Language(abc.ABC):
    """
    Base class for languages executed by SandboxedExecutor.

    A language adapter owns language-specific setup, cache management, runtime
    layout, argv construction, and stdin encoding. SandboxedExecutor owns the
    generic Wasmtime process, limits, output capture, watchdogs, and cleanup.
    """

    max_program_bytes: int = 128 * 1024

    @property
    @abc.abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    async def prepare(self, program: str) -> LanguageInvocation:
        encoded = program.encode("utf-8")
        if len(encoded) > self.max_program_bytes:
            raise ValueError(
                f"{self.name} program is too large; max is "
                f"{self.max_program_bytes} bytes"
            )
        return await self._prepare(encoded)

    @abc.abstractmethod
    async def _prepare(self, program: bytes) -> LanguageInvocation:
        raise NotImplementedError

    async def cleanup(self, invocation: LanguageInvocation) -> None:
        for root in invocation.cleanup_roots:
            await asyncio.to_thread(self._remove_tree, root)

    def _replace_dir(self, src: Path, dst: Path) -> None:
        """
        Replace dst with a copy of src.

        Handles previously chmod'd or otherwise awkward permissions by making
        dst/tmp writable before deleting them.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)

        tmp = dst.parent / f".tmp-{dst.name}"

        self._remove_tree(tmp)
        shutil.copytree(src, tmp, symlinks=True)

        self._remove_tree(dst)
        tmp.rename(dst)

    def _remove_tree(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return

        if path.is_symlink() or path.is_file():
            try:
                self._make_one_writable(path)
                path.unlink()
            except FileNotFoundError:
                pass
            return

        def onerror(func: Callable[..., Any], p: str, _: Any) -> None:
            failed_path = Path(p)
            self._make_one_writable(failed_path)
            func(failed_path)

        if "onexc" in inspect.signature(shutil.rmtree).parameters:
            shutil.rmtree(path, onexc=onerror)
        else:
            shutil.rmtree(path, onerror=onerror)

    @staticmethod
    def _make_one_writable(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
            path.chmod(mode | 0o700)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class PythonWasiInstall:
    root: Path
    python_wasm: Path
    python_version: str


@dataclass
class PythonLanguage(Language):
    # Trusted cache dir. This is never mounted into WASI.
    cache_root: Path = Path("./python-wasi")

    # Writable/disposable runtime dir. This is what gets mounted.
    runtime_root: Path = Path("./python-wasi-runtime")

    python_version: str | None = None

    releases_url: str = (
        "https://api.github.com/repos/brettcannon/cpython-wasi-build/releases"
    )

    @property
    def name(self) -> str:
        return "python"

    async def _prepare(self, program: bytes) -> LanguageInvocation:
        cache_install = await asyncio.to_thread(self._ensure_python_wasi)
        await asyncio.to_thread(self._prepare_runtime_tree, cache_install)

        runtime_install = self._inspect_install(self.runtime_root)
        if runtime_install is None:
            raise SandboxSetupError("runtime CPython WASI tree is invalid")

        return LanguageInvocation(
            wasm_path=runtime_install.python_wasm,
            args=("-I", "-B"),
            stdin=program,
            mounts=(Mount(runtime_install.root, "/"),),
            watch_roots=(runtime_install.root,),
            cleanup_roots=(runtime_install.root,),
        )

    def _ensure_python_wasi(self) -> PythonWasiInstall:
        """
        Ensure self.cache_root is the trusted extracted CPython WASI cache.

        After setup, self.cache_root should directly contain things like:

            ./python-wasi/
              python.wasm
              lib/
                python3.14/

        This directory is not mounted into the sandbox.
        """
        existing = self._inspect_install(self.cache_root)
        if existing is not None:
            return existing

        self.cache_root.parent.mkdir(parents=True, exist_ok=True)

        releases = self._fetch_releases()
        asset = self._select_asset(releases)

        zip_path = self.cache_root.with_suffix(".zip")
        self._download_asset(asset, zip_path)
        self._verify_asset_digest(asset, zip_path)

        tmp_extract = self.cache_root.parent / f".extracting-{self.cache_root.name}"
        tmp_old = self.cache_root.parent / f".old-{self.cache_root.name}"

        self._remove_tree(tmp_extract)
        tmp_extract.mkdir(parents=True)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_extract)
            os.remove(zip_path)

            extracted_root = self._normalize_extracted_root(tmp_extract)

            candidate = self._inspect_install(extracted_root)
            if candidate is None:
                raise SandboxSetupError(
                    f"downloaded archive does not look like CPython WASI: "
                    f"{asset.get('name')}"
                )

            self._remove_tree(tmp_old)

            if self.cache_root.exists():
                self.cache_root.rename(tmp_old)

            extracted_root.rename(self.cache_root)
            self._remove_tree(tmp_old)

        except Exception:
            if not self.cache_root.exists() and tmp_old.exists():
                tmp_old.rename(self.cache_root)
            raise

        finally:
            self._remove_tree(tmp_extract)
            self._remove_tree(tmp_old)

        install = self._inspect_install(self.cache_root)
        if install is None:
            raise SandboxSetupError("failed to install CPython WASI")

        return install

    def _prepare_runtime_tree(self, cache_install: PythonWasiInstall) -> None:
        self._replace_dir(cache_install.root, self.runtime_root)

    def _fetch_releases(self) -> list[dict[str, Any]]:
        req = urllib.request.Request(
            self.releases_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "sandboxed-executor",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, list):
            raise SandboxSetupError("GitHub releases response was not a list")

        return data

    def _select_asset(self, releases: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Select a release asset.

        python_version examples:
          None      -> newest available normal asset
          "3.14"    -> newest 3.14.x asset
          "3.14.5"  -> exact 3.14.5 asset
        """
        pattern = re.compile(
            r"^python-(?P<version>\d+\.\d+\.\d+)-wasi_sdk-(?P<sdk>\d+)\.zip$"
        )

        for release in releases:
            assets = release.get("assets", [])
            if not isinstance(assets, list):
                continue

            for asset in assets:
                if not isinstance(asset, dict):
                    continue

                name = asset.get("name")
                if not isinstance(name, str):
                    continue

                if name.startswith("_build-"):
                    continue

                match = pattern.fullmatch(name)
                if match is None:
                    continue

                version = match.group("version")

                if self.python_version is None:
                    return asset

                if re.fullmatch(r"\d+\.\d+\.\d+", self.python_version):
                    if version == self.python_version:
                        return asset

                elif re.fullmatch(r"\d+\.\d+", self.python_version):
                    if version.startswith(self.python_version + "."):
                        return asset

                else:
                    raise ValueError(
                        "python_version must look like '3.14' or '3.14.5'"
                    )

        raise SandboxSetupError(
            f"no CPython WASI asset found for version {self.python_version!r}"
        )

    def _download_asset(self, asset: dict[str, Any], destination: Path) -> None:
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            raise SandboxSetupError("release asset has no browser_download_url")

        tmp = destination.with_suffix(destination.suffix + ".tmp")

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "sandboxed-executor"},
        )

        with urllib.request.urlopen(req, timeout=120) as response:
            with tmp.open("wb") as f:
                shutil.copyfileobj(response, f)

        tmp.replace(destination)

    def _verify_asset_digest(self, asset: dict[str, Any], path: Path) -> None:
        digest = asset.get("digest")

        if not isinstance(digest, str):
            return

        if not digest.startswith("sha256:"):
            return

        expected = digest.removeprefix("sha256:").lower()
        actual = self._sha256_file(path).lower()

        if actual != expected:
            raise SandboxSetupError(
                f"sha256 mismatch for {path.name}: expected {expected}, got {actual}"
            )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()

        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)

        return h.hexdigest()

    @staticmethod
    def _normalize_extracted_root(tmp_extract: Path) -> Path:
        children = list(tmp_extract.iterdir())

        if len(children) == 1 and children[0].is_dir():
            return children[0]

        return tmp_extract

    def _inspect_install(self, root: Path) -> PythonWasiInstall | None:
        if not root.is_dir():
            return None

        python_wasm = self._find_python_wasm(root)
        python_version = self._find_stdlib_version(root)

        if python_wasm is None or python_version is None:
            return None

        return PythonWasiInstall(
            root=root,
            python_wasm=python_wasm,
            python_version=python_version,
        )

    @staticmethod
    def _find_python_wasm(root: Path) -> Path | None:
        candidates = [
            root / "python.wasm",
            root / "bin" / "python.wasm",
        ]

        for candidate in candidates:
            if candidate.is_file():
                return candidate

        matches = sorted(root.rglob("python*.wasm"))
        return matches[0] if matches else None

    @staticmethod
    def _find_stdlib_version(root: Path) -> str | None:
        lib = root / "lib"
        if not lib.is_dir():
            return None

        versions = sorted(
            p.name.removeprefix("python")
            for p in lib.iterdir()
            if p.is_dir() and re.fullmatch(r"python\d+\.\d+", p.name)
        )

        return versions[-1] if versions else None


@dataclass(frozen=True)
class JavascriptWasiInstall:
    root: Path
    js_wasm: Path
    branch: str
    buildid: str
    rev: str
    url: str


@dataclass
class JavascriptLanguage(Language):
    """
    JavaScript language adapter backed by Mozilla's SpiderMonkey WASI shell.

    This intentionally uses no preopened directories. User source is passed on
    stdin instead of writing an input file into a mounted filesystem.
    """

    cache_root: Path = Path("./javascript-wasi")
    branch: str = "mozilla-release"
    data_url: str = (
        "https://raw.githubusercontent.com/"
        "mozilla-spidermonkey/sm-wasi-demo/main/data.json"
    )
    manifest_name: str = "manifest.json"
    wasm_name: str = "js.wasm"
    allow_stale_cache_on_fetch_error: bool = True

    @property
    def name(self) -> str:
        return "javascript"

    async def _prepare(self, program: bytes) -> LanguageInvocation:
        install = await asyncio.to_thread(self._ensure_javascript_wasi)

        return LanguageInvocation(
            wasm_path=install.js_wasm,
            args=(),
            stdin=program,
            mounts=(),
            watch_roots=(),
            cleanup_roots=(),
        )

    def _ensure_javascript_wasi(self) -> JavascriptWasiInstall:
        existing = self._inspect_install(self.cache_root)

        try:
            entries = self._fetch_data()
            selected = self._select_entry(entries)
        except Exception:
            if existing is not None and self.allow_stale_cache_on_fetch_error:
                return existing
            raise

        if existing is not None and self._install_matches_entry(existing, selected):
            return existing

        self.cache_root.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_root.parent / f".tmp-{self.cache_root.name}"
        tmp_old = self.cache_root.parent / f".old-{self.cache_root.name}"

        self._remove_tree(tmp)
        tmp.mkdir(parents=True)

        try:
            wasm_path = tmp / self.wasm_name
            self._download_wasm(selected, wasm_path)

            manifest = self._manifest_from_entry(selected)
            (tmp / self.manifest_name).write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )

            candidate = self._inspect_install(tmp)
            if candidate is None:
                raise SandboxSetupError("downloaded SpiderMonkey WASI cache is invalid")

            self._remove_tree(tmp_old)
            if self.cache_root.exists():
                self.cache_root.rename(tmp_old)
            tmp.rename(self.cache_root)
            self._remove_tree(tmp_old)

        except Exception:
            if not self.cache_root.exists() and tmp_old.exists():
                tmp_old.rename(self.cache_root)
            raise

        finally:
            self._remove_tree(tmp)
            self._remove_tree(tmp_old)

        install = self._inspect_install(self.cache_root)
        if install is None:
            raise SandboxSetupError("failed to install SpiderMonkey WASI")

        return install

    def _fetch_data(self) -> list[dict[str, Any]]:
        req = urllib.request.Request(
            self.data_url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "User-Agent": "sandboxed-executor",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if not isinstance(data, list):
            raise SandboxSetupError("SpiderMonkey data.json response was not a list")

        return data

    def _select_entry(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("branch") == self.branch:
                self._validate_entry(entry)
                return entry

        available = sorted(
            str(entry.get("branch"))
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("branch"), str)
        )
        raise SandboxSetupError(
            f"no SpiderMonkey WASI entry found for branch {self.branch!r}; "
            f"available branches: {', '.join(available) or 'none'}"
        )

    @staticmethod
    def _validate_entry(entry: dict[str, Any]) -> None:
        for key in ("branch", "url", "buildid", "rev"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise SandboxSetupError(
                    f"SpiderMonkey data.json entry is missing {key!r}"
                )

    @staticmethod
    def _manifest_from_entry(entry: dict[str, Any]) -> dict[str, str]:
        return {
            "branch": entry["branch"],
            "url": entry["url"],
            "buildid": entry["buildid"],
            "rev": entry["rev"],
        }

    @staticmethod
    def _install_matches_entry(
        install: JavascriptWasiInstall,
        entry: dict[str, Any],
    ) -> bool:
        return (
            install.branch == entry["branch"]
            and install.buildid == entry["buildid"]
            and install.rev == entry["rev"]
            and install.url == entry["url"]
        )

    def _download_wasm(self, entry: dict[str, Any], destination: Path) -> None:
        url = entry["url"]
        tmp = destination.with_suffix(destination.suffix + ".tmp")

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "sandboxed-executor"},
        )

        with urllib.request.urlopen(req, timeout=120) as response:
            with tmp.open("wb") as f:
                shutil.copyfileobj(response, f)

        tmp.replace(destination)

    def _inspect_install(self, root: Path) -> JavascriptWasiInstall | None:
        if not root.is_dir():
            return None

        js_wasm = root / self.wasm_name
        manifest_path = root / self.manifest_name
        if not js_wasm.is_file() or not manifest_path.is_file():
            return None

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(manifest, dict):
            return None

        values: dict[str, str] = {}
        for key in ("branch", "url", "buildid", "rev"):
            value = manifest.get(key)
            if not isinstance(value, str) or not value:
                return None
            values[key] = value

        return JavascriptWasiInstall(
            root=root,
            js_wasm=js_wasm,
            branch=values["branch"],
            buildid=values["buildid"],
            rev=values["rev"],
            url=values["url"],
        )


@dataclass
class SandboxedExecutor:
    language: Language

    max_memory_size: int = 128 * 1024 * 1024
    max_wasm_stack: int = 512 * 1024
    fuel: int | None = None

    max_output_bytes: int = 256 * 1024
    max_runtime_tree_bytes: int = 128 * 1024 * 1024
    runtime_tree_watch_interval: float = 0.05

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def execute(self, program: str, timeout: float = 60) -> str:
        async with self._lock:
            return await self._execute_locked(program, timeout)

    @staticmethod
    def _wasmtime_timeout(timeout: float) -> str:
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        milliseconds = max(1, int(timeout * 1000))
        return f"{milliseconds}ms"

    async def _execute_locked(self, program: str, timeout: float) -> str:
        wasmtime = shutil.which("wasmtime")
        if wasmtime is None:
            raise SandboxSetupError("wasmtime was not found on PATH")

        invocation = await self.language.prepare(program)

        proc = await asyncio.create_subprocess_exec(
            wasmtime,
            "run",
            *(["-W", f"fuel={self.fuel}"] if self.fuel is not None else []),
            "-W", f"timeout={self._wasmtime_timeout(timeout)}",
            "-W", f"max-memory-size={self.max_memory_size}",
            "-W", f"max-wasm-stack={self.max_wasm_stack}",
            "-W", "max-instances=1",
            "-W", "max-memories=1",
            "-W", "max-tables=1",
            *self._mount_args(invocation.mounts),
            str(invocation.wasm_path),
            *invocation.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            async def write_to_stdin() -> None:
                if proc.stdin is None:
                    return

                try:
                    proc.stdin.write(invocation.stdin)
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    return

            output_task = asyncio.create_task(
                self._read_limited(proc.stdout, self.max_output_bytes, "output")
            )
            watchdog_task = asyncio.create_task(
                self._watch_runtime_tree_size(proc, invocation.watch_roots)
            )
            write_task = asyncio.create_task(write_to_stdin())

            tasks: set[asyncio.Task[Any]] = {
                output_task,
                watchdog_task,
                write_task,
            }

            try:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=timeout + 1.0,
                    return_when=asyncio.FIRST_EXCEPTION,
                )

                if pending:
                    self._kill(proc)
                    raise SandboxTimeoutError(f"execution timed out after {timeout}s")

                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        self._kill(proc)
                        raise exc

                output_bytes = output_task.result()

            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()

                for task in tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            await proc.wait()
            return output_bytes.decode("utf-8", errors="replace")

        except (SandboxOutputLimitError, SandboxFilesystemLimitError):
            self._kill(proc)
            raise

        finally:
            if proc.returncode is None:
                self._kill(proc)
                await proc.wait()

            await self.language.cleanup(invocation)

    @staticmethod
    def _mount_args(mounts: tuple[Mount, ...]) -> tuple[str, ...]:
        args: list[str] = []
        for mount in mounts:
            args.extend(("--dir", f"{mount.host}::{mount.guest}"))
        return tuple(args)

    def _tree_size_or_none(self, root: Path) -> int | None:
        """
        Return a conservative size of all filesystem entries in the tree.

        Uses max(apparent size, allocated blocks) where available. This catches
        both normal file growth and sparse-file apparent-size abuse. Returns None
        if the tree cannot be traversed/read reliably.
        """
        try:
            total = 0

            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                base = Path(dirpath)

                for name in dirnames:
                    path = base / name
                    try:
                        if path.is_symlink():
                            total += self._size_for_limit(path)
                            if total > self.max_runtime_tree_bytes:
                                return total
                    except OSError:
                        return None

                for name in filenames:
                    path = base / name
                    try:
                        total += self._size_for_limit(path)
                        if total > self.max_runtime_tree_bytes:
                            return total
                    except OSError:
                        return None

            return total

        except OSError:
            return None

    @staticmethod
    def _size_for_limit(path: Path) -> int:
        st = path.lstat()
        apparent = st.st_size
        blocks = getattr(st, "st_blocks", None)
        if blocks is None:
            return apparent
        allocated = blocks * 512
        return max(apparent, allocated)

    async def _watch_runtime_tree_size(
        self,
        proc: asyncio.subprocess.Process,
        roots: tuple[Path, ...],
    ) -> None:
        if not roots:
            return

        while proc.returncode is None:
            total = 0
            for root in roots:
                size = await asyncio.to_thread(self._tree_size_or_none, root)

                if size is None:
                    self._kill(proc)
                    raise SandboxFilesystemLimitError(
                        "runtime filesystem became unreadable"
                    )

                total += size
                if total > self.max_runtime_tree_bytes:
                    self._kill(proc)
                    raise SandboxFilesystemLimitError(
                        f"runtime filesystem exceeded "
                        f"{self.max_runtime_tree_bytes} bytes"
                    )

            await asyncio.sleep(self.runtime_tree_watch_interval)

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader | None,
        limit: int,
        name: str,
    ) -> bytes:
        if stream is None:
            return b""

        data = bytearray()

        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return bytes(data)

            data += chunk
            if len(data) > limit:
                raise SandboxOutputLimitError(f"{name} exceeded {limit} bytes")

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return

        try:
            proc.kill()
        except ProcessLookupError:
            pass
