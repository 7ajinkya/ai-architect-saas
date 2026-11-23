"""Focused regression tests for the Capacity Modeling empty-response fallback.

Covers, with all LLM calls faked:
1. valid structured output triggers zero retries;
2. empty structured response triggers exactly one unbound retry whose
   text parses into a valid CapacityModelInput feeding the deterministic
   calculator;
3. an empty retry fails loudly with ValueError (never None downstream);
4. required-field validation remains enforced on retry text.

Run: poetry run python -m unittest discover tests
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from ai_architect.agents.capacity import run as run_module
from ai_architect.agents.capacity.models import CapacityModelProposal
from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.domain.requirements.state import RequirementsState

RETRY_JSON = {
    "daily_active_users": 5000000,
    "messages_per_user_per_day": 10.0,
    "peak_factor": 3.0,
    "read_write_ratio": 4.0,
    "average_message_size_bytes": 1024,
    "connection_factor": 0.1,
    "storage_retention_days": 30,
    "rationale": {"daily_active_users": "explicitly provided 5M DAU"},
}


def _state() -> RequirementsState:
    return RequirementsState(
        session_id="s",
        original_request="e-commerce platform for 5M DAU",
        extraction=RequirementsExtraction(
            application_description="e-commerce",
            cloud_provider="aws",
            target_users_dau="5 million",
        ),
    )


class _FakeBoundAgent:
    """Stands in for the tools-bound structured agent."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self._outcome


class _FakeRawAgent:
    """Stands in for the unbound (no-tools) agent."""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0
        self.seen_messages = None

    def invoke(self, messages):
        self.calls += 1
        self.seen_messages = messages
        return AIMessage(content=self._content)


class CapacityEmptyResponseFallbackTests(unittest.TestCase):
    def test_valid_structured_output_never_retries(self):
        proposal = CapacityModelProposal(**RETRY_JSON)
        bound = _FakeBoundAgent(proposal)
        raw = _FakeRawAgent(json.dumps(RETRY_JSON))
        with (
            patch.object(
                run_module, "create_capacity_modeling_agent",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_capacity_modeling_agent_raw",
                return_value=raw,
            ),
        ):
            result = run_module.run_capacity_modeling(_state())
        self.assertIsInstance(result, CapacityModelingResult)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(result.proposal.daily_active_users, 5000000)
        self.assertAlmostEqual(result.calculation.peak_write_qps, 1736.111111, places=4)

    def test_empty_response_retries_unbound_and_calculates(self):
        bound = _FakeBoundAgent(None)
        raw = _FakeRawAgent(json.dumps(RETRY_JSON))
        with (
            patch.object(
                run_module, "create_capacity_modeling_agent",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_capacity_modeling_agent_raw",
                return_value=raw,
            ),
        ):
            result = run_module.run_capacity_modeling(_state())
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 1)
        # Same prompt was reused for the retry.
        self.assertEqual(len(raw.seen_messages), 2)
        self.assertIsInstance(result.proposal, CapacityModelProposal)
        self.assertEqual(result.proposal.daily_active_users, 5000000)
        self.assertAlmostEqual(result.calculation.peak_write_qps, 1736.111111, places=4)

    def test_empty_retry_fails_loudly(self):
        bound = _FakeBoundAgent(None)
        raw = _FakeRawAgent("")
        with (
            patch.object(
                run_module, "create_capacity_modeling_agent",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_capacity_modeling_agent_raw",
                return_value=raw,
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                run_module.run_capacity_modeling(_state())
        self.assertIn("empty text", str(ctx.exception))
        self.assertEqual(raw.calls, 1)

    def test_retry_missing_required_field_fails_loudly(self):
        bad = dict(RETRY_JSON)
        del bad["daily_active_users"]
        bound = _FakeBoundAgent(None)
        raw = _FakeRawAgent(json.dumps(bad))
        with (
            patch.object(
                run_module, "create_capacity_modeling_agent",
                return_value=bound,
            ),
            patch.object(
                run_module, "create_capacity_modeling_agent_raw",
                return_value=raw,
            ),
        ):
            with self.assertRaises(ValueError) as ctx:
                run_module.run_capacity_modeling(_state())
        self.assertIn("daily_active_users", str(ctx.exception))


NESTED_JSON = {
    "daily_active_users": {"value": 10000000, "rationale": "large launch"},
    "messages_per_user_per_day": {"value": 9, "rationale": "mixed reads"},
    "peak_factor": {"value": 2.0, "rationale": "evening peak"},
    "read_write_ratio": {"value": 0.95, "rationale": "write-heavy"},
    "average_message_size_bytes": {"value": 1024, "rationale": "1KB msgs"},
    "connection_factor": {"value": 1, "rationale": "short-lived"},
    "storage_retention_days": {"value": 365, "rationale": "one year"},
}


class CapacityOutputShapeContractTests(unittest.TestCase):
    """The flat-shape boundary: nested {value, rationale} parameter
    objects (as emitted by a real smoke-test run that skipped the
    function call) must stay loudly rejected; flat payloads stay valid."""

    def test_nested_value_rationale_payload_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            run_module._parse_text_envelope(json.dumps(NESTED_JSON))
        message = str(ctx.exception)
        self.assertIn("not valid CapacityModelProposal JSON", message)
        self.assertIn("daily_active_users", message)

    def test_flat_payload_with_top_level_rationale_accepted(self):
        proposal = run_module._parse_text_envelope(json.dumps(RETRY_JSON))
        self.assertIsInstance(proposal, CapacityModelProposal)
        self.assertEqual(proposal.daily_active_users, 5000000)
        self.assertAlmostEqual(proposal.messages_per_user_per_day, 10.0)
        self.assertEqual(
            proposal.rationale,
            {"daily_active_users": "explicitly provided 5M DAU"},
        )

    def test_prompt_states_flat_shape_and_bans_nested_objects(self):
        from ai_architect.agents.capacity.prompt import (
            CAPACITY_MODELING_SYSTEM_PROMPT,
        )
        self.assertIn("bare number", CAPACITY_MODELING_SYSTEM_PROMPT)
        self.assertIn('{"value"', CAPACITY_MODELING_SYSTEM_PROMPT)
        self.assertIn("top-level", CAPACITY_MODELING_SYSTEM_PROMPT)
        # Semantic instructions and parameter names are unchanged.
        for name in (
            "daily_active_users",
            "messages_per_user_per_day",
            "peak_factor",
            "read_write_ratio",
            "average_message_size_bytes",
            "connection_factor",
            "storage_retention_days",
        ):
            self.assertIn(name, CAPACITY_MODELING_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
