"""A8–A11: thread-safe Ledger, time pause, JSONL + monitor."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from turbine.governor import HALT, make_governor
from turbine.ledger import Ledger
from turbine.loop import run
from turbine import monitor


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TokenWorker:
    """Costs `cost` tokens; brief pause so many reservations overlap (A8)."""

    def __init__(self, cost: int = 1_000, pause_s: float = 0.005) -> None:
        self.cost = cost
        self.pause_s = pause_s

    def step(self, handoff: Any, trail: list[str]) -> tuple[str, int, str]:
        time.sleep(self.pause_s)
        return ("state", self.cost, "step")


def _flat_run(ledger: Ledger, factory) -> Any:
    return run(
        task="synthetic-task",
        scorer=lambda s: (0.5, "ok"),
        worker_factory=factory,
        initial_state="start",
        governor=make_governor(
            ideal=0.99, bar=0.90, eps=0.001, window=10_000, max_rounds=10_000
        ),
        ledger=ledger,
        context_level=0.0,
        max_attempts=1,
    )


def _rows(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= 100_000:
                break
            if line.strip():
                out.append(json.loads(line))
    return out


def _charged(row: dict) -> int:
    return int(row["input"]) + int(row["output"]) + int(row["cache_write"])


class TestA8SharedLedgerThreads(unittest.TestCase):
    def test_eight_threads_respect_token_cap(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spend.jsonl"
        max_tokens, step = 5_000, 1_000
        ledger = Ledger(
            max_tokens=max_tokens, max_step_tokens=step, path=str(path)
        )
        results: list[Any] = []
        lock = threading.Lock()

        def target() -> None:
            r = _flat_run(ledger, lambda: TokenWorker(step, pause_s=0.005))
            with lock:
                results.append(r)

        threads = [threading.Thread(target=target) for _ in range(8)]
        for t in threads:
            t.start()
        for i in range(8):
            threads[i].join(timeout=10.0)
            self.assertFalse(threads[i].is_alive(), f"thread {i} alive")

        rounds = sum(r.rounds for r in results)
        self.assertEqual(ledger.charged, rounds * step)
        self.assertEqual(ledger.charged, max_tokens)
        self.assertLessEqual(ledger.charged, max_tokens)
        rows = _rows(path)
        self.assertEqual(len(rows), rounds)
        self.assertEqual(sum(_charged(r) for r in rows), ledger.charged)
        for i in range(len(rows)):
            self.assertEqual(_charged(rows[i]), step)


class TestA9OverrunExhausts(unittest.TestCase):
    def test_one_overrun_stops_every_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spend.jsonl"
        overrun_settled = threading.Event()
        barrier = threading.Barrier(4, timeout=5.0)

        class SignalingLedger(Ledger):
            def settle(self, reserved: int, actual: int, **kwargs: Any) -> None:
                super().settle(reserved, actual, **kwargs)
                if self.overruns >= 1:
                    overrun_settled.set()

        ledger = SignalingLedger(
            max_tokens=100_000,
            max_step_tokens=1_000,
            path=str(path),
        )

        class SyncOverrunWorker:
            def step(self, handoff: Any, trail: list[str]) -> tuple[str, int, str]:
                barrier.wait()
                return ("state", 2_000, "overrun")

        class SyncTokenWorker:
            def step(self, handoff: Any, trail: list[str]) -> tuple[str, int, str]:
                barrier.wait()
                if not overrun_settled.wait(timeout=5.0):
                    raise TimeoutError("overrun settle event timed out")
                return ("state", 1_000, "step")

        overrun_result: list[Any] = []
        token_results: list[Any] = []
        lock = threading.Lock()

        def overrun_target() -> None:
            r = _flat_run(ledger, SyncOverrunWorker)
            with lock:
                overrun_result.append(r)

        def token_target() -> None:
            r = _flat_run(ledger, SyncTokenWorker)
            with lock:
                token_results.append(r)

        threads = [threading.Thread(target=overrun_target)]
        threads.extend(threading.Thread(target=token_target) for _ in range(3))
        for t in threads:
            t.start()
        for i in range(len(threads)):
            threads[i].join(timeout=10.0)
            self.assertFalse(threads[i].is_alive(), f"thread {i} alive")

        self.assertTrue(overrun_settled.is_set())
        self.assertTrue(ledger.exhausted)
        self.assertEqual(ledger.overruns, 1)
        self.assertEqual(len(overrun_result), 1)
        self.assertEqual(len(token_results), 3)
        self.assertEqual(overrun_result[0].exit, HALT)
        self.assertEqual(overrun_result[0].rounds, 1)
        for i in range(3):
            # At most one more round after the flip (held reserve completes).
            self.assertLessEqual(token_results[i].rounds, 1)
            self.assertEqual(token_results[i].exit, HALT)


class TestA10PauseResumeTime(unittest.TestCase):
    def test_pause_excluded_from_time_cap(self) -> None:
        clock = FakeClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(
            max_tokens=1_000_000,
            max_step_tokens=10,
            max_seconds=10.0,
            path=str(Path(tmp.name) / "spend.jsonl"),
            clock=clock,
        )
        self.assertIsNotNone(ledger.reserve(10))
        ledger.settle(10, 10)
        ledger.pause()
        clock.advance(100.0)
        ledger.resume()
        self.assertFalse(ledger.exhausted)
        self.assertIsNotNone(ledger.reserve(10))
        ledger.settle(10, 10)
        clock.advance(9.0)
        self.assertIsNotNone(ledger.reserve(10))
        ledger.settle(10, 10)
        clock.advance(2.0)
        self.assertIsNone(ledger.reserve(10))
        self.assertTrue(ledger.exhausted)


class TestA11JsonlAndMonitor(unittest.TestCase):
    def test_jsonl_sums_and_exact_monitor_flags(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spend.jsonl"
        ledger = Ledger(max_tokens=1_000, max_step_tokens=100, path=str(path))
        self.assertIsNotNone(ledger.reserve(100))
        ledger.settle(
            100, 35, source="synthetic", input=10, output=20,
            cache_write=5, cache_read=50, note="with-cache", time=0.01,
        )
        self.assertIsNotNone(ledger.reserve(100))
        ledger.settle(
            100, 40, source="synthetic", input=15, output=25,
            cache_write=0, cache_read=10, note="more-cache", time=0.02,
        )
        rows = _rows(path)
        self.assertEqual(sum(_charged(r) for r in rows), 75)
        self.assertEqual(ledger.charged, 75)
        self.assertEqual(sum(int(r["cache_read"]) for r in rows), 60)
        self.assertEqual(ledger.cache_read_total, 60)
        self.assertEqual(
            monitor.format_report(ledger),
            "spend 75/1000 (7.5%) cache_read 60",
        )

        # 79% is not WARN.
        p79 = Path(tmp.name) / "p79.jsonl"
        mid = Ledger(max_tokens=100, max_step_tokens=79, path=str(p79))
        self.assertIsNotNone(mid.reserve(79))
        mid.settle(79, 79, input=39, output=40, cache_write=0, cache_read=3)
        self.assertEqual(
            monitor.format_report(mid),
            "spend 79/100 (79.0%) cache_read 3",
        )

        # Exact 80% → WARN.
        p80 = Path(tmp.name) / "p80.jsonl"
        warn = Ledger(max_tokens=100, max_step_tokens=80, path=str(p80))
        self.assertIsNotNone(warn.reserve(80))
        warn.settle(80, 80, input=40, output=40, cache_write=0, cache_read=99)
        self.assertEqual(
            monitor.format_report(warn),
            "spend 80/100 (80.0%) cache_read 99 WARN",
        )

        # 100% → HALT.
        p100 = Path(tmp.name) / "p100.jsonl"
        halt = Ledger(max_tokens=50, max_step_tokens=50, path=str(p100))
        self.assertIsNotNone(halt.reserve(50))
        halt.settle(50, 50, input=25, output=25, cache_write=0, cache_read=7)
        self.assertEqual(
            monitor.format_report(halt),
            "spend 50/50 (100.0%) cache_read 7 HALT",
        )
        self.assertEqual(halt.charged, 50)


if __name__ == "__main__":
    unittest.main()
