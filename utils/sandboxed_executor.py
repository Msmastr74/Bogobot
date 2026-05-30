import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import urllib.request
import zipfile
import inspect
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
class PythonWasiInstall:
    root: Path
    python_wasm: Path
    python_version: str


@dataclass
class SandboxedExecutor:
    # Trusted cache dir. This is never mounted into WASI.
    cache_root: Path = Path("./python-wasi")

    # Writable/disposable runtime dir. This is what gets mounted.
    runtime_root: Path = Path("./python-wasi-runtime")

    python_version: str | None = None

    max_memory_size: int = 128 * 1024 * 1024
    max_wasm_stack: int = 512 * 1024
    fuel: int | None = None

    max_output_bytes: int = 256 * 1024
    max_program_bytes: int = 128 * 1024
    
    max_runtime_tree_bytes: int = 128 * 1024 * 1024
    runtime_tree_watch_interval: float = 0.05

    releases_url: str = (
        "https://api.github.com/repos/brettcannon/cpython-wasi-build/releases"
    )

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
        if len(program.encode("utf-8")) > self.max_program_bytes:
            raise ValueError(
                f"program is too large; max is {self.max_program_bytes} bytes"
            )

        wasmtime = shutil.which("wasmtime")
        if wasmtime is None:
            raise SandboxSetupError("wasmtime was not found on PATH")

        # Ensure trusted cache exists.
        cache_install = await asyncio.to_thread(self._ensure_python_wasi)

        await asyncio.to_thread(self._prepare_runtime_tree, cache_install)
        runtime_install = self._inspect_install(self.runtime_root)
        if runtime_install is None:
            raise SandboxSetupError("runtime CPython WASI tree is invalid")

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
            "--dir", f"{runtime_install.root}::/",
            str(runtime_install.python_wasm),
            "-I",
            "-B",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

        try:
            async def write_to_stdin() -> None:
                if proc.stdin is None:
                    return

                try:
                    proc.stdin.write(program.encode("utf-8", errors="ignore"))
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    return

            output_task = asyncio.create_task(
                self._read_limited(proc.stdout, self.max_output_bytes, "output")
            )
            watchdog_task = asyncio.create_task(
                self._watch_runtime_tree_size(proc)
            )
            write_task = asyncio.create_task(write_to_stdin())

            tasks: set[asyncio.Task] = {output_task, watchdog_task, write_task}

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

            await asyncio.to_thread(self._remove_tree, self.runtime_root)
    
    def _tree_size_or_none(self, root: Path) -> int | None:
        """
        Return the apparent size of all files in the tree.

        Returns None if the tree cannot be traversed/read reliably. For the runtime
        tree, callers should treat None as unsafe and repair/replace the tree.
        """
        try:
            total = 0

            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                base = Path(dirpath)

                # Count symlink directory entries themselves, not targets.
                for name in dirnames:
                    path = base / name
                    try:
                        if path.is_symlink():
                            st = path.lstat()
                            total += max(st.st_size, getattr(st, "st_blocks", 0) * 512)
                            if total > self.max_runtime_tree_bytes:
                                return total
                    except OSError:
                        return None

                for name in filenames:
                    path = base / name
                    try:
                        total += path.lstat().st_size
                        if total > self.max_runtime_tree_bytes:
                            return total
                    except OSError:
                        return None

            return total

        except OSError:
            return None

    async def _watch_runtime_tree_size(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        while proc.returncode is None:
            size = await asyncio.to_thread(self._tree_size_or_none, self.runtime_root)

            if size is None:
                self._kill(proc)
                raise SandboxFilesystemLimitError(
                    "runtime filesystem became unreadable"
                )

            if size > self.max_runtime_tree_bytes:
                self._kill(proc)
                raise SandboxFilesystemLimitError(
                    f"runtime filesystem exceeded {self.max_runtime_tree_bytes} bytes"
                )

            await asyncio.sleep(self.runtime_tree_watch_interval)

    def _ensure_python_wasi(self) -> PythonWasiInstall:
        """
        Ensure `self.root` is the trusted extracted CPython WASI cache.

        After setup, self.root should directly contain things like:

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

        def onerror(func: Callable[..., Any], p: str, _):
            failed_path = Path(p)
            try:
                self._make_one_writable(failed_path)
                func(failed_path)
            except Exception:
                raise
        
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
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
