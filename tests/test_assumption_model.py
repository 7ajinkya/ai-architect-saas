"""Focused regression tests for the Assumption confidence contract.

Slice 4B: the region-default workflow builds an Assumption with
confidence="medium", and the requirements retry prompt plus the served
API/frontend contract expose ``confidence`` as an assumption field, but
the domain model previously forbade it, so region-default construction
failed with a pydantic ValidationError. These tests pin the field:
bounded values, an honest unspecified default, serialization, and
fail-closed rejection of invalid input.

No network, no LLM, no AWS.

Run: poetry run python -m unittest discover tests
"""
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from pydantic import ValidationError

from ai_architect.config.settings import settings
from ai_architect.domain.requirements.models import (
    Assumption,
    AssumptionConfidence,
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
)
from ai_architect.services.requirements.assumptions import evaluate_deferral

REGION_QUESTION = ClarificationCandidate(
    requirement_key="region",
    requirement="geographic deployment region",
    question="Which region should this deploy to?",
    reason="Region changes cost and latency.",
    impact=RequirementImpact.ARCHITECTURE,
)


@contextmanager
def _default_region(value: str):
    with patch.multiple(settings, ai_architect_default_region=value):
        yield


def _extraction():
    return RequirementsExtraction(
        application_description="appointment booking platform",
        cloud_provider="aws",
        clarification_candidates=[REGION_QUESTION],
    )


class RegionDefaultConfidenceTests(unittest.TestCase):
    def test_region_default_assumption_constructs_with_medium_confidence(self):
        with _default_region("ap-south-1"):
            decision = evaluate_deferral(REGION_QUESTION, _extraction())
        self.assertTrue(decision.deferrable)
        self.assertIsInstance(decision.assumption, Assumption)
        self.assertEqual(decision.assumption.confidence, "medium")
        self.assertEqual(
            decision.assumption.confidence, AssumptionConfidence.MEDIUM
        )

    def test_region_default_assumption_revalidates_and_round_trips(self):
        with _default_region("ap-south-1"):
            decision = evaluate_deferral(REGION_QUESTION, _extraction())
        payload = decision.assumption.model_dump(mode="json")
        self.assertEqual(payload["confidence"], "medium")
        self.assertEqual(payload["source"], "architect_default")
        restored = Assumption.model_validate(payload)
        self.assertEqual(restored, decision.assumption)


class AssumptionConfidenceModelTests(unittest.TestCase):
    def test_confidence_defaults_to_unspecified_empty_string(self):
        assumption = Assumption(name="retention", value="30 days")
        self.assertEqual(
            assumption.confidence, AssumptionConfidence.UNSPECIFIED
        )
        self.assertEqual(
            assumption.model_dump(mode="json")["confidence"], ""
        )

    def test_supported_confidence_levels_serialize_as_strings(self):
        for level in ("high", "medium", "low"):
            with self.subTest(level=level):
                assumption = Assumption(
                    name="region", value="us-east-1", confidence=level
                )
                self.assertEqual(
                    assumption.model_dump(mode="json")["confidence"], level
                )

    def test_unenumerated_confidence_is_rejected(self):
        with self.assertRaises(ValidationError):
            Assumption(name="region", value="us-east-1", confidence="moderate")

    def test_non_string_confidence_is_rejected(self):
        with self.assertRaises(ValidationError):
            Assumption(name="region", value="us-east-1", confidence=3)

    def test_none_confidence_is_rejected(self):
        with self.assertRaises(ValidationError):
            Assumption(name="region", value="us-east-1", confidence=None)

    def test_unknown_fields_are_still_rejected(self):
        with self.assertRaises(ValidationError):
            Assumption(name="region", value="us-east-1", unexpected="x")

    def test_extraction_round_trip_preserves_confidence(self):
        extraction = RequirementsExtraction(
            application_description="app",
            cloud_provider="aws",
            assumptions=[
                Assumption(
                    name="region",
                    value="us-east-1",
                    source="architect_default",
                    rationale="User delegated region selection.",
                    confidence="medium",
                )
            ],
        )
        restored = RequirementsExtraction.model_validate(
            extraction.model_dump(mode="json")
        )
        self.assertEqual(restored.assumptions[0].confidence, "medium")
        self.assertEqual(
            restored.assumptions[0].source, "architect_default"
        )


if __name__ == "__main__":
    unittest.main()
