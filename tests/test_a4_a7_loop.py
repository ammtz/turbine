"""A4–A7: run loop, trail trimming, park handoff, exit set."""

from __future__ import annotations

import unittest
from typing import Any

from turbine.governor import ACCEPT, DONE, HALT, PARK, make_governor
from turbine.ledger import MemoryLedger
from turbine.loop import run
from turbine.trail import trim_trail
from turbine.types import Handoff


class DoneForeverWorker:
    """Returns the same state and claims to be done every step."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.steps = 0

    def step(self, handoff: Handoff, trail: list[str]) -> tuple[str, int, str]:
        self.steps += 1
        note = "I am done"
        self.notes.append(note)
        return ("frozen-state", 1, note)


class RecordingWorker:
    """Records every handoff and trail; emits a new note each step."""

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


def _const_scorer(score: float, feedback: str = "fb"):
    def scorer(state: Any) -> tuple[float, str]:
        return (score, f"{feedback}:{state}")

    return scorer


class TestA4GovernorEndsRun(unittest.TestCase):
    def test_done_note_never_exits_early(self) -> None:
        workers: list[DoneForeverWorker] = []

        def factory() -> DoneForeverWorker:
            w = DoneForeverWorker()
            workers.append(w)
            return w

        gov = make_governor(
            ideal=0.99, bar=0.80, eps=0.01, window=3, max_rounds=20
        )
        ledger = MemoryLedger(max_tokens=1_000_000, max_step_tokens=10)
        result = run(
            task="synthetic-task",
            scorer=_const_scorer(0.50, "flat"),
            worker_factory=factory,
            initial_state="start",
            governor=gov,
            ledger=ledger,
            context_level=1.0,
            max_attempts=2,
        )
        self.assertIn(result.exit, {PARK, ACCEPT})
        self.assertNotEqual(result.exit, DONE)
        # Worker claimed done every step; governor still drove park/accept.
        total_notes: list[str] = []
        for w in workers:
            total_notes.extend(w.notes)
        self.assertGreater(len(total_notes), 0)
        for n in range(min(len(total_notes), 64)):
            self.assertEqual(total_notes[n], "I am done")
        # Stall needs len(history) > window → at least window+1 steps on attempt 1.
        self.assertGreaterEqual(workers[0].steps, 4)
        # Exit matches governor on a flat history of that length, not worker text.
        from turbine.types import HistoryEntry, LedgerView

        attempt_hist = [HistoryEntry(score=0.50) for _ in range(workers[0].steps)]
        expected = gov.decide(attempt_hist, LedgerView(exhausted=False))
        if result.attempts == 1:
            self.assertEqual(result.exit, expected)
        else:
            self.assertEqual(result.exit, PARK)


class TestA5ContextLevelTrail(unittest.TestCase):
    def tearDown(self) -> None:
        RecordingWorker.instances.clear()

    def test_level_zero_empty_trail_every_step(self) -> None:
        RecordingWorker.instances.clear()
        gov = make_governor(
            ideal=0.99, bar=0.90, eps=0.01, window=3, max_rounds=5
        )
        ledger = MemoryLedger(max_tokens=1_000_000, max_step_tokens=10)
        # Score climbs so we keep continuing until max_rounds.
        scores = iter([0.10, 0.20, 0.30, 0.40, 0.50])

        def scorer(state: Any) -> tuple[float, str]:
            return (next(scores), "ok")

        run(
            task="synthetic-task",
            scorer=scorer,
            worker_factory=RecordingWorker,
            initial_state="start",
            governor=gov,
            ledger=ledger,
            context_level=0.0,
            max_attempts=1,
        )
        worker = RecordingWorker.instances[0]
        self.assertGreaterEqual(len(worker.trails), 4)
        for i in range(len(worker.trails)):
            self.assertEqual(worker.trails[i], [])

    def test_level_matches_trim_trail_length(self) -> None:
        for level in (0.25, 1.0):
            with self.subTest(level=level):
                RecordingWorker.instances.clear()
                gov = make_governor(
                    ideal=0.99, bar=0.90, eps=0.01, window=3, max_rounds=5
                )
                ledger = MemoryLedger(max_tokens=1_000_000, max_step_tokens=10)
                scores = iter([0.10, 0.20, 0.30, 0.40, 0.50])

                def scorer(state: Any) -> tuple[float, str]:
                    return (next(scores), "ok")

                run(
                    task="synthetic-task",
                    scorer=scorer,
                    worker_factory=RecordingWorker,
                    initial_state="start",
                    governor=gov,
                    ledger=ledger,
                    context_level=level,
                    max_attempts=1,
                )
                worker = RecordingWorker.instances[0]
                full: list[str] = []
                for i in range(len(worker.trails)):
                    expected = trim_trail(full, level)
                    self.assertEqual(len(worker.trails[i]), len(expected))
                    self.assertEqual(worker.trails[i], expected)
                    full.append(f"note-{i + 1}")

    def test_one_note_trail_at_quarter(self) -> None:
        RecordingWorker.instances.clear()
        gov = make_governor(
            ideal=0.99, bar=0.90, eps=0.01, window=10, max_rounds=2
        )
        ledger = MemoryLedger(max_tokens=1_000_000, max_step_tokens=10)
        scores = iter([0.10, 0.20])

        def scorer(state: Any) -> tuple[float, str]:
            return (next(scores), "ok")

        run(
            task="synthetic-task",
            scorer=scorer,
            worker_factory=RecordingWorker,
            initial_state="start",
            governor=gov,
            ledger=ledger,
            context_level=0.25,
            max_attempts=1,
        )
        worker = RecordingWorker.instances[0]
        self.assertEqual(len(worker.trails), 2)
        self.assertEqual(worker.trails[0], [])
        one = ["note-1"]
        self.assertEqual(worker.trails[1], trim_trail(one, 0.25))
        self.assertEqual(len(trim_trail(one, 0.25)), 1)


class TestA6ParkFreshEyes(unittest.TestCase):
    def tearDown(self) -> None:
        RecordingWorker.instances.clear()

    def test_after_park_new_worker_gets_fresh_eyes(self) -> None:
        RecordingWorker.instances.clear()
        gov = make_governor(
            ideal=0.99, bar=0.80, eps=0.01, window=3, max_rounds=20
        )
        ledger = MemoryLedger(max_tokens=1_000_000, max_step_tokens=10)
        # Flat below bar → stall → park → second attempt.
        result = run(
            task="synthetic-task",
            scorer=_const_scorer(0.40, "lastfb"),
            worker_factory=RecordingWorker,
            initial_state="seed-state",
            governor=gov,
            ledger=ledger,
            context_level=1.0,
            max_attempts=2,
        )
        self.assertGreaterEqual(len(RecordingWorker.instances), 2)
        first, second = RecordingWorker.instances[0], RecordingWorker.instances[1]
        self.assertGreaterEqual(len(first.trails), 4)
        # Second worker first call: empty trail, best state, full scores, last feedback.
        self.assertEqual(second.trails[0], [])
        hand = second.handoffs[0]
        # Flat scores: best keeps the first improving/first-scored state.
        self.assertEqual(hand.best_state, "state-1")
        self.assertEqual(hand.score_history, [0.40] * len(first.trails))
        self.assertEqual(hand.last_feedback, f"lastfb:state-{len(first.trails)}")
        self.assertIn(result.exit, {PARK, ACCEPT, HALT, DONE})


class TestA7ExitSet(unittest.TestCase):
    ALLOWED = frozenset({DONE, ACCEPT, PARK, HALT})

    def test_exits_only_governor_verdicts(self) -> None:
        cases = [
            # done
            dict(
                score=0.95,
                ideal=0.90,
                bar=0.70,
                max_rounds=5,
                max_attempts=1,
                exhausted=False,
            ),
            # accept via stall >= bar
            dict(
                score=0.75,
                ideal=0.90,
                bar=0.70,
                max_rounds=5,
                max_attempts=1,
                exhausted=False,
            ),
            # park via stall < bar, last attempt
            dict(
                score=0.40,
                ideal=0.90,
                bar=0.70,
                max_rounds=5,
                max_attempts=1,
                exhausted=False,
            ),
            # halt via exhausted < bar before/during
            dict(
                score=0.40,
                ideal=0.90,
                bar=0.70,
                max_rounds=5,
                max_attempts=1,
                exhausted=True,
            ),
        ]
        for i in range(len(cases)):
            c = cases[i]
            with self.subTest(i=i, score=c["score"]):
                gov = make_governor(
                    ideal=c["ideal"],
                    bar=c["bar"],
                    eps=0.01,
                    window=3,
                    max_rounds=c["max_rounds"],
                )
                ledger = MemoryLedger(max_tokens=1_000_000, max_step_tokens=10)
                if c["exhausted"]:
                    ledger.mark_exhausted()

                def factory() -> DoneForeverWorker:
                    return DoneForeverWorker()

                result = run(
                    task="synthetic-task",
                    scorer=_const_scorer(c["score"]),
                    worker_factory=factory,
                    initial_state="start",
                    governor=gov,
                    ledger=ledger,
                    context_level=0.0,
                    max_attempts=c["max_attempts"],
                )
                self.assertIn(result.exit, self.ALLOWED)
                self.assertNotEqual(result.exit, "continue")


if __name__ == "__main__":
    unittest.main()
