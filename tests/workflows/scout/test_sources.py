"""Scout's sources: the Greenhouse and Ashby boards in config/sources.toml."""

from pathlib import Path

import pytest

from jhunt.workflows.scout.sources import Source, SourcesError, load_sources

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


def test_repo_sources_are_the_thirteen_api_boards() -> None:
    sources = load_sources(REPO_CONFIG)
    assert len(sources) == 13
    assert Source("greenhouse", "anthropic", "Anthropic") in sources
    assert Source("ashby", "mistral.ai", "Mistral AI") in sources
    assert {s.provider for s in sources} == {"greenhouse", "ashby"}


def write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "sources.toml").write_text(body)
    return tmp_path


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('[[sources]]\nprovider = "lever"\nboard = "x"\ncompany = "X"\n', "unknown provider"),
        ('[[sources]]\nprovider = "ashby"\nboard = "has space"\ncompany = "X"\n', "board"),
        ('[[sources]]\nprovider = "ashby"\nboard = "x"\ncompany = ""\n', "company"),
        ('[[sources]]\nprovider = "ashby"\nboard = "x"\n', "company"),
        (
            '[[sources]]\nprovider = "ashby"\nboard = "x"\ncompany = "X"\n' * 2,
            "listed twice",
        ),
        ("[other]\n", "exactly one list"),
    ],
)
def test_bad_sources_fail(tmp_path: Path, body: str, error: str) -> None:
    with pytest.raises(SourcesError, match=error):
        load_sources(write(tmp_path, body))


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(SourcesError, match="sources file missing"):
        load_sources(tmp_path)
