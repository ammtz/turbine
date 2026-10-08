"""M1 in-memory ledger stand-in. Full thread-safe Ledger is M2."""

from __future__ import annotations


class MemoryLedger:
    """Budget view with reserve/settle so the governor can see exhausted."""

    def __init__(self, *, max_tokens: int, max_step_tokens: int) -> None:
        self.max_tokens = max_tokens
        self.max_step_tokens = max_step_tokens
        self.charged = 0
        self.exhausted = False

    def mark_exhausted(self) -> None:
        self.exhausted = True

    def reserve(self, max_step_tokens: int) -> int | None:
        """Return reserved amount, or None if the call must not be made."""
        if self.exhausted:
            return None
        remaining = self.max_tokens - self.charged
        if remaining < max_step_tokens:
            self.exhausted = True
            return None
        return max_step_tokens

    def settle(self, reserved: int, actual: int) -> None:
        self.charged += actual
        if actual > reserved or self.charged >= self.max_tokens:
            self.exhausted = True
