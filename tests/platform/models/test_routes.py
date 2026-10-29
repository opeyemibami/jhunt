"""Routes load from JHUNT_CONFIG_DIR/model_routes.toml and fail fast when wrong."""

from pathlib import Path

import pytest

from jhunt.platform.models.routes import Route, load_routes
from jhunt.platform.models.types import ModelConfigError

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


def write_routes(tmp_path: Path, text: str) -> Path:
    (tmp_path / "model_routes.toml").write_text(text)
    return tmp_path


def test_repo_routes_are_valid_and_default_to_haiku() -> None:
    routes = load_routes(REPO_CONFIG, None)
    assert routes.for_step("default") == Route("anthropic", "claude-haiku-5-5")


def test_step_route_wins_and_unknown_steps_use_default(tmp_path: Path) -> None:
    config = write_routes(
        tmp_path,
        '[routes]\ndefault = "anthropic:model-a"\n"scout.score" = "anthropic:model-b"\n',
    )
    routes = load_routes(config, None)
    assert routes.for_step("scout.score") == Route("anthropic", "model-b")
    assert routes.for_step("scout.rank") == Route("anthropic", "model-a")


def test_override_replaces_single_keys(tmp_path: Path) -> None:
    config = write_routes(tmp_path, '[routes]\ndefault = "anthropic:model-a"\n')
    routes = load_routes(config, '{"prep.brief": "anthropic:model-c"}')
    assert routes.for_step("prep.brief") == Route("anthropic", "model-c")
    assert routes.for_step("default") == Route("anthropic", "model-a")


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ('[routes]\n"scout.score" = "anthropic:m"\n', "no 'default' route"),
        ('[routes]\ndefault = "openrouter:m"\n', "unknown provider 'openrouter'"),
        ('[routes]\ndefault = "anthropic"\n', "expected 'provider:model'"),
        ('[routes]\ndefault = "anthropic:"\n', "expected 'provider:model'"),
        ('[routes]\ndefault = "anthropic:has space"\n', "expected 'provider:model'"),
        ("[routes]\ndefault = 3\n", "must be a 'provider:model' string"),
        ('[routes]\ndefault = "anthropic:m"\n"Scout" = "anthropic:m"\n', "step must look like"),
        ('[routes]\ndefault = "anthropic:m"\n[budgets]\n', "exactly one table"),
        ('default = "anthropic:m"\n', "exactly one table"),
        ("[routes\n", "invalid TOML"),
    ],
)
def test_bad_routes_file_fails(tmp_path: Path, toml: str, error: str) -> None:
    with pytest.raises(ModelConfigError, match=error):
        load_routes(write_routes(tmp_path, toml), None)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ("{not json", "MODEL_ROUTES is not valid JSON"),
        ('["anthropic:m"]', "MODEL_ROUTES must be a JSON object"),
        ('{"default": "nope:m"}', "MODEL_ROUTES: route 'default': unknown provider"),
    ],
)
def test_bad_override_fails(tmp_path: Path, override: str, error: str) -> None:
    config = write_routes(tmp_path, '[routes]\ndefault = "anthropic:m"\n')
    with pytest.raises(ModelConfigError, match=error):
        load_routes(config, override)


def test_missing_config_dir_fails(tmp_path: Path) -> None:
    with pytest.raises(ModelConfigError, match=r"JHUNT_CONFIG_DIR=.* is not a directory"):
        load_routes(tmp_path / "nope", None)


def test_missing_routes_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ModelConfigError, match="routes file missing"):
        load_routes(tmp_path, None)
