"""Workload-consistency guardrail: stated RPS vs derived average.

Locks the forensic finding that total daily traffic filed as
messages_per_user_per_day (WRITE-only) and then multiplied again by
read_write_ratio inflates average throughput ~10x. The guardrail is
fail-open when no parseable rate is stated and one-sided (only derived
average materially *above* stated fires; an average below a stated
peak is normal).

Run: poetry run python -m unittest discover tests
"""
import unittest
from unittest.mock import patch

from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.services.capacity.consistency import (
    MATERIAL_MISMATCH_RATIO,
    check_workload_consistency,
    derived_avg_total_rps,
    parse_stated_rps,
)


def _proposal(**overrides):
    base = {
        "daily_active_users": 10000000,
        "messages_per_user_per_day": 8.5,
        "peak_factor": 3.0,
        "read_write_ratio": 9.0,
        "average_message_size_bytes": 150,
        "connection_factor": 0.01,
        "storage_retention_days": 365,
        "rationale": {},
    }
    base.update(overrides)
    return CapacityModelProposal(**base)


def _extraction(traffic):
    from ai_architect.domain.requirements.models import RequirementsExtraction
    return RequirementsExtraction(
        application_description="url shortener",
        cloud_provider="aws",
        traffic_characteristics=traffic,
    )


class ParseStatedRpsTests(unittest.TestCase):
    def test_per_second_forms(self):
        self.assertAlmostEqual(
            parse_stated_rps("around 1,000 requests per second"), 1000.0)
        self.assertAlmostEqual(
            parse_stated_rps("1,000 requests/second"), 1000.0)
        self.assertAlmostEqual(parse_stated_rps("2.5k rps"), 2500.0)
        self.assertAlmostEqual(
            parse_stated_rps("Handle 5M req/s spikes"), 5000000.0)

    def test_per_day_converted(self):
        self.assertAlmostEqual(
            parse_stated_rps("86.4M requests per day"), 1000.0)

    def test_non_rate_text_returns_none(self):
        for text in ("", "high traffic", "3x average load",
                     "p99 latency under 100 ms", "99.99% availability",
                     "100 concurrent connections", "1,000"):
            self.assertIsNone(parse_stated_rps(text), msg=text)


class WorkloadConsistencyTests(unittest.TestCase):
    def test_double_counted_reads_flagged_with_ratio(self):
        extraction = _extraction("around 1,000 requests per second")
        with self.assertRaises(ValueError) as ctx:
            check_workload_consistency(extraction, _proposal())
        message = str(ctx.exception)
        self.assertIn("9.84x", message)
        self.assertIn("WRITE-only", message)
        self.assertIn("messages_per_user_per_day=8.5", message)
        self.assertIn("read_write_ratio=9.0", message)

    def test_corrected_writes_pass_with_expected_average(self):
        extraction = _extraction("around 1,000 requests per second")
        proposal = _proposal(messages_per_user_per_day=0.85)
        self.assertAlmostEqual(
            derived_avg_total_rps(proposal), 983.796, places=2)
        self.assertAlmostEqual(
            check_workload_consistency(extraction, proposal), 1000.0)

    def test_no_stated_rate_never_fires(self):
        self.assertIsNone(check_workload_consistency(_extraction(""), _proposal()))
        self.assertIsNone(
            check_workload_consistency(_extraction("bursty"), _proposal()))

    def test_threshold_boundary(self):
        self.assertEqual(MATERIAL_MISMATCH_RATIO, 3.0)
        extraction = _extraction("100 requests per second")
        ok_proposal = _proposal(
            daily_active_users=100000, messages_per_user_per_day=25.92)
        self.assertAlmostEqual(
            check_workload_consistency(extraction, ok_proposal), 100.0)
        bad_proposal = _proposal(
            daily_active_users=100000, messages_per_user_per_day=25.93)
        with self.assertRaises(ValueError):
            check_workload_consistency(extraction, bad_proposal)

    def test_derived_below_stated_peak_does_not_fire(self):
        extraction = _extraction("1,000 requests per second")
        proposal = _proposal(
            daily_active_users=1000000, messages_per_user_per_day=0.85,
            read_write_ratio=9.0)
        # ~98 avg RPS against a 1,000 RPS (peak) statement: normal.
        self.assertAlmostEqual(
            check_workload_consistency(extraction, proposal), 1000.0)


class RunPathGuardrailTests(unittest.TestCase):
    def test_run_capacity_modeling_enforces_consistency(self):
        from ai_architect.agents.capacity import run as run_module
        from ai_architect.domain.requirements.state import RequirementsState

        proposal = _proposal()

        class _FakeBoundAgent:
            def invoke(self, messages):
                return proposal

        state = RequirementsState(
            session_id="s",
            original_request="url shortener for 1000 rps",
            extraction=_extraction("around 1,000 requests per second"),
        )
        with patch.object(
            run_module, "create_capacity_modeling_agent",
            return_value=_FakeBoundAgent(),
        ):
            with self.assertRaises(ValueError) as ctx:
                run_module.run_capacity_modeling(state)
        self.assertIn("inconsistent with the stated request rate",
                      str(ctx.exception))

    def test_run_capacity_modeling_passes_consistent_proposal(self):
        from ai_architect.agents.capacity import run as run_module
        from ai_architect.domain.requirements.state import RequirementsState

        proposal = _proposal(messages_per_user_per_day=0.85)

        class _FakeBoundAgent:
            def invoke(self, messages):
                return proposal

        state = RequirementsState(
            session_id="s",
            original_request="url shortener for 1000 rps",
            extraction=_extraction("around 1,000 requests per second"),
        )
        with patch.object(
            run_module, "create_capacity_modeling_agent",
            return_value=_FakeBoundAgent(),
        ):
            result = run_module.run_capacity_modeling(state)
        self.assertAlmostEqual(
            result.calculation.peak_write_qps, 295.138, places=2)


if __name__ == "__main__":
    unittest.main()
