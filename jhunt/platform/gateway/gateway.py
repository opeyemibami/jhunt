"""The tool gateway, v0 (ADR 0008): every side effect goes through call().

For each call it:

1. builds the idempotency key from thread, node and a stable step key
   (AGENTS.md hard rule 3: never a timestamp or a random value);
2. returns the recorded result if that key already completed, so a node that
   re-runs after a crash doesn't act again;
3. otherwise checks the action's level in autonomy_settings (hard rule 10);
4. runs the tool, then records its result in tool_calls, the audit log.

v0 runs level 1 actions only. Approvals (level 2) and notifications come later.
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

# A level-1 action: act internally and reversibly, then notify.
SUPPORTED_LEVEL = 1


class ToolRefused(Exception):
    """The gateway would not run this call: no such tool, or its level forbids it."""


class IdempotencyConflict(Exception):
    """A completed call has this key but different arguments: a caller bug.

    The same step must always make the same call; otherwise the recorded
    result would answer a question nobody asked.
    """


@dataclass(frozen=True)
class Tool:
    name: str
    action: str  # the autonomy_settings action whose level governs this tool
    run: Callable[[dict[str, Any]], dict[str, Any]]


def idempotency_key(thread_id: str, node: str, step_key: str) -> str:
    """sha256 of [thread_id, node, step_key], encoded as a JSON list so that
    no two different triples can produce the same text."""
    if not (thread_id and node and step_key):
        raise ValueError("thread_id, node and step_key must all be non-empty")
    return hashlib.sha256(json.dumps([thread_id, node, step_key]).encode()).hexdigest()


def _args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()


class ToolGateway:
    def __init__(self, engine: Engine, tools: Iterable[Tool]) -> None:
        self._engine = engine
        # A "not allowed" action has no tool at all (AGENTS.md), so it can
        # never be called: it simply isn't in this mapping.
        self._tools = {tool.name: tool for tool in tools}

    def call(
        self,
        thread_id: str,
        node: str,
        step_key: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        run_id: int | None,
    ) -> dict[str, Any]:
        key = idempotency_key(thread_id, node, step_key)
        args_hash = _args_hash(args)

        recorded = self._recorded(key, args_hash)
        if recorded is not None:
            return recorded

        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolRefused(f"no tool {tool_name!r}")
        level = self._level(tool.action)
        if level is None:
            raise ToolRefused(f"no autonomy setting for {tool.action!r}")
        if level != SUPPORTED_LEVEL:
            raise ToolRefused(
                f"{tool.action!r} is level {level}; gateway v0 runs level {SUPPORTED_LEVEL} only"
            )

        # If the tool raises, nothing is recorded and the error propagates: the
        # next attempt runs it again. Tools must be safe to repeat (ADR 0008).
        result = tool.run(args)

        with self._engine.begin() as conn:
            # Two workers can run the same step at once (ADR 0005, known limit).
            # Both run the tool; the first insert wins, the second does nothing,
            # and both return the winner's recorded result. Neither raises.
            conn.execute(
                text(
                    "INSERT INTO tool_calls (idempotency_key, thread_id, node, step_key, run_id,"
                    " tool, args_hash, level, outcome, result)"
                    " VALUES (:key, :thread_id, :node, :step_key, :run_id,"
                    " :tool, :args_hash, :level, 'succeeded', CAST(:result AS jsonb))"
                    " ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {
                    "key": key,
                    "thread_id": thread_id,
                    "node": node,
                    "step_key": step_key,
                    "run_id": run_id,
                    "tool": tool.name,
                    "args_hash": args_hash,
                    "level": level,
                    "result": json.dumps(result),
                },
            )
        winner = self._recorded(key, args_hash)
        if winner is None:  # the row was just inserted, by us or the other worker
            raise RuntimeError(f"tool call {key} was not recorded")
        return winner

    def _recorded(self, key: str, args_hash: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT args_hash, result FROM tool_calls WHERE idempotency_key = :key"),
                {"key": key},
            ).first()
        if row is None:
            return None
        if row.args_hash != args_hash:
            raise IdempotencyConflict(f"tool call {key} was recorded with different arguments")
        result: dict[str, Any] = row.result
        return result

    def _level(self, action: str) -> int | None:
        with self._engine.connect() as conn:
            level: int | None = conn.execute(
                text(
                    "SELECT level FROM autonomy_settings WHERE action = :action"
                    " ORDER BY id DESC LIMIT 1"
                ),
                {"action": action},
            ).scalar_one_or_none()
        return level
