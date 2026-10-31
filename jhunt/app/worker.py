"""`jhunt-worker` entry point.

The worker has to load workflow graphs, so it lives here in the composition
root, above jhunt.workflows, and not in jhunt.platform.runtime, which may not
import workflows. The loop itself arrives in jhunt.platform.runtime (PR 1.3);
this module will build the graph registry and hand it over.
"""


def main() -> None:
    raise SystemExit("jhunt-worker: not implemented")
