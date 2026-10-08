"""A1–A3: governor truth table, stall window, exit precedence."""

from __future__ import annotations

import unittest

from turbine.governor import ACCEPT, CONTINUE, DONE, HALT, PARK, make_governor
from turbine.types import HistoryEntry, LedgerView


def H(*scores: float) -> list[HistoryEntry]:
    return [HistoryEntry(score=s) for s in scores]


def gov(**kw):
    base = dict(ideal=0.90, bar=0.70, eps=0.01, window=3, max_rounds=100)
    base.update(kw)
    return make_governor(**base)


class TestA1TruthTable(unittest.TestCase):
    def setUp(self) -> None:
        self.g = gov()
        self.ok = LedgerView(exhausted=False)
        self.out = LedgerView(exhausted=True)

    def test_done_at_ideal_boundary(self) -> None:
        self.assertEqual(self.g.decide(H(0.90), self.ok), DONE)
        self.assertEqual(self.g.decide(H(0.91), self.ok), DONE)
        self.assertNotEqual(self.g.decide(H(0.8999), self.ok), DONE)

    def test_accept_when_stalled_at_or_above_bar(self) -> None:
        self.assertEqual(self.g.decide(H(0.70, 0.70, 0.70, 0.70), self.ok), ACCEPT)
        self.assertEqual(self.g.decide(H(0.71, 0.71, 0.71, 0.71), self.ok), ACCEPT)

    def test_park_when_stalled_below_bar(self) -> None:
        self.assertEqual(self.g.decide(H(0.6999, 0.6999, 0.6999, 0.6999), self.ok), PARK)
        self.assertEqual(self.g.decide(H(0.50, 0.50, 0.50, 0.50), self.ok), PARK)

    def test_budget_exhausted_halt_or_accept(self) -> None:
        self.assertEqual(self.g.decide(H(0.69), self.out), HALT)
        self.assertEqual(self.g.decide(H(0.0), self.out), HALT)
        self.assertEqual(self.g.decide(H(0.70), self.out), ACCEPT)
        self.assertEqual(self.g.decide(H(0.85), self.out), ACCEPT)

    def test_exhausted_beats_stall_below_bar(self) -> None:
        # Budget precedence over stall/max_rounds (SPEC §4): both true, best < bar → HALT.
        stalled_below = H(0.40, 0.40, 0.40, 0.40)
        self.assertEqual(self.g.decide(stalled_below, self.ok), PARK)
        self.assertEqual(self.g.decide(stalled_below, self.out), HALT)

    def test_continue_and_eps_boundary(self) -> None:
        self.assertEqual(self.g.decide(H(0.50, 0.50, 0.50, 0.52), self.ok), CONTINUE)
        self.assertEqual(self.g.decide(H(0.50, 0.50, 0.50, 0.51), self.ok), CONTINUE)
        self.assertEqual(self.g.decide(H(0.50, 0.50, 0.50, 0.509), self.ok), PARK)

    def test_max_rounds_like_stall(self) -> None:
        g = gov(window=50, max_rounds=4)
        self.assertEqual(g.decide(H(0.10, 0.20, 0.30, 0.40), self.ok), PARK)
        self.assertEqual(g.decide(H(0.10, 0.40, 0.60, 0.75), self.ok), ACCEPT)


class TestA2StallWindow(unittest.TestCase):
    def test_exactly_window_is_continue(self) -> None:
        g, ok = gov(), LedgerView(exhausted=False)
        self.assertEqual(g.decide(H(0.40, 0.40, 0.40), ok), CONTINUE)
        self.assertEqual(g.decide(H(0.40, 0.40, 0.40, 0.40), ok), PARK)


class TestA3Precedence(unittest.TestCase):
    def test_done_beats_exhausted(self) -> None:
        g, out = gov(), LedgerView(exhausted=True)
        self.assertEqual(g.decide(H(0.95), out), DONE)
        self.assertNotEqual(g.decide(H(0.95), out), HALT)
        self.assertNotEqual(g.decide(H(0.95), out), ACCEPT)


if __name__ == "__main__":
    unittest.main()
