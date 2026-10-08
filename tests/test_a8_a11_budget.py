"""A8–A11: thread-safe Ledger, time pause, JSONL + monitor."""

from __future__ import annotations

import json
import tempfile
import threading
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
    """Each step costs exactly `cost` charged tokens."""

    def __init__(self, cost: int = 1_000) -> None:
        self.cost = cost

    def step(self, handoff: Any, trail: list[str]) -> tuple[str, int, str]:
        return ("state", self.cost, "step")


class OverrunWorker:
    def step(self, handoff: Any, trail: list[str]) -> tuple[str, int, str]:
        return ("state", 2_000, "overrun")


def _flat_run(ledger: Ledger, worker_factory, **gkw) -> Any:
    cfg = dict(ideal=0.99, bar=0.90, eps=0.001, window=10_000, max_rounds=10_000)
    cfg.update(gkw)
    return run(
        task="synthetic-task",
        scorer=lambda s: (0.5, "ok"),
        worker_factory=worker_factory,
        initial_state="start",
        governor=make_governor(**cfg),
        ledger=ledger,
        context_level=0.0,
        max_attempts=1,
    )


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= 100_000:
                break
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _charged_of(row: dict) -> int:
    return int(row["input"]) + int(row["output"]) + int(row["cache_write"])


class TestA8SharedLedgerThreads(unittest.TestCase):
    def test_eight_threads_respect_token_cap(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spend.jsonl"
        max_tokens = 5_000
        step = 1_000
        ledger = Ledger(
            max_tokens=max_tokens,
            max_step_tokens=step,
            path=str(path),
            max_seconds=None,
        )
        results: list[Any] = []
        lock = threading.Lock()

        def target() -> None:
            r = _flat_run(ledger, lambda: TokenWorker(step))
            with lock:
                results.append(r)

        threads = [threading.Thread(target=target) for _ in range(8)]
        for t in threads:
            t.start()
        for i in range(len(threads)):
            threads[i].join(timeout=5.0)
            self.assertFalse(threads[i].is_alive(), f"thread {i} still alive")

        total_rounds = sum(r.rounds for r in results)
        self.assertEqual(ledger.charged, total_rounds * step)
        self.assertEqual(ledger.charged, max_tokens)
        self.assertLessEqual(ledger.charged, max_tokens)
        rows = _read_jsonl(path)
        self.assertEqual(len(rows), total_rounds)
        self.assertEqual(sum(_charged_of(r) for r in rows), ledger.charged)
        for i in range(len(rows)):
            self.assertEqual(_charged_of(rows[i]), step)


class TestA9OverrunExhausts(unittest.TestCase):
    def test_overrun_exhausts_and_stops_within_one_round(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spend.jsonl"
        ledger = Ledger(
            max_tokens=100_000,
            max_step_tokens=1_000,
            path=str(path),
            max_seconds=None,
        )
        results: list[Any] = []
        lock = threading.Lock()

        def target() -> None:
            r = _flat_run(ledger, OverrunWorker)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=target) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(len(threads)):
            threads[i].join(timeout=5.0)
            self.assertFalse(threads[i].is_alive(), f"thread {i} still alive")

        self.assertTrue(ledger.exhausted)
        self.assertGreaterEqual(ledger.overruns, 1)
        self.assertEqual(len(results), 4)
        for i in range(len(results)):
            self.assertLessEqual(results[i].rounds, 1)
            self.assertEqual(results[i].exit, HALT)


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
        clock.advance(100.0)  # must not count
        ledger.resume()
        self.assertFalse(ledger.exhausted)
        self.assertIsNotNone(ledger.reserve(10))
        ledger.settle(10, 10)
        clock.advance(9.0)  # active ~9s (+tiny), still under 10
        self.assertIsNotNone(ledger.reserve(10))
        ledger.settle(10, 10)
        clock.advance(2.0)  # active >= 10 → exhausted on next reserve
        self.assertIsNone(ledger.reserve(10))
        self.assertTrue(ledger.exhausted)


class TestA11JsonlAndMonitor(unittest.TestCase):
    def test_jsonl_sums_and_cache_reads_not_charged(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "spend.jsonl"
        ledger = Ledger(
            max_tokens=1_000,
            max_step_tokens=100,
            path=str(path),
            max_seconds=None,
        )
        self.assertIsNotNone(ledger.reserve(100))
        ledger.settle(
            100,
            35,
            source="synthetic",
            input=10,
            output=20,
            cache_write=5,
            cache_read=50,
            note="with-cache",
            time=0.01,
        )
        self.assertIsNotNone(ledger.reserve(100))
        ledger.settle(
            100,
            40,
            source="synthetic",
            input=15,
            output=25,
            cache_write=0,
            cache_read=10,
            note="more-cache",
            time=0.02,
        )
        rows = _read_jsonl(path)
        self.assertEqual(len(rows), 2)
        charged_sum = sum(_charged_of(r) for r in rows)
        cache_sum = sum(int(r["cache_read"]) for r in rows)
        self.assertEqual(charged_sum, 75)
        self.assertEqual(ledger.charged, 75)
        self.assertEqual(cache_sum, 60)
        self.assertNotEqual(ledger.charged, charged_sum + cache_sum)

        report = monitor.format_report(ledger)
        self.assertIn("75", report)
        self.assertIn("60", report)
        self.assertIn("cache_read", report.lower().replace(" ", "_"))
        self.assertNotIn("WARN", report)
        self.assertNotIn("HALT", report)

        ledger_warn = Ledger(
            max_tokens=100,
            max_step_tokens=80,
            path=str(Path(tmp.name) / "spend2.jsonl"),
        )
        self.assertIsNotNone(ledger_warn.reserve(80))
        ledger_warn.settle(
            80, 80, input=40, output=40, cache_write=0, cache_read=99
        )
        warn_report = monitor.format_report(ledger_warn)
        self.assertIn("WARN", warn_report)
        self.assertEqual(ledger_warn.charged, 80)
        self.assertEqual(ledger_warn.cache_read_total, 99)

        ledger_halt = Ledger(
            max_tokens=50,
            max_step_tokens=50,
            path=str(Path(tmp.name) / "spend3.jsonl"),
        )
        self.assertIsNotNone(ledger_halt.reserve(50))
        ledger_halt.settle(
            50, 50, input=25, output=25, cache_write=0, cache_read=7
        )
        halt_report = monitor.format_report(ledger_halt)
        self.assertIn("HALT", halt_report)
        self.assertIn("7", halt_report)
        self.assertEqual(ledger_halt.charged, 50)


if __name__ == "__main__":
    unittest.main()
