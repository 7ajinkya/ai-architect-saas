"""Part B tests: configurable architect default region on delegation.

Required cases:
  1. "you decide" + configured default region -> assumption + continue
  2. "you decide" + material residency constraint -> still blocks
  3. explicit region -> explicit value wins
  4. no default + no defensible choice -> clarification stays required
  5. same behaviour across unrelated domains

Also asserts the single-cloud provider policy is not weakened.

Run: poetry run python -m unittest discover tests
"""
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from tests.api_auth import api_client

from ai_architect.api import sessions as api_sessions_mod
from ai_architect.api.main import create_app
from ai_architect.agents.requirements import run as requirements_run
from ai_architect.config.settings import settings
from ai_architect.domain.requirements.models import (
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
)
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.services.requirements.assumptions import (
    ArchitectDefaultError,
    ASSUMPTION_SOURCE,
    configured_default_region,
    evaluate_deferral,
)
from ai_architect.services.requirements.validator import RequirementsValidator

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


def _extraction(description="appointment booking platform", **overrides):
    payload = {
        "application_description": description,
        "cloud_provider": "aws",
        "region": "",
    }
    payload.update(overrides)
    return RequirementsExtraction(
        **payload, clarification_candidates=[REGION_QUESTION]
    )


class RegionDefaultPolicyTests(unittest.TestCase):
    def test_01_configured_default_is_applied(self):
        with _default_region("ap-south-1"):
            decision = evaluate_deferral(REGION_QUESTION, _extraction())
        self.assertTrue(decision.deferrable)
        self.assertEqual(decision.value, "ap-south-1")
        self.assertEqual(decision.assumption.source, ASSUMPTION_SOURCE)
        self.assertEqual(decision.assumption.confidence, "medium")
        self.assertIn("delegated region selection", decision.assumption.rationale)
        self.assertIn("ap-south-1", decision.assumption.rationale)

    def test_02_residency_constraint_still_blocks(self):
        with _default_region("ap-south-1"):
            for description, compliance in (
                ("patient records platform", "HIPAA data residency in the US"),
                ("payments platform", "GDPR residency inside the EU"),
                ("internal tool", "data residency for EU customers"),
            ):
                with self.subTest(description=description):
                    extraction = _extraction(
                        description,
                        compliance_requirements=compliance,
                    )
                    decision = evaluate_deferral(REGION_QUESTION, extraction)
                    self.assertFalse(decision.deferrable)
                    self.assertIn("residency", decision.reason.lower())

    def test_02b_geographic_constraint_in_question_blocks(self):
        question = ClarificationCandidate(
            requirement_key="region",
            requirement="region",
            question="Users are in India only - which region?",
            reason="stated geographic constraint",
            impact=RequirementImpact.ARCHITECTURE,
        )
        with _default_region("us-east-1"):
            decision = evaluate_deferral(question, _extraction())
        self.assertFalse(decision.deferrable)

    def test_04_no_configured_default_blocks(self):
        with _default_region(""):
            decision = evaluate_deferral(REGION_QUESTION, _extraction())
        self.assertFalse(decision.deferrable)
        self.assertIn("AI_ARCHITECT_DEFAULT_REGION", decision.reason)
        self.assertIsNone(decision.assumption)

    def test_malformed_default_region_is_config_error(self):
        with _default_region("Mumbai region"):
            with self.assertRaises(ArchitectDefaultError):
                configured_default_region()

    def test_valid_default_region_reads_cleanly(self):
        for region in ("ap-south-1", "us-east-1", "eu-west-1"):
            with _default_region(region):
                self.assertEqual(configured_default_region(), region)

    def test_provider_policy_not_weakened(self):
        provider_question = ClarificationCandidate(
            requirement_key="cloud_provider",
            question="Which cloud provider?",
            impact=RequirementImpact.ARCHITECTURE,
        )
        with _default_region("ap-south-1"):
            decision = evaluate_deferral(
                provider_question, _extraction()
            )
        self.assertFalse(decision.deferrable)
        self.assertIn("single-cloud", decision.reason)
        self.assertIsNone(decision.assumption)

    def test_05_same_policy_across_unrelated_domains(self):
        domains = (
            "healthcare records platform",
            "iot telemetry pipeline",
            "ecommerce checkout service",
            "document processing pipeline",
            "video transcoding worker",
        )
        for domain in domains:
            with self.subTest(domain=domain):
                # No constraint -> deferrable with the configured default.
                with _default_region("eu-west-1"):
                    free = evaluate_deferral(
                        REGION_QUESTION, _extraction(domain)
                    )
                self.assertTrue(free.deferrable, domain)
                self.assertEqual(free.value, "eu-west-1")
                # No configured default -> blocking, regardless of domain.
                with _default_region(""):
                    bare = evaluate_deferral(
                        REGION_QUESTION, _extraction(domain)
                    )
                self.assertFalse(bare.deferrable, domain)


