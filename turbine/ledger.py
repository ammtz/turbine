"""Thread-safe process Ledger: reserve/settle, time cap, JSONL spend."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable


Clock = Callable[[], float]

DEFAULT_SPEND_PATH = "turbine-spend.jsonl"


class Ledger:
    """One shared budget object for every run in a process."""

    def __init__(
        self,
        *,
        max_tokens: int,
        max_step_tokens: int,
        path: str | None = None,
        max_seconds: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.max_step_tokens = max_step_tokens
        self.max_seconds = max_seconds
        self._clock: Clock = clock if clock is not None else time.monotonic
        self._lock = threading.RLock()
        self.charged = 0
        self.cache_read_total = 0
        self.overruns = 0
        self.exhausted = False
        self._held = 0
        self._paused = False
        self._pause_started: float | None = None
        self._paused_total = 0.0
        self._started = self._clock()
        self.path = str(path if path is not None else DEFAULT_SPEND_PATH)
        self.budget_path = str(Path(self.path).with_suffix(".budget.json"))
        self._write_budget()

    def mark_exhausted(self) -> None:
        with self._lock:
            self.exhausted = True

    def pause(self) -> None:
        with self._lock:
            if not self._paused:
                self._paused = True
                self._pause_started = self._clock()

    def resume(self) -> None:
        with self._lock:
            if self._paused and self._pause_started is not None:
                self._paused_total += self._clock() - self._pause_started
                self._pause_started = None
                self._paused = False

    def active_seconds(self) -> float:
        with self._lock:
            now = self._clock()
            paused = self._paused_total
            if self._paused and self._pause_started is not None:
                paused += now - self._pause_started
            return max(0.0, now - self._started - paused)

    def reserve(self, max_step_tokens: int) -> int | None:
        with self._lock:
            if self.exhausted or self._time_exceeded():
                self.exhausted = True
                return None
            remaining = self.max_tokens - self.charged - self._held
            if remaining < max_step_tokens:
                self.exhausted = True
                return None
            self._held += max_step_tokens
            return max_step_tokens

    def settle(
        self,
        reserved: int,
        actual: int,
        *,
        source: str = "worker",
        input: int | None = None,
        output: int | None = None,
        cache_write: int = 0,
        cache_read: int = 0,
        note: str = "",
        time: float = 0.0,
    ) -> None:
        with self._lock:
            if reserved > self._held:
                reserved = self._held
            self._held -= reserved
            if input is None and output is None:
                in_tok, out_tok, cw = 0, actual, 0
                charged_delta = actual
            else:
                in_tok = 0 if input is None else input
                out_tok = 0 if output is None else output
                cw = cache_write
                charged_delta = in_tok + out_tok + cw
            self.charged += charged_delta
            self.cache_read_total += cache_read
            if actual > reserved or charged_delta > reserved:
                self.overruns += 1
                self.exhausted = True
            if self.charged >= self.max_tokens or self._time_exceeded():
                self.exhausted = True
            self._append_row(
                {
                    "source": source,
                    "input": in_tok,
                    "output": out_tok,
                    "cache_write": cw,
                    "cache_read": cache_read,
                    "note": note,
                    "time": time,
                }
            )
            self._write_budget()

    def _time_exceeded(self) -> bool:
        if self.max_seconds is None:
            return False
        return self.active_seconds() >= self.max_seconds

    def _append_row(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def _write_budget(self) -> None:
        payload = {
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "charged": self.charged,
            "cache_read_total": self.cache_read_total,
            "spend_path": self.path,
            "exhausted": self.exhausted,
            "overruns": self.overruns,
        }
        with open(self.budget_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)


# M1 name kept as a compatible alias.
MemoryLedger = Ledger
