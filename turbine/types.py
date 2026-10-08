"""Shared M1 types for governor, ledger view, and worker handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HistoryEntry:
    score: float


@dataclass
class LedgerView:
    """Minimal exhausted flag the governor can read (M1 stand-in)."""

    exhausted: bool = False


@dataclass(frozen=True)
class Handoff:
    best_state: Any
    score_history: list[float] = field(default_factory=list)
    last_feedback: str = ""
