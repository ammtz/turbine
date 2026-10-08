"""Pure governor: decide(history, ledger) → verdict."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from turbine.types import HistoryEntry, LedgerView

CONTINUE = "continue"
DONE = "done"
ACCEPT = "accept"
PARK = "park"
HALT = "halt"

_EXITS = frozenset({CONTINUE, DONE, ACCEPT, PARK, HALT})


class _Ledger(Protocol):
    exhausted: bool


@dataclass(frozen=True)
class Governor:
    ideal: float
    bar: float
    eps: float
    window: int
    max_rounds: int

    def decide(
        self, history: Sequence[HistoryEntry], ledger: _Ledger | LedgerView
    ) -> str:
        best = _best(history)
        # Precedence: done > budget > stall/max_rounds.
        if best >= self.ideal:
            return DONE
        if ledger.exhausted:
            return ACCEPT if best >= self.bar else HALT
        if _stalled(history, self.window, self.eps) or len(history) >= self.max_rounds:
            return ACCEPT if best >= self.bar else PARK
        return CONTINUE


def make_governor(
    *,
    ideal: float,
    bar: float,
    eps: float,
    window: int,
    max_rounds: int,
) -> Governor:
    if window < 1 or max_rounds < 1:
        raise ValueError("window and max_rounds must be >= 1")
    return Governor(
        ideal=ideal, bar=bar, eps=eps, window=window, max_rounds=max_rounds
    )


def _best(history: Sequence[HistoryEntry]) -> float:
    if not history:
        return 0.0
    best = history[0].score
    for i in range(1, min(len(history), 10_000)):
        if history[i].score > best:
            best = history[i].score
    return best


def _stalled(history: Sequence[HistoryEntry], window: int, eps: float) -> bool:
    n = len(history)
    if n <= window:
        return False
    # Best gained over the last `window` rounds.
    before_len = n - window
    best_before = _best(history[:before_len])
    best_now = _best(history)
    return (best_now - best_before) < eps
