"""Offline unit tests for the closed-loop routing (src/spec_loop.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.spec_loop import (  # noqa: E402
    ACCEPT, RECLARIFY, REGENERATE,
    LoopBudget, build_regeneration_diagnostic, route,
)


class TestRoute(unittest.TestCase):

    def test_blocking_routes_to_regenerate(self):
        v = {"issues": [{"code": "CONFIRMED_VALUE_MISSING",
                         "severity": "blocking"}],
             "assumed_to_reclarify": []}
        self.assertEqual(route(v), REGENERATE)

    def test_unsupported_literal_routes_to_regenerate(self):
        # V2 is a warning, but the paper routes it to grounded re-generation
        # (the user's info is complete; re-asking returns the same answer).
        v = {"issues": [{"code": "UNSUPPORTED_LITERAL", "severity": "warning"}],
             "assumed_to_reclarify": []}
        self.assertEqual(route(v), REGENERATE)

    def test_assumed_without_blocking_routes_to_reclarify(self):
        v = {"issues": [], "assumed_to_reclarify": [{"feature_id": "boss_1"}]}
        self.assertEqual(route(v), RECLARIFY)

    def test_blocking_takes_priority_over_assumed(self):
        v = {"issues": [{"code": "B_REP_DIM_MISMATCH", "severity": "blocking"}],
             "assumed_to_reclarify": [{"feature_id": "boss_1"}]}
        self.assertEqual(route(v), REGENERATE)

    def test_clean_accepts(self):
        self.assertEqual(route({"issues": [], "assumed_to_reclarify": []}),
                         ACCEPT)


class TestRegenerationDiagnostic(unittest.TestCase):

    def test_names_missing_field_with_value(self):
        v = {"issues": [{"code": "CONFIRMED_VALUE_MISSING",
                         "severity": "blocking",
                         "feature_id": "hole_1", "param": "radius",
                         "expected_mm": 5.0}]}
        diag = build_regeneration_diagnostic(v)
        self.assertIn("hole_1.radius", diag)
        self.assertIn("5.0 mm", diag)

    def test_unsupported_literal_gets_diagnostic(self):
        diag = build_regeneration_diagnostic(
            {"issues": [{"code": "UNSUPPORTED_LITERAL", "severity": "warning",
                         "literal_mm": 3.0, "call": ".fillet()"}]})
        self.assertIn("3.0", diag)

    def test_empty_when_no_regenerate_issue(self):
        self.assertEqual(
            build_regeneration_diagnostic(
                {"issues": [{"code": "B_REP_UNAVAILABLE",
                             "severity": "info"}]}),
            "")


class TestLoopBudget(unittest.TestCase):

    def test_spend_until_exhausted(self):
        b = LoopBudget(max_rounds=2)
        self.assertTrue(b.can_act("a"))
        self.assertEqual(b.remaining("a"), 2)
        b.record("a")
        b.record("a")
        self.assertFalse(b.can_act("a"))
        self.assertEqual(b.remaining("a"), 0)

    def test_exceeding_raises(self):
        b = LoopBudget(max_rounds=0)
        with self.assertRaises(RuntimeError):
            b.record("a")

    def test_budget_is_per_sample(self):
        b = LoopBudget(max_rounds=1)
        b.record("a")
        self.assertFalse(b.can_act("a"))
        self.assertTrue(b.can_act("b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
