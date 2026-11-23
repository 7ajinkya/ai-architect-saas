"""Focused regression tests for the Requirements LLM boundary.

Covers, with all LLM calls faked:
1. valid structured output continues normally to validation + state;
2. a null structured result with empty text triggers exactly one unbound
   retry whose text parses into a valid RequirementsExtraction;
3. a null structured result with non-empty text parses without retry;
4. empty retry text fails with an explicit LLM boundary error (never the
   opaque "'NoneType' object has no attribute
   'clarification_candidates'");
5. invalid text fails explicitly;
6. a genuine schema violation (parsing_error) fails explicitly;
7. a valid extraction with zero clarification_candidates stays valid;
8. the deterministic provider-clarification policy still blocks;
9. HITL state mechanics (answer -> re-validate -> READY) still work.

Run: poetry run python -m unittest discover tests
"""
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from ai_architect.agents.requirements import run as run_module
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.services.requirements.validator import RequirementsValidator

RETRY_JSON = {
    "application_description": "URL shortener for 10000 DAU",
    "cloud_provider": "aws",
    "region": "Mumbai region",
    "target_users_dau": "10000",
    "clarification_candidates": [],
}


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


class _ScriptedBoundAgent:
    """Replays one scripted outcome per invoke; records call count."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        return outcome() if callable(outcome) else outcome


@contextmanager
def _patched(bound, raw):
    with (
        patch.object(
            run_module, "create_requirements_agent_with_raw",
            return_value=bound,
        ),
        patch.object(
            run_module, "create_requirements_agent_raw",
            return_value=raw,
        ),
    ):
        yield


def _parsed_extraction(**overrides):
    payload = dict(RETRY_JSON)
    payload.update(overrides)
    return RequirementsExtraction(**payload)


class RequirementsBoundaryTests(unittest.TestCase):
    def test_valid_structured_output_continues_normally(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content=""),
                "parsed": _parsed_extraction(),
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent(json.dumps(RETRY_JSON))
        with _patched(bound, raw):
            state = run_module.build_initial_requirements_state(
                "URL shortener for 10000 DAU on AWS"
            )
        self.assertIsInstance(state, RequirementsState)
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            state.extraction.application_description,
            "URL shortener for 10000 DAU",
        )
        self.assertEqual(state.status, SessionStatus.READY)

    def test_null_result_retries_unbound_and_recovers(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content="", tool_calls=[]),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent(json.dumps(RETRY_JSON))
        with _patched(bound, raw):
            state = run_module.build_initial_requirements_state(
                "URL shortener for 10000 DAU on AWS"
            )
        self.assertEqual(bound.calls, 1)
        self.assertEqual(raw.calls, 1)
        # Same prompt was reused for the retry.
        self.assertEqual(len(raw.seen_messages), 2)
        self.assertIsInstance(state.extraction, RequirementsExtraction)
        self.assertEqual(state.status, SessionStatus.READY)

    def test_text_envelope_parses_without_retry(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content=json.dumps(RETRY_JSON)),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent("should never be used")
        with _patched(bound, raw):
            state = run_module.build_initial_requirements_state(
                "URL shortener for 10000 DAU on AWS"
            )
        self.assertEqual(raw.calls, 0)
        self.assertEqual(
            state.extraction.cloud_provider, "aws"
        )

    def test_empty_retry_fails_with_explicit_boundary_error(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content="", tool_calls=[]),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent("")
        with _patched(bound, raw):
            with self.assertRaises(ValueError) as ctx:
                run_module.build_initial_requirements_state(
                    "URL shortener for 10000 DAU on AWS"
                )
        message = str(ctx.exception)
        self.assertIn("no function call and empty text", message)
        self.assertNotIn("NoneType", message)
        self.assertNotIn(
            "'NoneType' object has no attribute", message
        )
        # Bounded retry: one empty-response retry per attempt, two attempts.
        self.assertEqual(raw.calls, 2)
        self.assertEqual(bound.calls, 2)

    def test_invalid_text_fails_explicitly(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content="not json at all"),
                "parsed": None,
                "parsing_error": None,
            }
        )
        raw = _FakeRawAgent("should never be used")
        with _patched(bound, raw):
            with self.assertRaises(ValueError) as ctx:
                run_module.build_initial_requirements_state(
                    "URL shortener for 10000 DAU on AWS"
                )
        self.assertIn("not valid RequirementsExtraction JSON",
                      str(ctx.exception))

    def test_schema_violation_fails_explicitly(self):
        bound = _FakeBoundAgent(
            {
                "raw": AIMessage(content=""),
                "parsed": None,
                "parsing_error": "3 validation errors for RequirementsExtraction",
            }
        )
        raw = _FakeRawAgent("should never be used")
        with _patched(bound, raw):
            with self.assertRaises(ValueError) as ctx:
                run_module.build_initial_requirements_state(
                    "URL shortener for 10000 DAU on AWS"
                )
        self.assertIn("Requirements LLM boundary failure",
                      str(ctx.exception))
        self.assertEqual(raw.calls, 0)

    def test_schema_rejects_unexpected_fields(self):
        payload = dict(RETRY_JSON)
        payload["clarification_candidates_additional_fields"] = []
        payload["additional_requirements_extra"] = []
        payload["additional_requirements_extra2"] = []
        payload["additional_requirements_extra3"] = []
        payload["additional_requirements_extra4"] = []
        with self.assertRaises(ValidationError):
            RequirementsExtraction(**payload)

    def test_extra_fields_schema_violation_retries_then_succeeds(self):
        violation = {
            "raw": AIMessage(content=""),
            "parsed": None,
            "parsing_error": (
                "5 validation errors for RequirementsExtraction\n"
                "clarification_candidates_additional_fields\n"
                "  Extra inputs are not permitted "
                "[type=extra_forbidden, ...]"
            ),
        }
        bound = _ScriptedBoundAgent([violation, _parsed_outcome()])
        raw = _FakeRawAgent("should never be used")
        with _patched(bound, raw):
            state = run_module.build_initial_requirements_state(
                "URL shortener for 10000 DAU on AWS"
            )
        self.assertIsInstance(state, RequirementsState)
        # Exactly two attempts: initial violation, then bounded retry.
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)
        self.assertEqual(state.status, SessionStatus.READY)
        self.assertEqual(
            state.extraction.application_description, "URL shortener for 10000 DAU"
        )

    def test_schema_violation_exhaustion_fails_loudly(self):
        violation = {
            "raw": AIMessage(content=""),
            "parsed": None,
            "parsing_error": "5 validation errors for RequirementsExtraction",
        }
        bound = _ScriptedBoundAgent([violation, violation])
        raw = _FakeRawAgent("should never be used")
        with _patched(bound, raw):
            with self.assertRaises(ValueError) as ctx:
                run_module.build_initial_requirements_state(
                    "URL shortener for 10000 DAU on AWS"
                )
        message = str(ctx.exception)
        self.assertIn("Requirements LLM boundary failure", message)
        self.assertIn("even after one bounded correction retry", message)
        self.assertIn("First:", message)
        self.assertIn("Retry:", message)
        # Exactly two attempts, no third.
        self.assertEqual(bound.calls, 2)
        self.assertEqual(raw.calls, 0)

    def test_zero_candidates_with_provider_is_ready(self):
        extraction = _parsed_extraction()
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status.value, "ready")
        self.assertEqual(validation.blocking_clarifications, [])

    def test_missing_provider_still_blocks_deterministically(self):
        extraction = _parsed_extraction(cloud_provider="")
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status.value, "needs_clarification")
        self.assertTrue(
            any(
                c.requirement_key == "cloud_provider"
                for c in validation.blocking_clarifications
            )
        )

    def test_hitl_answer_then_ready(self):
        extraction = _parsed_extraction(
            cloud_provider="aws",
            region="",
            clarification_candidates=[
                {
                    "requirement_key": "region",
                    "requirement": "geographic deployment region",
                    "question": "Which region?",
                    "reason": "Region changes the architecture.",
                    "impact": "architecture",
                }
            ],
        )
        validation = RequirementsValidator().validate(extraction)
        state = RequirementsState.from_extraction(
            session_id="s",
            original_request="URL shortener",
            extraction=extraction,
            validation_result=validation,
        )
        self.assertEqual(state.status, SessionStatus.NEEDS_CLARIFICATION)
        self.assertEqual(len(state.pending_clarifications()), 1)
        state.answer_clarification(
            state.pending_clarifications()[0].id, "Mumbai region"
        )
        self.assertEqual(state.extraction.region, "Mumbai region")
        revalidation = RequirementsValidator().validate(state.extraction)
        self.assertEqual(revalidation.status.value, "ready")


def _parsed_outcome() -> dict:
    return {
        "raw": AIMessage(content=""),
        "parsed": _parsed_extraction(),
        "parsing_error": None,
    }


if __name__ == "__main__":
    unittest.main()
