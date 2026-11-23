"""Offline tests for the probe's LLM-audit row alignment (regression)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe_spec_fidelity as psf  # noqa: E402


class TestAttachLlmAudits(unittest.TestCase):

    def test_aligns_when_leading_samples_skipped(self):
        # results[0] has no standardized prompt (skipped) -> only results[1]
        # produces a row. The audit must come from results[1], not results[0].
        rows = [{"uid": "kept"}]
        result_indices = [1]
        llm_audits = {
            0: {"channel": "llm", "wrong": True},
            1: {"channel": "llm", "correct": True},
        }
        psf.attach_llm_audits(rows, result_indices, llm_audits)
        self.assertIn("correct", rows[0]["audit_llm"])
        self.assertNotIn("wrong", rows[0]["audit_llm"])

    def test_missing_audit_left_absent(self):
        rows = [{"uid": "a"}]
        result_indices = [2]
        psf.attach_llm_audits(rows, result_indices, {2: None})
        self.assertNotIn("audit_llm", rows[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
