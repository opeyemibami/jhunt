# 0001: One `jhunt` namespace package

Status: accepted (owner, 2026-10-29)

## Context

The design puts `platform/`, `workflows/` and `api/` at the repo root. A top-level Python package named `platform` collides with the stdlib `platform` module, which httpx, LangGraph and many other libraries import. Depending on `sys.path` order, either those libraries get our package and break, or our package is never importable.

## Decision

All Python code lives under one package, `jhunt`:

- `jhunt.platform` (the execution layer)
- `jhunt.workflows` (one subpackage per workflow)
- `jhunt.app` (the composition root: wires workflows into the runtime; added in week 1)
- `jhunt.api` (FastAPI)

Non-Python directories (`web/`, `evals/`, `infra/`, `docs/`, `config/`, `migrations/`, `tests/`) stay at the root.

The package lives at `jhunt/` in the repo root (not under `src/`) and is built with `uv_build`. `uv sync` installs it in editable mode, so `jhunt` imports from any working directory without path tweaks.

## Consequences

- Entry points are console scripts: `jhunt-worker` (`jhunt.app.worker:main`; it moved from `jhunt.platform.runtime` in week 1, because the worker must load workflow graphs and the platform may not import workflows) and `jhunt-api` (`jhunt.api.main:main`). Containers run these instead of depending on the working directory.
- Import boundaries are three import-linter contracts in `pyproject.toml`, checked directly and through intermediate modules:
  - forbidden: `jhunt.platform` may not import `jhunt.workflows` or `jhunt.api`
  - layers: `jhunt.app` above `jhunt.api` above `jhunt.workflows` above `jhunt.platform`; a lower layer may not import a higher one
  - independence: the seven `jhunt.workflows.*` packages may not import each other
- import-linter only sees Python, so it does not check `web/`. That boundary holds anyway, because Python cannot import TypeScript.
- `tests/test_packages_import.py` checks that the stdlib `platform` module is still the one Python imports.
- `tests/test_entry_points.py` checks that both console scripts are installed.
