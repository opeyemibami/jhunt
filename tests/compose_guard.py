"""The compose guard: no test touches the developer's compose project.

check(argv, env) raises RuntimeError for a command that runs docker compose,
or jhunt-up or jhunt-down (which run it), unless the project is a test's own:
-p/--project-name or COMPOSE_PROJECT_NAME starting "jhunt-test-". It runs
before the process starts (tests/conftest.py wraps subprocess.Popen with it),
so a refused command never runs.
"""

import shlex
from collections.abc import Mapping, Sequence
from os import PathLike
from pathlib import Path

PREFIX = "jhunt-test-"
# Commands that run docker compose themselves.
RUN_COMPOSE = {"jhunt-up", "jhunt-down"}


def check(args: str | bytes | PathLike[str] | Sequence[object], env: Mapping[str, str]) -> None:
    argv = _argv(args)
    if not argv:
        return
    command = Path(argv[0]).name
    compose = (command == "docker" and "compose" in argv[1:]) or command == "docker-compose"
    if not compose and command not in RUN_COMPOSE:
        return
    project = _project(argv) or env.get("COMPOSE_PROJECT_NAME", "")
    if not project.startswith(PREFIX):
        raise RuntimeError(
            f"a test ran {' '.join(argv)!r} without its own compose project: use"
            f" -p {PREFIX}<id> or COMPOSE_PROJECT_NAME={PREFIX}<id>, never the dev"
            " project (got " + (repr(project) if project else "none") + ")"
        )


def _argv(args: str | bytes | PathLike[str] | Sequence[object]) -> list[str]:
    if isinstance(args, bytes):
        return shlex.split(args.decode())
    if isinstance(args, str):
        return shlex.split(args)
    if isinstance(args, PathLike):
        return [str(args)]
    return [str(a) for a in args]


def _project(argv: list[str]) -> str:
    for n, arg in enumerate(argv):
        if arg in ("-p", "--project-name") and n + 1 < len(argv):
            return argv[n + 1]
        if arg.startswith("--project-name="):
            return arg.split("=", 1)[1]
    return ""
