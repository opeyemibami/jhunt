"""jhunt-records-delete can never target a whole bucket, and says how much it will delete.

No GCS here: the owner client and the listing are replaced, and a refused
prefix must be refused before either is touched.
"""

import sys
from typing import Any

import pytest

from jhunt.app import records_delete
from jhunt.platform.artifacts import curator
from jhunt.platform.artifacts.curator import Generation


class Untouchable:
    """A stand-in client that fails the test if anything uses it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the client was used ({name}) for a refused prefix")


def run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["jhunt-records-delete", *argv])
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    records_delete.get_gcp_settings.cache_clear()
    monkeypatch.setattr(records_delete, "owner_client", lambda project: Untouchable())
    try:
        records_delete.main()
    finally:
        records_delete.get_gcp_settings.cache_clear()


@pytest.mark.parametrize("prefix", ["", "/", "//", "snapshots", "snapshots/1"])
def test_command_refuses_a_prefix_that_could_reach_beyond_one_folder(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    def no_listing(*args: Any) -> list[Generation]:
        raise AssertionError("listed generations for a refused prefix")

    monkeypatch.setattr(records_delete, "list_generations", no_listing)
    with pytest.raises(SystemExit, match="prefix"):
        run(monkeypatch, "jhunt-ci-test", prefix)


@pytest.mark.parametrize("prefix", ["", "/", "//", "snapshots"])
def test_curator_refuses_the_same_prefixes(prefix: str) -> None:
    # Defence in depth: the functions refuse too, not only the command.
    with pytest.raises(ValueError, match="prefix"):
        curator.delete_all_generations("jhunt-ci-test", prefix, Untouchable())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="prefix"):
        curator.list_generations("jhunt-ci-test", prefix, Untouchable())  # type: ignore[arg-type]


def test_confirmation_shows_how_many_generations_it_will_delete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = [
        Generation("tests/x/a.html", 1, True),
        Generation("tests/x/a.html", 2, False),
        Generation("tests/x/b.txt", 3, True),
    ]
    monkeypatch.setattr(records_delete, "list_generations", lambda *a: found)

    def must_not_delete(*args: Any) -> list[Generation]:
        raise AssertionError("deleted without confirmation")

    monkeypatch.setattr(records_delete, "delete_all_generations", must_not_delete)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "not the prefix"

    monkeypatch.setattr("builtins.input", answer)

    with pytest.raises(SystemExit, match="not confirmed; nothing deleted"):
        run(monkeypatch, "jhunt-ci-test", "tests/x/")

    assert prompts == [
        "Delete 3 generations under gs://jhunt-ci-test/tests/x/? Type the prefix to confirm: "
    ]
    assert "gs://jhunt-ci-test/tests/x/a.html#2 (noncurrent)" in capsys.readouterr().out


def test_records_delete_needs_only_gcp_project(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A storage-only command must not need the database variables.
    for name in [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "JHUNT_CONFIG_DIR",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(records_delete, "list_generations", lambda *a: [])
    run(monkeypatch, "jhunt-ci-test", "tests/x/")
    assert "nothing under gs://jhunt-ci-test/tests/x/" in capsys.readouterr().out
