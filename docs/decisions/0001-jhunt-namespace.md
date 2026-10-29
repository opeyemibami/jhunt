# 0001: One `jhunt` namespace package

Status: accepted (owner, 2026-10-29)

## Context

The design puts `platform/`, `workflows/` and `api/` at the repo root. A top-level Python package named `platform` collides with the stdlib `platform` module, which httpx, LangGraph and many other libraries import. Depending on `sys.path` order, either those libraries get our package and break, or our package is never importable.

## Decision

All Python code lives under one package, `jhunt`:

- `jhunt.platform` (the execution layer)
- `jhunt.workflows` (one subpackage per workflow)
- `jhunt.api` (FastAPI)

Non-Python directories (`web/`, `evals/`, `infra/`, `docs/`, `config/`, `migrations/`, `tests/`) stay at the root.

The project is not installed as a wheel (`[tool.uv] package = false`). Code runs from the repo root, where `jhunt` is importable as a directory.

## Consequences

- The import boundary is an import-linter `forbidden` contract: `jhunt.platform` may not import `jhunt.workflows` or `jhunt.api`, directly or indirectly.
- import-linter only sees Python, so it does not check `web/`. That boundary holds anyway, because Python cannot import TypeScript.
- `tests/test_packages_import.py` checks that the stdlib `platform` module is still the one Python imports.
