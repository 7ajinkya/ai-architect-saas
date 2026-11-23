"""Slice 2 tests: API session lifecycle + "you decide" assumption policy.

Covers the required smoke cases:
  1. create session
  2. send normal requirement
  3. clarification response
  4. "you decide" response -> documented assumption -> READY/continue
  5. genuinely blocking requirement -> clarification remains required
  6. retrieve session state

Extraction (the LLM call) is faked; the requirements graph, validator,
deferral policy, and checkpoint persistence are the real code paths.

Run: poetry run python -m unittest discover tests
"""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.api_auth import api_client

from ai_architect.api import sessions as api_sessions_mod
from ai_architect.api.main import create_app
from ai_architect.agents.requirements import run as requirements_run
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
from ai_architect.services.requirements.assumptions import (
    ARCHITECTURAL_DEFAULTS,
    ASSUMPTION_SOURCE,
    evaluate_deferral,
    is_deferral,
)
from ai_architect.services.requirements.validator import RequirementsValidator


def _extraction(**overrides) -> RequirementsExtraction:
    payload = {
        "application_description": "appointment booking platform",
        "cloud_provider": "aws",
        "region": "",
        "target_users_dau": "50000",
        "storage_characteristics": "",
        "retention": "",
        "availability": "",
    }
    payload.update(overrides)
    return RequirementsExtraction(**payload)


def _extraction_with_storage_question() -> RequirementsExtraction:
    return _extraction(
        clarification_candidates=[
            ClarificationCandidate(
                requirement_key="storage_characteristics",
                requirement="storage characteristics",
                question=(
                    "What storage requirements apply to this workload "
                    "(e.g. size, durability, transactional needs)?"
                ),
                reason="Storage choice changes the architecture.",
                impact=RequirementImpact.ARCHITECTURE,
            )
        ]
    )


def _extraction_with_region_question() -> RequirementsExtraction:
    return _extraction(
        clarification_candidates=[
            ClarificationCandidate(
                requirement_key="region",
                requirement="geographic deployment region",
                question="Which region should this deploy to?",
                reason="Region changes cost and latency.",
                impact=RequirementImpact.ARCHITECTURE,
            )
        ]
    )


def _state(extraction: RequirementsExtraction) -> RequirementsState:
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=str(uuid.uuid4()),
        original_request="build me a booking platform",
        extraction=extraction,
        validation_result=validation,
    )


