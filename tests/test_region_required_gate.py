"""R3 regression tests: deterministic region gate in requirements validation.

Locks the invariant ``READY => resolve_effective_region succeeds`` so no
READY state can reach Cost Estimation with an empty/unresolvable region.

All tests are LLM-free (hand-built extractions). Covers:
A. missing region, no candidates, no assumptions -> NEEDS_CLARIFICATION/region
B. missing region + sizing-impact region candidate -> still blocked
C. missing region + unknown-impact region candidate -> still blocked
D. explicit valid region -> no region block
E. single valid region assumption -> promotion satisfies, no block
F. conflicting region assumptions -> still blocked
G. existing promotion fixtures keep passing (see test_region_effective.py,
   run unmodified as part of the full suite)
H. API-level: a session with no resolvable region cannot become READY;
   answering the region question makes it READY (no default invented).
"""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.api_auth import api_client

from ai_architect.api import sessions as api_sessions_mod
from ai_architect.api.main import create_app
from ai_architect.domain.requirements.models import (
    Assumption,
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
)
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.services.region.normalizer import (
    RegionNormalizationError,
    resolve_effective_region,
)
from ai_architect.services.requirements.validator import RequirementsValidator


def _extraction(**overrides) -> RequirementsExtraction:
    payload = {
        "application_description": "telemetry ingestion platform",
        "cloud_provider": "aws",
        "region": "",
    }
    payload.update(overrides)
    return RequirementsExtraction(**payload)


def _candidate(impact=RequirementImpact.ARCHITECTURE, key="region"):
    return ClarificationCandidate(
        requirement_key=key,
        requirement="geographic deployment region",
        question="Which geographic region(s) should the system be deployed in?",
        reason="Region changes cost and latency.",
        impact=impact,
    )


def _region_keys(validation):
    return [
        c.requirement_key for c in validation.blocking_clarifications
    ]


class DeterministicRegionGateTests(unittest.TestCase):
    def test_A_missing_region_no_candidates_blocks(self):
        validation = RequirementsValidator().validate(_extraction())
        self.assertEqual(validation.status.value, "needs_clarification")
        self.assertIn("region", _region_keys(validation))
        region = next(
            c for c in validation.blocking_clarifications
            if c.requirement_key == "region"
        )
        self.assertEqual(region.impact, RequirementImpact.ARCHITECTURE)
        self.assertTrue(region.question)

    def test_no_duplicate_when_region_candidate_already_blocking(self):
        extraction = _extraction(
            clarification_candidates=[_candidate(RequirementImpact.ARCHITECTURE)]
        )
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(
            _region_keys(validation).count("region"), 1
        )

    def test_B_sizing_impact_region_candidate_still_blocks(self):
        extraction = _extraction(
            clarification_candidates=[_candidate(RequirementImpact.SIZING)]
        )
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status.value, "needs_clarification")
        self.assertIn("region", _region_keys(validation))

    def test_C_unknown_impact_region_candidate_still_blocks(self):
        extraction = _extraction(
            clarification_candidates=[_candidate(RequirementImpact.UNKNOWN)]
        )
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status.value, "needs_clarification")
        self.assertIn("region", _region_keys(validation))

    def test_D_explicit_valid_region_has_no_region_block(self):
        validation = RequirementsValidator().validate(
            _extraction(region="us-east-1")
        )
        self.assertEqual(validation.status.value, "ready")
        self.assertNotIn("region", _region_keys(validation))

    def test_D_display_name_region_has_no_region_block(self):
        validation = RequirementsValidator().validate(
            _extraction(region="US East (N. Virginia)")
        )
        self.assertEqual(validation.status.value, "ready")
        self.assertNotIn("region", _region_keys(validation))

    def test_E_single_region_assumption_promotes_without_blocking(self):
        extraction = _extraction(
            assumptions=[
                Assumption(name="region", value="us-east-1", source="assumption")
            ]
        )
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status.value, "ready")
        self.assertNotIn("region", _region_keys(validation))
        # Canonical field untouched by validation: promotion happens at Cost.
        self.assertEqual(extraction.region, "")

    def test_F_conflicting_assumptions_stay_blocked(self):
        extraction = _extraction(
            assumptions=[
                Assumption(name="region", value="us-east-1"),
                Assumption(name="primary region", value="ap-south-1"),
            ]
        )
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status.value, "needs_clarification")
        self.assertIn("region", _region_keys(validation))

    def test_F_garbage_region_stays_blocked(self):
        validation = RequirementsValidator().validate(
            _extraction(region="Atlantis")
        )
        self.assertEqual(validation.status.value, "needs_clarification")
        self.assertIn("region", _region_keys(validation))

    def test_F_ambiguous_region_value_stays_blocked(self):
        # "US"/"India" must never be silently converted into a region:
        # they stay blocking here and fail closed at Cost.
        for ambiguous in ("US", "India"):
            with self.subTest(value=ambiguous):
                validation = RequirementsValidator().validate(
                    _extraction(region=ambiguous)
                )
                self.assertEqual(validation.status.value, "needs_clarification")
                self.assertIn("region", _region_keys(validation))

    def test_non_aws_provider_keeps_existing_behavior(self):
        # Region normalization is AWS-scoped; azure keeps whatever the
        # provider rule and downstream stages already did (no new trap).
        validation = RequirementsValidator().validate(
            _extraction(cloud_provider="azure")
        )
        self.assertNotIn("region", _region_keys(validation))

    def test_resolver_not_mutated_by_validation(self):
        extraction = _extraction(
            assumptions=[
                Assumption(name="region", value="us-east-1", source="assumption")
            ]
        )
        RequirementsValidator().validate(extraction)
        self.assertEqual(extraction.region, "")
        self.assertEqual(extraction.assumptions[0].value, "us-east-1")


class RegionGateApiTests(unittest.TestCase):
    """A session must not become READY while its region is unresolvable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite

        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self._ckpt_sqlite = ckpt_sqlite
        self.client = api_client(create_app())

    def tearDown(self):
        self._ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()

    def _create(self, extraction):
        def _state_for(request, session_id=None):
            validation = RequirementsValidator().validate(extraction)
            return RequirementsState.from_extraction(
                session_id=session_id or str(uuid.uuid4()),
                original_request="ingest telemetry",
                extraction=extraction,
                validation_result=validation,
            )

        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=_state_for,
        ):
            return self.client.post(
                "/design/sessions", json={"user_request": "ingest telemetry"}
            )

    def test_H_unresolvable_region_never_ready_then_answer_readies(self):
        created = self._create(_extraction())
        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertEqual(body["status"], SessionStatus.NEEDS_CLARIFICATION.value)
        pending = body["pending_clarification"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["requirement_key"], "region")
        session_id = body["session_id"]

        # Invariant: READY implies Cost can resolve a region, so the
        # pre-answer extraction must fail resolution here.
        with self.assertRaises(RegionNormalizationError):
            resolve_effective_region(_extraction())
        answered = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "us-east-1"},
        )
        self.assertEqual(answered.status_code, 200)
        self.assertEqual(
            answered.json()["status"], SessionStatus.READY.value
        )
        self.assertEqual(
            answered.json()["requirements"]["region"], "us-east-1"
        )
        # No default invented: the stored value is the user's answer.
        self.assertFalse(
            any(a["name"] == "region" for a in answered.json()["assumptions"])
        )


if __name__ == "__main__":
    unittest.main()
