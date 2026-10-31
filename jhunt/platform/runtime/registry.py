"""The graph registry: which code runs each (workflow, version).

Workflows register their graph builders in the composition root (jhunt.app).
A registered version is never replaced (AGENTS.md hard rule 12): a changed
graph is registered as a new version, so threads already running on the old
one keep running it. Pinning threads to versions comes in week 2.
"""

from collections.abc import Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

# Takes the checkpointer for this run and returns the compiled graph.
Builder = Callable[[BaseCheckpointSaver], CompiledStateGraph]  # type: ignore[type-arg]


class Registry:
    def __init__(self) -> None:
        self._builders: dict[tuple[str, int], Builder] = {}

    def register(self, workflow: str, version: int, builder: Builder) -> None:
        key = (workflow, version)
        if key in self._builders:
            raise ValueError(
                f"{workflow!r} version {version} is already registered; "
                "register a new version instead of changing this one"
            )
        self._builders[key] = builder

    def get(self, workflow: str, version: int) -> Builder | None:
        return self._builders.get((workflow, version))
