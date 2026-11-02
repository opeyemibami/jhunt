"""`jhunt-up`, `jhunt-down`, `jhunt-logs`: run everything locally (ADR 0017).

jhunt-up starts the database, migrates it, then starts jhunt-worker and
jhunt-api in the background, each in its own session (a Ctrl-C in this
terminal doesn't reach them), with its PID in <state>/<service>.pid and its
log in <state>/logs/<service>.log. jhunt-down stops them; jhunt-logs follows
their logs. Settings come from the environment, which the services inherit:
uv run --env-file .env jhunt-up.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, NoReturn

import httpx

from jhunt.platform.config import LocalSettings, get_local_settings

# The services jhunt-up starts, by name, and the command each runs.
SERVICES = {"worker": "jhunt-worker", "web": "jhunt-api"}
READY_SECONDS = 60
STOP_SECONDS = 60  # the worker finishes the run it's on before it stops


def up_main() -> None:
    parser = argparse.ArgumentParser(
        prog="jhunt-up", description="Start the database, worker and web app."
    )
    parser.parse_args()
    settings = get_local_settings()
    state, logs = _folders(settings)

    running = {name: pid for name, pid in _running(state).items()}
    if running:
        listed = ", ".join(f"{n} (pid {p})" for n, p in running.items())
        _fail(f"already running: {listed}; jhunt-down first")
    for pid in _other_workers(state):
        print(f"warning: another jhunt-worker is running (pid {pid}); it will claim runs too")

    if not Path("docker-compose.yml").is_file():
        _fail("run jhunt-up from the repository (docker-compose.yml not found)")
    # --no-recreate: an existing database container is used as it is. Without
    # it, compose recreates the container whenever its environment differs
    # (say POSTGRES_DB), dropping every connection to the database (PR U1).
    _step(
        "database",
        ["docker", "compose", "up", "-d", "--wait", "--no-recreate", "db"],
        logs / "db.log",
    )
    _step("migrations", [_bin("alembic"), "upgrade", "head"], logs / "migrate.log")

    started: dict[str, int] = {}
    for name, cmd in SERVICES.items():
        log = logs / f"{name}.log"
        offset = log.stat().st_size if log.exists() else 0
        with log.open("ab") as out:
            proc = subprocess.Popen(
                [_bin(cmd)],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (state / f"{name}.pid").write_text(str(proc.pid))
        started[name] = proc.pid
        if not _ready(name, proc, log, offset, settings.jhunt_api_port):
            _stop_all(state, force=True, quiet=True)
            sys.stderr.write(_tail(log, 20))
            _fail(f"{cmd} didn't start; its log is above and in {log}")

    print(
        f"started: worker (pid {started['worker']}), web (pid {started['web']})"
        f" on http://127.0.0.1:{settings.jhunt_api_port}"
    )
    print(f"logs: {logs} (jhunt-logs to follow; jhunt-down to stop)")


def down_main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-down", description="Stop the worker and web app.")
    parser.add_argument("--force", action="store_true", help="kill what doesn't stop in time")
    parser.add_argument("--db", action="store_true", help="stop the database too")
    args = parser.parse_args()
    state, logs = _folders(get_local_settings())
    if not _stop_all(state, force=args.force, quiet=False):
        print("nothing running")
    if args.db:
        _step("database", ["docker", "compose", "stop", "db"], logs / "db.log")
        print("stopped: database")


def logs_main() -> None:
    parser = argparse.ArgumentParser(prog="jhunt-logs", description="Follow every service's log.")
    parser.add_argument("--lines", type=int, default=20, help="lines to show from each first")
    parser.add_argument("--no-follow", action="store_true", help="print and exit")
    args = parser.parse_args()
    _, logs = _folders(get_local_settings())
    width = max(len(name) for name in SERVICES)
    files: dict[str, IO[str]] = {}
    for name in SERVICES:
        path = logs / f"{name}.log"
        if not path.exists():
            continue
        handle = path.open(encoding="utf-8", errors="replace")
        for line in handle.readlines()[-args.lines :]:
            print(f"{name:<{width}} | {line}", end="")
        files[name] = handle
    if args.no_follow:
        return
    try:
        while True:
            for name, followed in files.items():
                for line in followed.readlines():
                    print(f"{name:<{width}} | {line}", end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _folders(settings: LocalSettings) -> tuple[Path, Path]:
    state = settings.jhunt_state_dir
    logs = state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return state, logs


def _bin(command: str) -> str:
    """A command from this environment, next to the running Python."""
    return str(Path(sys.executable).parent / command)


def _step(what: str, cmd: list[str], log: Path) -> None:
    with log.open("ab") as out:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    if done.returncode != 0:
        sys.stderr.write(_tail(log, 20))
        _fail(f"{what} failed; its log is above and in {log}")


def _ready(name: str, proc: subprocess.Popen[bytes], log: Path, offset: int, port: int) -> bool:
    """Whether the service came up: the worker says so in its log; the web
    app answers on its port."""
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if name == "worker":
            with log.open("rb") as f:
                f.seek(offset)
                if b"jhunt-worker started" in f.read():
                    return True
        else:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/", timeout=2).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
        time.sleep(0.3)
    return False


def _running(state: Path) -> dict[str, int]:
    """Each service whose PID file names a live jhunt process. Stale files,
    and files naming some other process, are removed."""
    found = {}
    for name, cmd in SERVICES.items():
        pid_file = state / f"{name}.pid"
        if not pid_file.exists():
            continue
        pid = int(pid_file.read_text())
        if _is_jhunt(pid, cmd):
            found[name] = pid
        else:
            pid_file.unlink()
    return found


def _stop_all(state: Path, *, force: bool, quiet: bool) -> bool:
    """Stop every service jhunt-up started. Returns whether there was any."""
    any_file = False
    for name, cmd in SERVICES.items():
        pid_file = state / f"{name}.pid"
        if not pid_file.exists():
            continue
        any_file = True
        pid = int(pid_file.read_text())
        if not _alive(pid):
            pid_file.unlink()
            continue
        if not _is_jhunt(pid, cmd):
            # The PID was reused: never signal a process jhunt didn't start.
            if not quiet:
                print(f"{name}: pid {pid} is not a jhunt process; left alone")
            pid_file.unlink()
            continue
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_SECONDS
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _alive(pid):
            if not force:
                print(f"{name}: pid {pid} is still stopping; jhunt-down --force kills it")
                continue
            os.kill(pid, signal.SIGKILL)
        pid_file.unlink()
        if not quiet:
            print(f"stopped: {name} (pid {pid})")
    return any_file


def _alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return "\nState:\tZ" not in status  # a zombie has already exited


def _is_jhunt(pid: int, command: str) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return _alive(pid) and any(Path(part.decode(errors="replace")).name == command
                               for part in cmdline if part)  # fmt: skip


def _other_workers(state: Path) -> list[int]:
    ours = set(_running(state).values())
    others = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) in ours or int(proc.name) == os.getpid():
            continue
        if _is_jhunt(int(proc.name), SERVICES["worker"]):
            others.append(int(proc.name))
    return others


def _tail(log: Path, lines: int) -> str:
    if not log.exists():
        return ""
    return "".join(log.read_text(errors="replace").splitlines(keepends=True)[-lines:])


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"jhunt-up: {message}")
