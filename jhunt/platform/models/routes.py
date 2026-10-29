"""Model routes: which provider and model serve each step.

Routes live in <JHUNT_CONFIG_DIR>/model_routes.toml, in git and reviewed like
code. MODEL_ROUTES (a JSON object) overrides single keys for local experiments.
Everything is validated when the gateway starts, so a bad route fails there and
not on the first call that uses it.
"""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jhunt.platform.models.types import ModelConfigError

ROUTES_FILE = "model_routes.toml"
DEFAULT_STEP = "default"
# Providers with an adapter. openrouter comes later, for eval sweeps.
PROVIDERS = frozenset({"anthropic", "openai"})

_STEP = re.compile(r"[a-z_]+(\.[a-z_]+)*")
_MODEL = re.compile(r"[A-Za-z0-9._/-]+")


@dataclass(frozen=True)
class Route:
    provider: str
    model: str


@dataclass(frozen=True)
class Routes:
    by_step: Mapping[str, Route]  # always has DEFAULT_STEP

    def for_step(self, step: str) -> Route:
        return self.by_step.get(step, self.by_step[DEFAULT_STEP])

    def providers(self) -> set[str]:
        return {route.provider for route in self.by_step.values()}


def parse_route(step: object, value: object, source: str) -> tuple[str, Route]:
    where = f"{source}: route {step!r}"
    if not isinstance(step, str) or not _STEP.fullmatch(step):
        raise ModelConfigError(f"{where}: step must look like 'scout.score'")
    if not isinstance(value, str):
        raise ModelConfigError(f"{where}: value must be a 'provider:model' string")
    provider, sep, model = value.partition(":")
    if not sep or not _MODEL.fullmatch(model):
        raise ModelConfigError(f"{where} = {value!r}: expected 'provider:model'")
    if provider not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise ModelConfigError(f"{where}: unknown provider {provider!r} (known: {known})")
    return step, Route(provider, model)


def parse_routes(table: Mapping[object, object], source: str) -> dict[str, Route]:
    return dict(parse_route(step, value, source) for step, value in table.items())


def load_routes(config_dir: Path, override_json: str | None) -> Routes:
    path = config_dir / ROUTES_FILE
    if not config_dir.is_dir():
        raise ModelConfigError(
            f"JHUNT_CONFIG_DIR={config_dir} is not a directory (resolved: {config_dir.resolve()})"
        )
    if not path.is_file():
        raise ModelConfigError(f"routes file missing: {path} (resolved: {path.resolve()})")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ModelConfigError(f"{path}: invalid TOML: {exc}") from None

    # Unknown tables are rejected so a typo can't silently drop a setting.
    if set(data) != {"routes"} or not isinstance(data["routes"], dict):
        raise ModelConfigError(f"{path}: must contain exactly one table, [routes]")
    by_step = parse_routes(data["routes"], str(path))

    if override_json is not None:
        try:
            override = json.loads(override_json)
        except json.JSONDecodeError as exc:
            raise ModelConfigError(f"MODEL_ROUTES is not valid JSON: {exc}") from None
        if not isinstance(override, dict):
            raise ModelConfigError('MODEL_ROUTES must be a JSON object, e.g. {"default": "..."}')
        by_step.update(parse_routes(override, "MODEL_ROUTES"))

    if DEFAULT_STEP not in by_step:
        raise ModelConfigError(f"{path}: no {DEFAULT_STEP!r} route")
    return Routes(by_step)
