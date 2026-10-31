"""A lease-lost handler for in-process tests.

The worker's default handler ends the process (os._exit), which in a test
would kill pytest itself, without a summary or a failure naming the test.
Tests that run the worker inside the pytest process pass this handler
instead: it records the lost lease and stops the worker loop, and the
`lease_watch` fixture fails the test at teardown with the run id and reason.
An exception raised in the heartbeat thread wouldn't do: pytest only warns.
"""

import threading
from dataclasses import dataclass, field

import pytest

from jhunt.platform.runtime.queue import Run


@dataclass
class LeaseWatch:
    lost: list[tuple[int, str]] = field(default_factory=list)
    # Pass this as run_worker's stop event, so a lost lease also ends the loop.
    stop: threading.Event = field(default_factory=threading.Event)

    def __call__(self, run: Run, reason: str) -> None:
        self.lost.append((run.id, reason))
        self.stop.set()

    def check(self) -> None:
        if self.lost:
            details = "; ".join(f"run {run_id}: {reason}" for run_id, reason in self.lost)
            pytest.fail(f"a worker lost its lease inside the test process ({details})")