class RegionDefaultApiTests(unittest.TestCase):
    """End-to-end through the real API, graph, validator and checkpoint."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite

        self._orig = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self.client = api_client(create_app())

    def tearDown(self):
        from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite

        ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig
        self._tmp.cleanup()

    def _create(self, extraction):
        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=lambda request, session_id=None: _state_for(
                extraction, session_id
            ),
        ):
            return self.client.post(
                "/design/sessions",
                json={"user_request": "build a platform"},
            )

    def test_api_you_decide_applies_default_and_continues(self):
        created = self._create(_extraction())
        self.assertEqual(
            created.json()["status"],
            SessionStatus.NEEDS_CLARIFICATION.value,
        )
        session_id = created.json()["session_id"]

        with _default_region("ap-south-1"):
            deferred = self.client.post(
                f"/design/sessions/{session_id}/messages",
                json={"text": "you decide"},
            )

        self.assertEqual(deferred.status_code, 200)
        body = deferred.json()
        self.assertEqual(body["status"], SessionStatus.READY.value)
        self.assertIsNone(body["pending_clarification"])
        self.assertEqual(body["requirements"]["region"], "ap-south-1")
        assumption = next(
            a for a in body["assumptions"] if a["name"] == "region"
        )
        self.assertEqual(assumption["source"], ASSUMPTION_SOURCE)
        self.assertEqual(assumption["confidence"], "medium")

    def test_api_explicit_region_wins_over_default(self):
        created = self._create(_extraction())
        session_id = created.json()["session_id"]

        with _default_region("ap-south-1"):
            explicit = self.client.post(
                f"/design/sessions/{session_id}/messages",
                json={"text": "eu-central-1"},
            )

        body = explicit.json()
        self.assertEqual(body["status"], SessionStatus.READY.value)
        self.assertEqual(body["requirements"]["region"], "eu-central-1")
        # No default assumption was recorded for an explicit answer.
        self.assertFalse(
            any(a["name"] == "region" for a in body["assumptions"])
        )

    def test_api_residency_still_blocks_on_you_decide(self):
        extraction = _extraction(
            compliance_requirements="HIPAA data residency in the US"
        )
        created = self._create(extraction)
        session_id = created.json()["session_id"]

        with _default_region("ap-south-1"):
            refused = self.client.post(
                f"/design/sessions/{session_id}/messages",
                json={"text": "you decide"},
            )
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(
            refused.json()["detail"]["code"], "clarification_required"
        )

        # Still outstanding, then explicitly answered.
        current = self.client.get(f"/design/sessions/{session_id}").json()
        self.assertFalse(current["pending_clarification"]["assumable"])
        answered = self.client.post(
            f"/design/sessions/{session_id}/messages",
            json={"text": "us-east-1"},
        )
        self.assertEqual(answered.json()["status"], SessionStatus.READY.value)
        self.assertEqual(
            answered.json()["requirements"]["region"], "us-east-1"
        )

    def test_api_no_default_region_keeps_clarification(self):
        created = self._create(_extraction())
        session_id = created.json()["session_id"]

        with _default_region(""):
            refused = self.client.post(
                f"/design/sessions/{session_id}/messages",
                json={"text": "you decide"},
            )
        self.assertEqual(refused.status_code, 409)
        self.assertIn(
            "AI_ARCHITECT_DEFAULT_REGION",
            refused.json()["detail"]["detail"],
        )

    def test_api_assumable_flag_reflects_configured_default(self):
        created = self._create(_extraction())
        session_id = created.json()["session_id"]
        with _default_region("ap-south-1"):
            view = self.client.get(f"/design/sessions/{session_id}").json()
        self.assertTrue(view["pending_clarification"]["assumable"])
        with _default_region(""):
            view = self.client.get(f"/design/sessions/{session_id}").json()
        self.assertFalse(view["pending_clarification"]["assumable"])


def _state_for(extraction, session_id):
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=session_id or str(uuid.uuid4()),
        original_request="build a platform",
        extraction=extraction,
        validation_result=validation,
    )


if __name__ == "__main__":
    unittest.main()
