"""jhunt-up, jhunt-down and jhunt-logs, against real processes (ADR 0017).

Each test has its own compose project (jhunt-test-<id>), so its own Postgres
container on a free port, removed with its volume afterwards; its own web
port; and its own state folder. jhunt-up starts that database, migrates it,
then starts a real jhunt-worker and jhunt-api. Never the developer's compose
project, database or port: the compose guard (tests/compose_guard.py)
refuses anything else.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

TIMEOUT = 120


def command(name: str) -> str:
    path = shutil.which(name)
    assert path is not None, f"{name} is not installed; run `uv sync`"
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def env(gcs_test_bucket: str, tmp_path: Path) -> Iterator[dict[str, str]]:
    test_env = os.environ | {
        # A compose project of the test's own: its own database container.
        "COMPOSE_PROJECT_NAME": f"jhunt-test-{uuid.uuid4().hex[:8]}",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(free_port()),
        "POSTGRES_DB": "jhunt_test",
        "GCS_RECORDS_BUCKET": gcs_test_bucket,
        "GCS_STAGING_BUCKET": gcs_test_bucket,
        "JHUNT_API_PORT": str(free_port()),
        "JHUNT_STATE_DIR": str(tmp_path / "state"),
    }
    yield test_env
    # The test's container and its volume go; nothing else is touched.
    subprocess.run(
        ["docker", "compose", "down", "-v", "--remove-orphans"],
        env=test_env, capture_output=True, timeout=TIMEOUT, check=False,
    )  # fmt: skip


def run(name: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [command(name), *args], env=env, capture_output=True, text=True, timeout=TIMEOUT
    )


def pids(env: dict[str, str]) -> dict[str, int]:
    folder = Path(env["JHUNT_STATE_DIR"])
    return {p.stem: int(p.read_text()) for p in folder.glob("*.pid")}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return (
        Path(f"/proc/{pid}").exists()
        and "zombie" not in Path(f"/proc/{pid}/status").read_text().lower()
    )


@pytest.fixture
def up(env: dict[str, str]) -> Iterator[dict[str, int]]:
    result = run("jhunt-up", env)
    assert result.returncode == 0, result.stdout + result.stderr
    started = pids(env)
    yield started
    for pid in started.values():  # whatever a failed test left running
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_up_starts_the_worker_and_the_web_app_with_logs_in_one_place(
    env: dict[str, str], up: dict[str, int]
) -> None:
    assert set(up) == {"worker", "web"} and all(alive(p) for p in up.values())
    page = httpx.get(f"http://127.0.0.1:{env['JHUNT_API_PORT']}/", timeout=10)
    assert page.status_code == 200 and "Scout digest" in page.text
    logs = Path(env["JHUNT_STATE_DIR"]) / "logs"
    assert "jhunt-worker started" in (logs / "worker.log").read_text()
    assert (logs / "web.log").exists() and (logs / "migrate.log").exists()

    shown = run("jhunt-logs", env, "--no-follow")
    assert shown.returncode == 0
    assert any(line.startswith("worker | ") for line in shown.stdout.splitlines())
    assert any(line.startswith("web    | ") for line in shown.stdout.splitlines())


def database_container(env: dict[str, str]) -> str:
    found = subprocess.run(
        ["docker", "compose", "ps", "-q", "db"], env=env, capture_output=True, text=True,
        check=True,
    )  # fmt: skip
    return found.stdout.strip()


def test_up_uses_the_running_database_as_it_is(env: dict[str, str]) -> None:
    # The test's own database container, already running, then started again
    # with a different environment: jhunt-up must use it as it is, not
    # recreate it, which drops every connection (it once did, to the
    # developer's database, before --no-recreate).
    started = subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "db"], env=env, capture_output=True,
        timeout=TIMEOUT, check=True,
    )  # fmt: skip
    assert started.returncode == 0
    before = database_container(env)
    # A database the app can use, under a name the container wasn't created
    # with: compose sees a different environment.
    subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "createdb", "-U", env["POSTGRES_USER"],
         "jhunt_test_other"],
        env=env, capture_output=True, timeout=TIMEOUT, check=True,
    )  # fmt: skip
    env = env | {"POSTGRES_DB": "jhunt_test_other"}
    result = run("jhunt-up", env)
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert database_container(env) == before
    finally:
        run("jhunt-down", env)


def test_up_refuses_to_start_a_second_copy(env: dict[str, str], up: dict[str, int]) -> None:
    again = run("jhunt-up", env)
    assert again.returncode == 1
    assert "already running" in again.stderr
    assert pids(env) == up  # nothing new started


def test_down_stops_them_and_leaves_nothing_behind(env: dict[str, str], up: dict[str, int]) -> None:
    result = run("jhunt-down", env)
    assert result.returncode == 0, result.stdout + result.stderr
    deadline = time.monotonic() + 10
    while any(alive(p) for p in up.values()) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not any(alive(p) for p in up.values())
    assert pids(env) == {}
    assert "nothing running" in run("jhunt-down", env).stdout


def test_down_never_signals_a_process_that_isnt_jhunt(env: dict[str, str]) -> None:
    # A PID file whose process has gone and whose number now belongs to
    # something else: jhunt-down must leave it alone.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        state = Path(env["JHUNT_STATE_DIR"])
        state.mkdir(parents=True)
        (state / "worker.pid").write_text(str(other.pid))
        result = run("jhunt-down", env)
        assert "not a jhunt process" in result.stdout
        assert other.poll() is None  # still running
        assert pids(env) == {}  # the stale file is gone
    finally:
        other.kill()
        other.wait()


def test_a_stale_pid_file_doesnt_stop_jhunt_up(env: dict[str, str]) -> None:
    state = Path(env["JHUNT_STATE_DIR"])
    state.mkdir(parents=True)
    (state / "web.pid").write_text("999999")  # no such process
    result = run("jhunt-up", env)
    try:
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        run("jhunt-down", env)


def test_a_service_that_doesnt_start_stops_the_rest_and_shows_its_log(
    env: dict[str, str],
) -> None:
    # Something else holds the web app's port, so jhunt-api can't bind it.
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", int(env["JHUNT_API_PORT"])))
        taken.listen()
        result = run("jhunt-up", env)
    assert result.returncode == 1
    assert "jhunt-api didn't start" in result.stderr
    assert "address already in use" in result.stderr.lower()  # the end of its log, shown
    worker = Path(env["JHUNT_STATE_DIR"]) / "worker.pid"
    assert not worker.exists()  # the worker it had started was stopped again
