"""A1–A3: governor truth table, stall window, exit precedence."""

from __future__ import annotations

import unittest

from turbine.governor import (
    ACCEPT,
    CONTINUE,
    DONE,
    HALT,
    PARK,
    make_governor,
)
from turbine.types import HistoryEntry, LedgerView


def hist(*scores: float) -> list[HistoryEntry]:
    return [HistoryEntry(score=s) for s in scores]


class TestA1TruthTable(unittest.TestCase):
    def setUp(self) -> None:
        self.gov = make_governor(
            ideal=0.90,
            bar=0.70,
            eps=0.01,
            window=3,
            max_rounds=100,
        )
        self.ok = LedgerView(exhausted=False)
        self.broke = LedgerView(exhausted=True)

    def test_done_at_ideal_boundary(self) -> None:
        self.assertEqual(self.gov.decide(hist(0.90), self.ok), DONE)
        self.assertEqual(self.gov.decide(hist(0.91), self.ok), DONE)
        self.assertNotEqual(self.gov.decide(hist(0.8999), self.ok), DONE)

    def test_accept_when_stalled_at_bar_boundary(self) -> None:
        # len > window, gain < eps, best == bar → accept
        stalled_at_bar = hist(0.70, 0.70, 0.70, 0.70)
        self.assertEqual(self.gov.decide(stalled_at_bar, self.ok), ACCEPT)
        stalled_above_bar = hist(0.71, 0.71, 0.71, 0.71)
        self.assertEqual(self.gov.decide(stalled_above_bar, self.ok), ACCEPT)

    def test_park_when_stalled_below_bar(self) -> None:
        stalled_below = hist(0.6999, 0.6999, 0.6999, 0.6999)
        self.assertEqual(self.gov.decide(stalled_below, self.ok), PARK)
        stalled_low = hist(0.50, 0.50, 0.50, 0.50)
        self.assertEqual(self.gov.decide(stalled_low, self.ok), PARK)

    def test_halt_when_exhausted_below_bar(self) -> None:
        self.assertEqual(self.gov.decide(hist(0.69), self.broke), HALT)
        self.assertEqual(self.gov.decide(hist(0.0), self.broke), HALT)

    def test_accept_when_exhausted_at_bar_boundary(self) -> None:
        self.assertEqual(self.gov.decide(hist(0.70), self.broke), ACCEPT)
        self.assertEqual(self.gov.decide(hist(0.85), self.broke), ACCEPT)

    def test_continue_when_improving_and_budget_ok(self) -> None:
        # gain over last window == 0.02 >= eps → continue
        improving = hist(0.50, 0.50, 0.50, 0.52)
        self.assertEqual(self.gov.decide(improving, self.ok), CONTINUE)

    def test_eps_boundary_exact_eps_is_not_stalled(self) -> None:
        # gain == eps is not < eps → continue
        exact_eps = hist(0.50, 0.50, 0.50, 0.51)
        self.assertEqual(self.gov.decide(exact_eps, self.ok), CONTINUE)
        # gain just under eps → stalled (below bar → park)
        under_eps = hist(0.50, 0.50, 0.50, 0.509)
        self.assertEqual(self.gov.decide(under_eps, self.ok), PARK)

    def test_max_rounds_triggers_like_stall(self) -> None:
        gov = make_governor(
            ideal=0.90, bar=0.70, eps=0.01, window=50, max_rounds=4
        )
        # improving each step so not stalled by eps, but hit max_rounds
        h = hist(0.10, 0.20, 0.30, 0.40)
        self.assertEqual(gov.decide(h, self.ok), PARK)  # best < bar
        gov2 = make_governor(
            ideal=0.90, bar=0.70, eps=0.01, window=50, max_rounds=4
        )
        h2 = hist(0.10, 0.40, 0.60, 0.75)
        self.assertEqual(gov2.decide(h2, self.ok), ACCEPT)  # best >= bar


class TestA2StallWindow(unittest.TestCase):
    def test_exactly_window_entries_is_continue(self) -> None:
        gov = make_governor(
            ideal=0.90, bar=0.70, eps=0.01, window=3, max_rounds=100
        )
        ledger = LedgerView(exhausted=False)
        # flat scores — would be stalled if window check applied, but len == window
        exact = hist(0.40, 0.40, 0.40)
        self.assertEqual(len(exact), 3)
        self.assertEqual(gov.decide(exact, ledger), CONTINUE)
        # one more entry → stalled → park
        over = hist(0.40, 0.40, 0.40, 0.40)
        self.assertEqual(gov.decide(over, ledger), PARK)


class TestA3Precedence(unittest.TestCase):
    def test_done_beats_exhausted(self) -> None:
        gov = make_governor(
            ideal=0.90, bar=0.70, eps=0.01, window=3, max_rounds=100
        )
        both = hist(0.95)
        exhausted = LedgerView(exhausted=True)
        self.assertEqual(gov.decide(both, exhausted), DONE)
        # and not halt/accept from budget branch
        self.assertNotEqual(gov.decide(both, exhausted), HALT)
        self.assertNotEqual(gov.decide(both, exhausted), ACCEPT)


if __name__ == "__main__":
    unittest.main()
