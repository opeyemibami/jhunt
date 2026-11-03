"""No test can touch the developer's compose project (owner, after PR U1).

Any test that runs docker compose, or jhunt-up or jhunt-down (which run it),
must name its own project: COMPOSE_PROJECT_NAME=jhunt-test-<id>, or -p. The
guard in tests/compose_guard.py is installed around subprocess.Popen for the
whole session (tests/conftest.py) and refuses anything else before it starts.

The rules are tested on argument lists without running them. Whether the
guard is installed is tested only with `docker compose version`, which
changes nothing, so these tests are safe even if the guard were missing.
"""

import os
import subprocess

import pytest

from tests.compose_guard import check

WITHOUT = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROJECT_NAME"}


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "compose", "ps"],
        ["docker", "compose", "-p", "jhunt", "ps"],
        ["docker", "compose", "--project-name", "jhunt", "up", "-d", "db"],
        ["/usr/bin/docker", "compose", "up", "-d", "db"],
        ["docker-compose", "up", "-d", "db"],
        ["jhunt-up"],
        ["/some/venv/bin/jhunt-down", "--db"],
        "docker compose up -d db",  # a shell string
    ],
)
def test_compose_without_a_test_project_is_refused(argv: list[str] | str) -> None:
    with pytest.raises(RuntimeError, match="jhunt-test-"):
        check(argv, WITHOUT)


def test_the_dev_project_by_environment_is_refused_too() -> None:
    with pytest.raises(RuntimeError, match="jhunt-test-"):
        check(["docker", "compose", "ps"], WITHOUT | {"COMPOSE_PROJECT_NAME": "jhunt"})


@pytest.mark.parametrize(
    ("argv", "env"),
    [
        (["docker", "compose", "-p", "jhunt-test-1a2b", "up", "-d", "db"], WITHOUT),
        (["docker", "compose", "--project-name=jhunt-test-1a2b", "ps"], WITHOUT),
        (["jhunt-up"], WITHOUT | {"COMPOSE_PROJECT_NAME": "jhunt-test-1a2b"}),
        (["docker", "ps"], WITHOUT),  # not compose
        (["true"], WITHOUT),
    ],
)
def test_a_test_project_and_other_commands_are_allowed(
    argv: list[str], env: dict[str, str]
) -> None:
    check(argv, env)  # no error


def test_the_guard_is_installed_for_every_test() -> None:
    # `docker compose version` changes nothing, with or without the guard.
    with pytest.raises(RuntimeError, match="jhunt-test-"):
        subprocess.run(["docker", "compose", "version"], env=WITHOUT, capture_output=True)
    allowed = subprocess.run(
        ["docker", "compose", "-p", "jhunt-test-guard", "version"], capture_output=True
    )
    assert allowed.returncode == 0
