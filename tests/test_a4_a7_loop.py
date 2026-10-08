"""A4–A7: run loop, trail trimming, park handoff, exit set."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from turbine.governor import ACCEPT, DONE, HALT, PARK, make_governor
from turbine.ledger import MemoryLedger
from turbine.loop import run
from turbine.trail import trim_trail
from turbine.types import Handoff


def _ledger(test: unittest.TestCase, **kw: Any) -> MemoryLedger:
    """Per-test spend path so the cwd default JSONL is not shared."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = str(Path(tmp.name) / "spend.jsonl")
    return MemoryLedger(max_tokens=1_000_000, max_step_tokens=10, path=path, **kw)


class DoneForeverWorker:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.steps = 0

    def step(self, handoff: Handoff, trail: list[str]) -> tuple[str, int, str]:
        self.steps += 1
        self.notes.append("I am done")
        return ("frozen-state", 1, "I am done")


class RecordingWorker:
    instances: list[RecordingWorker] = []

    def __init__(self) -> None:
        self.handoffs: list[Handoff] = []
        self.trails: list[list[str]] = []
        self._n = 0
        RecordingWorker.instances.append(self)

    def step(self, handoff: Handoff, trail: list[str]) -> tuple[str, int, str]:
        self.handoffs.append(
            Handoff(
                best_state=handoff.best_state,
                score_history=list(handoff.score_history),
                last_feedback=handoff.last_feedback,
            )
        )
        self.trails.append(list(trail))
        self._n += 1
        return (f"state-{self._n}", 1, f"note-{self._n}")


def const_scorer(score: float, feedback: str = "fb"):
    def scorer(state: Any) -> tuple[float, str]:
        return (score, f"{feedback}:{state}")

    return scorer


def climbing_scorer(values: list[float]):
    it = iter(values)

    def scorer(state: Any) -> tuple[float, str]:
        return (next(it), "ok")

    return scorer


def base_gov(**kw):
    cfg = dict(ideal=0.99, bar=0.80, eps=0.01, window=3, max_rounds=20)
    cfg.update(kw)
    return make_governor(**cfg)


class TestA4GovernorEndsRun(unittest.TestCase):
    def test_done_note_never_exits_early(self) -> None:
        workers: list[DoneForeverWorker] = []

        def factory() -> DoneForeverWorker:
            w = DoneForeverWorker()
            workers.append(w)
            return w

        # Flat 0.50 < bar=0.80, window=3 → stall→park per attempt; max_attempts=2 → PARK.
        result = run(
            task="synthetic-task",
            scorer=const_scorer(0.50, "flat"),
            worker_factory=factory,
            initial_state="start",
            governor=base_gov(ideal=0.99, bar=0.80, eps=0.01, window=3, max_rounds=20),
            ledger=_ledger(self),
            context_level=1.0,
            max_attempts=2,
        )
        self.assertEqual(result.exit, PARK)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(workers), 2)
        self.assertEqual(workers[0].steps, 4)
        self.assertEqual(workers[1].steps, 4)
        notes = [n for w in workers for n in w.notes]
        self.assertEqual(len(notes), 8)
        for i in range(len(notes)):
            self.assertEqual(notes[i], "I am done")


class TestA5ContextLevelTrail(unittest.TestCase):
    def tearDown(self) -> None:
        RecordingWorker.instances.clear()

    def _run(self, level: float, scores: list[float], **gkw):
        RecordingWorker.instances.clear()
        return run(
            task="synthetic-task",
            scorer=climbing_scorer(scores),
            worker_factory=RecordingWorker,
            initial_state="start",
            governor=base_gov(**gkw),
            ledger=_ledger(self),
            context_level=level,
            max_attempts=1,
        )

    def test_level_zero_empty_trail_every_step(self) -> None:
        self._run(0.0, [0.10, 0.20, 0.30, 0.40, 0.50], max_rounds=5)
        trails = RecordingWorker.instances[0].trails
        self.assertGreaterEqual(len(trails), 4)
        for i in range(len(trails)):
            self.assertEqual(trails[i], [])

    def test_level_matches_trim_trail_length(self) -> None:
        for level in (0.25, 1.0):
            with self.subTest(level=level):
                self._run(level, [0.10, 0.20, 0.30, 0.40, 0.50], max_rounds=5)
                worker = RecordingWorker.instances[0]
                full: list[str] = []
                for i in range(len(worker.trails)):
                    exp = trim_trail(full, level)
                    self.assertEqual(worker.trails[i], exp)
                    full.append(f"note-{i + 1}")

    def test_one_note_trail_at_quarter(self) -> None:
        self._run(0.25, [0.10, 0.20], window=10, max_rounds=2)
        trails = RecordingWorker.instances[0].trails
        self.assertEqual(len(trails), 2)
        self.assertEqual(trails[0], [])
        self.assertEqual(trails[1], trim_trail(["note-1"], 0.25))
        self.assertEqual(len(trim_trail(["note-1"], 0.25)), 1)


class TestA6ParkFreshEyes(unittest.TestCase):
    def tearDown(self) -> None:
        RecordingWorker.instances.clear()

    def test_after_park_new_worker_gets_fresh_eyes(self) -> None:
        RecordingWorker.instances.clear()
        result = run(
            task="synthetic-task",
            scorer=const_scorer(0.40, "lastfb"),
            worker_factory=RecordingWorker,
            initial_state="seed-state",
            governor=base_gov(bar=0.80),
            ledger=_ledger(self),
            context_level=1.0,
            max_attempts=2,
        )
        self.assertGreaterEqual(len(RecordingWorker.instances), 2)
        first, second = RecordingWorker.instances[0], RecordingWorker.instances[1]
        self.assertGreaterEqual(len(first.trails), 4)
        self.assertEqual(second.trails[0], [])
        hand = second.handoffs[0]
        self.assertEqual(hand.best_state, "state-1")
        self.assertEqual(hand.score_history, [0.40] * len(first.trails))
        self.assertEqual(hand.last_feedback, f"lastfb:state-{len(first.trails)}")
        self.assertEqual(result.exit, PARK)


class TestA7ExitSet(unittest.TestCase):
    ALLOWED = frozenset({DONE, ACCEPT, PARK, HALT})

    def test_exits_only_governor_verdicts(self) -> None:
        cases = [
            (0.95, 0.90, 0.70, False, DONE),
            (0.75, 0.90, 0.70, False, ACCEPT),
            (0.40, 0.90, 0.70, False, PARK),
            (0.40, 0.90, 0.70, True, HALT),
        ]
        for i in range(len(cases)):
            score, ideal, bar, exhausted, want = cases[i]
            with self.subTest(i=i, want=want):
                ledger = _ledger(self)
                if exhausted:
                    ledger.mark_exhausted()
                result = run(
                    task="synthetic-task",
                    scorer=const_scorer(score),
                    worker_factory=DoneForeverWorker,
                    initial_state="start",
                    governor=make_governor(
                        ideal=ideal, bar=bar, eps=0.01, window=3, max_rounds=5
                    ),
                    ledger=ledger,
                    context_level=0.0,
                    max_attempts=1,
                )
                self.assertEqual(result.exit, want)
                self.assertIn(result.exit, self.ALLOWED)


if __name__ == "__main__":
    unittest.main()
