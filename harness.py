import os
import signal
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CMD = [sys.executable, "main.py"]
SLEEP_SECONDS = 10

child: subprocess.Popen | None = None
app_commit: str | None = None


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=SCRIPT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def current_commit() -> str | None:
    result = run_git("rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def start_app() -> None:
    global child, app_commit

    app_commit = current_commit()

    print(f"Starting: {' '.join(CMD)}", flush=True)
    child = subprocess.Popen(
        CMD,
        cwd=SCRIPT_DIR,
    )


def stop_app() -> None:
    global child

    if child is None:
        return

    if child.poll() is None:
        print("Stopping app...", flush=True)
        child.send_signal(signal.SIGINT)

        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()

            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    child = None


def restart_app() -> None:
    print("Commit changed. Restarting app...", flush=True)
    stop_app()
    start_app()


def shutdown(signum: int, frame) -> None:
    stop_app()
    raise SystemExit(0)


def pull_latest() -> bool:
    result = run_git("pull", "--ff-only")

    if result.returncode != 0:
        print("git pull failed; keeping current app running.", flush=True)

        stderr = result.stderr.strip()
        if stderr:
            print(stderr, flush=True)

        return False

    return True


def main() -> None:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    os.chdir(SCRIPT_DIR)

    pull_latest()
    start_app()

    while True:
        pull_latest()

        commit = current_commit()
        if commit != app_commit:
            restart_app()

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main()