class DeferralPolicyTests(unittest.TestCase):
    def test_markers_detected(self):
        for text in (
            "you decide", "You decide.", "your call", "up to you",
            "whatever you think", "I don't know", "you're the architect",
            "no idea",
        ):
            self.assertTrue(is_deferral(text), text)

    def test_real_answers_not_deferrals(self):
        for text in (
            "aws", "Mumbai region", "365 days", "99.99%",
            "high write throughput", "HIPAA",
        ):
            self.assertFalse(is_deferral(text), text)
        self.assertFalse(is_deferral(""))

    def test_deferrable_field_records_documented_assumption(self):
        extraction = _extraction()
        candidate = extraction.clarification_candidates = None
        decision = evaluate_deferral(
            ClarificationCandidate(
                requirement_key="storage_characteristics",
                question="What storage requirements apply?",
                impact=RequirementImpact.ARCHITECTURE,
            ),
            extraction,
        )
        self.assertTrue(decision.deferrable)
        self.assertIsNotNone(decision.assumption)
        self.assertEqual(decision.assumption.source, ASSUMPTION_SOURCE)
        self.assertEqual(
            decision.value, ARCHITECTURAL_DEFAULTS["storage_characteristics"]["value"]
        )
        self.assertTrue(decision.assumption.rationale)
        self.assertFalse(candidate)

    def test_unconstrained_field_records_empty_value_assumption(self):
        extraction = _extraction()
        # budget_constraints has no generic architectural default, so it
        # is deferred as explicitly unconstrained rather than invented.
        decision = evaluate_deferral(
            ClarificationCandidate(
                requirement_key="budget_constraints",
                question="What budget applies?",
                impact=RequirementImpact.ARCHITECTURE,
            ),
            extraction,
        )
        self.assertTrue(decision.deferrable)
        self.assertEqual(decision.value, "")
        self.assertIn("unconstrained", decision.assumption.rationale)

    def test_provider_is_material_and_never_assumed(self):
        decision = evaluate_deferral(
            ClarificationCandidate(
                requirement_key="cloud_provider",
                question="Which cloud provider?",
                impact=RequirementImpact.ARCHITECTURE,
            ),
            _extraction(),
        )
        self.assertFalse(decision.deferrable)
        self.assertIn("material", decision.reason)
        self.assertIsNone(decision.assumption)

    def test_region_is_material_and_never_assumed(self):
        decision = evaluate_deferral(
            ClarificationCandidate(
                requirement_key="region",
                question="Which region?",
                impact=RequirementImpact.ARCHITECTURE,
            ),
            _extraction(),
        )
        self.assertFalse(decision.deferrable)
        self.assertIsNone(decision.assumption)

    def test_compliance_material_in_regulated_context(self):
        regulated = _extraction(
            application_description="banking payments platform"
        )
        decision = evaluate_deferral(
            ClarificationCandidate(
                requirement_key="compliance_requirements",
                question="Which compliance regime applies?",
                impact=RequirementImpact.COMPLIANCE,
            ),
            regulated,
        )
        self.assertFalse(decision.deferrable)
        self.assertIn("compliance", decision.reason.lower())

        # Same question, non-regulated context: deferrable.
        permissive = _extraction(
            application_description="internal tool"
        )
        permissive.compliance_requirements = "none"
        permissive_decision = evaluate_deferral(
            ClarificationCandidate(
                requirement_key="compliance_requirements",
                question="Which compliance regime applies?",
                impact=RequirementImpact.COMPLIANCE,
            ),
            permissive,
        )
        self.assertTrue(permissive_decision.deferrable)

    def test_policy_is_domain_neutral(self):
        # Identical policy decision regardless of workload domain.
        for domain in (
            "healthcare records platform",
            "iot telemetry pipeline",
            "ecommerce checkout",
            "video transcoding service",
        ):
            decision = evaluate_deferral(
                ClarificationCandidate(
                    requirement_key="storage_characteristics",
                    question="Storage requirements?",
                    impact=RequirementImpact.ARCHITECTURE,
                ),
                _extraction(application_description=domain),
            )
            self.assertTrue(decision.deferrable, domain)
            self.assertEqual(
                decision.value,
                ARCHITECTURAL_DEFAULTS["storage_characteristics"]["value"],
            )


class ApiSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Persist into an isolated checkpoint database per test.
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

    def _create(self, extraction: RequirementsExtraction):
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state_for(
                extraction, session_id
            ),
        ):
            return self.client.post(
                "/design/sessions",
                json={"user_request": "build me a booking platform"},
            )

    def test_01_create_session_with_no_questions(self):
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state_for(
                _extraction(region="ap-south-1"), session_id
            ),
        ):
            response = self.client.post(
                "/design/sessions",
                json={"user_request": "build me a booking platform"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], SessionStatus.READY.value)
        self.assertTrue(body["session_id"])
        self.assertIsNone(body["pending_clarification"])
        self.assertIsNotNone(body["requirements"])

    def test_02_clarification_response_advances_to_ready(self):
        response = self._create(_extraction_with_storage_question())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], SessionStatus.NEEDS_CLARIFICATION.value)
        pending = body["pending_clarification"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["requirement_key"], "storage_characteristics")

        answered = self.client.post(
            f"/design/sessions/{body['session_id']}/messages",
            json={"text": "high durability object storage, 365 days"},
        )
        self.assertEqual(answered.status_code, 200)
        after = answered.json()
        self.assertEqual(
            after["requirements"]["storage_characteristics"],
            "high durability object storage, 365 days",
        )
        # The deterministic region gate now follows: an unresolved region
        # can no longer ride along to READY (and then fail at Cost).
        self.assertEqual(
            after["status"], SessionStatus.NEEDS_CLARIFICATION.value
        )
        self.assertIsNotNone(after["pending_clarification"])
        self.assertEqual(
            after["pending_clarification"]["requirement_key"], "region"
        )

        region_answered = self.client.post(
            f"/design/sessions/{body['session_id']}/messages",
            json={"text": "us-east-1"},
        )
        self.assertEqual(region_answered.status_code, 200)
        final = region_answered.json()
        self.assertEqual(final["status"], SessionStatus.READY.value)
        self.assertIsNone(final["pending_clarification"])

    def test_03_you_decide_records_documented_assumption(self):
        response = self._create(_extraction_with_storage_question())
        session_id = response.json()["session_id"]

        deferred = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "you decide"},
        )
        self.assertEqual(deferred.status_code, 200)
        body = deferred.json()
        # The storage deferral is recorded, but the deterministic region
        # gate now keeps the session open on the region question instead
        # of letting an empty region reach READY (and then fail at Cost).
        self.assertEqual(
            body["status"], SessionStatus.NEEDS_CLARIFICATION.value
        )
        self.assertIsNotNone(body["pending_clarification"])
        self.assertEqual(
            body["pending_clarification"]["requirement_key"], "region"
        )
        # Assumption recorded with source + rationale.
        self.assertEqual(len(body["assumptions"]), 1)
        assumption = body["assumptions"][0]
        self.assertEqual(assumption["name"], "storage_characteristics")
        self.assertEqual(assumption["source"], ASSUMPTION_SOURCE)
        self.assertTrue(assumption["rationale"])
        self.assertEqual(
            assumption["value"],
            ARCHITECTURAL_DEFAULTS["storage_characteristics"]["value"],
        )
        # Not counted as a user-provided requirement.
        self.assertEqual(
            body["requirements"]["storage_characteristics"],
            ARCHITECTURAL_DEFAULTS["storage_characteristics"]["value"],
        )

    def test_04_blocking_requirement_stays_required(self):
        response = self._create(_extraction_with_region_question())
        session_id = response.json()["session_id"]

        refused = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "you decide"},
        )
        self.assertEqual(refused.status_code, 409)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "clarification_required")
        self.assertIn("region", detail["detail"])

        # Still outstanding and answerable explicitly.
        current = self.client.get(f"/design/sessions/{session_id}").json()
        self.assertEqual(current["status"], SessionStatus.NEEDS_CLARIFICATION.value)
        self.assertIsNotNone(current["pending_clarification"])
        self.assertFalse(current["pending_clarification"]["assumable"])

        answered = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "Mumbai region"},
        )
        self.assertEqual(answered.status_code, 200)
        self.assertEqual(answered.json()["status"], SessionStatus.READY.value)

    def test_05_get_session_returns_state(self):
        response = self._create(_extraction_with_storage_question())
        session_id = response.json()["session_id"]
        fetched = self.client.get(f"/design/sessions/{session_id}")
        self.assertEqual(fetched.status_code, 200)
        body = fetched.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertEqual(body["user_request"], "build me a booking platform")
        self.assertEqual(
            body["pending_clarification"]["requirement_key"],
            "storage_characteristics",
        )

    def test_06_unknown_session_returns_404(self):
        missing = self.client.get("/design/sessions/does-not-exist")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json()["detail"]["code"], "session_not_found"
        )

    def test_07_message_without_pending_clarification_conflicts(self):
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state_for(
                _extraction(region="ap-south-1"), session_id
            ),
        ):
            created = self.client.post(
                "/design/sessions", json={"user_request": "hello"}
            )
        session_id = created.json()["session_id"]
        conflict = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "aws"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["detail"]["code"], "no_pending_clarification"
        )

    def test_08_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})


def _state_for(
    extraction: RequirementsExtraction, session_id: str | None
) -> RequirementsState:
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=session_id or str(uuid.uuid4()),
        original_request="build me a booking platform",
        extraction=extraction,
        validation_result=validation,
    )


if __name__ == "__main__":
    unittest.main()
