"""The repo layout from AGENTS.md exists and every package imports."""

import importlib

import pytest

PACKAGES = [
    "jhunt",
    "jhunt.app",
    "jhunt.jobs",
    "jhunt.platform",
    "jhunt.platform.runtime",
    "jhunt.platform.timers",
    "jhunt.platform.gateway",
    "jhunt.platform.memory",
    "jhunt.platform.models",
    "jhunt.platform.artifacts",
    "jhunt.platform.events",
    "jhunt.platform.observability",
    "jhunt.workflows",
    "jhunt.workflows.scout",
    "jhunt.workflows.capture",
    "jhunt.workflows.application_pack",
    "jhunt.workflows.workspace",
    "jhunt.workflows.tracker",
    "jhunt.workflows.prep",
    "jhunt.workflows.funnel",
    "jhunt.api",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    importlib.import_module(name)


def test_stdlib_platform_is_not_shadowed() -> None:
    # The reason for the jhunt namespace (ADR 0001): our package must not
    # replace the stdlib `platform` module that httpx and others rely on.
    import platform

    assert callable(platform.system)
