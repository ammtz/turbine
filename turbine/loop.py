"""Core run loop: worker proposes; governor forces the exit."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from turbine.governor import ACCEPT, CONTINUE, DONE, HALT, PARK, Governor
from turbine.ledger import Ledger
from turbine.trail import trim_trail
from turbine.types import Handoff, HistoryEntry

Scorer = Callable[[Any], tuple[float, str]]


class Worker(Protocol):
    def step(self, handoff: Handoff, trail: list[str]) -> tuple[Any, int, str]:
        ...


WorkerFactory = Callable[[], Worker]

_MAX_STEPS = 10_000


@dataclass
class Result:
    exit: str
    best_score: float
    best_state: Any
    rounds: int
    attempts: int
    tokens: int
    seconds: float
    history: list[HistoryEntry] = field(default_factory=list)
    held_out_score: float | None = None


def run(
    task: Any,
    scorer: Scorer,
    worker_factory: WorkerFactory,
    initial_state: Any,
    governor: Governor,
    ledger: Ledger,
    context_level: float,
    max_attempts: int,
) -> Result:
    """Run attempts until the governor returns a terminal verdict."""
    _ = task  # reserved for later milestones
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    t0 = time.monotonic()
    best_state: Any = initial_state
    best_score = float("-inf")
    last_feedback = ""
    score_history: list[float] = []
    full_history: list[HistoryEntry] = []
    total_tokens = 0
    total_rounds = 0
    attempts_used = 0

    for attempt_i in range(min(max_attempts, _MAX_STEPS)):
        attempts_used = attempt_i + 1
        worker = worker_factory()
        trail: list[str] = []
        attempt_history: list[HistoryEntry] = []

        for _step in range(_MAX_STEPS):
            reserved = ledger.reserve(ledger.max_step_tokens)
            if reserved is None:
                verdict = governor.decide(attempt_history, ledger)
                if verdict == CONTINUE:
                    verdict = HALT if best_score < governor.bar else ACCEPT
                return _result(
                    verdict, best_score, best_state, total_rounds, attempts_used,
                    total_tokens, t0, full_history,
                )

            handoff = Handoff(
                best_state=best_state,
                score_history=list(score_history),
                last_feedback=last_feedback,
            )
            visible = trim_trail(trail, context_level)
            state, tokens, note = worker.step(handoff, visible)
            ledger.settle(
                reserved,
                tokens,
                source="worker",
                output=tokens,
                note=note,
            )
            total_tokens += tokens

            score, feedback = scorer(state)
            # Always refresh feedback (closes v0 gap: blind on non-improving steps).
            last_feedback = feedback
            entry = HistoryEntry(score=score)
            attempt_history.append(entry)
            full_history.append(entry)
            score_history.append(score)
            trail.append(note)
            total_rounds += 1

            if score > best_score:
                best_score = score
                best_state = state

            verdict = governor.decide(attempt_history, ledger)
            if verdict == CONTINUE:
                continue
            if verdict == PARK and attempt_i + 1 < max_attempts:
                break  # fresh eyes on the next attempt
            return _result(
                verdict, best_score, best_state, total_rounds, attempts_used,
                total_tokens, t0, full_history,
            )
        else:
            # Bound exhausted without a terminal verdict — halt conservatively.
            return _result(
                HALT, best_score, best_state, total_rounds, attempts_used,
                total_tokens, t0, full_history,
            )

    return _result(
        PARK, best_score, best_state, total_rounds, attempts_used,
        total_tokens, t0, full_history,
    )


def _result(
    exit_code: str,
    best_score: float,
    best_state: Any,
    rounds: int,
    attempts: int,
    tokens: int,
    t0: float,
    history: list[HistoryEntry],
) -> Result:
    if best_score == float("-inf"):
        best_score = 0.0
    if exit_code == CONTINUE:
        exit_code = HALT
    return Result(
        exit=exit_code,
        best_score=best_score,
        best_state=best_state,
        rounds=rounds,
        attempts=attempts,
        tokens=tokens,
        seconds=time.monotonic() - t0,
        history=list(history),
        held_out_score=None,
    )
