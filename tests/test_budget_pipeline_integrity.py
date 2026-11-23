"""Budget pipeline integrity: the stated monthly ceiling must survive from
the HTTP payload through requirements processing and into the Quality Gate.

Root cause being pinned (2026-10-17 live smoke test): the USD 50 budget was
lost CLIENT-side before the request was JSON-encoded. The PowerShell test
harness embedded the request in a double-quoted string, so ``$50`` was
expanded as an (undefined) shell variable and the payload itself already
read "below ." when it reached the API. The backend never saw the budget.

Evidence that the application path is correct and must stay correct:
the original 2026-10-16 run (same request, payload intact) persisted
"Target a monthly infrastructure cost below $50." through extraction,
validation, state, and the design-session handoff, and the gate parser
resolves it to a 50.0 ceiling.

These tests lock the deterministic segments of that path:
1. parse_budget_ceiling accepts the supported budget wordings and never
   invents a ceiling from missing/ambiguous text.
2. RequirementsValidator + RequirementsState preserve a supplied ceiling
   verbatim and leave a missing one unresolved (no fabrication).
3. The design quality_gate_stage reads the ceiling from requirements
   state and enforces it (within -> approved, breach -> BLOCKED, missing
   -> explicit "no parseable ceiling" review deferral).
4. The HTTP API stores and returns the request text verbatim, so any
   dollar-amount loss between "original request" and persisted state can
   only originate in the caller, not in serialization.

No LLM calls: extraction objects are constructed directly, exactly the
contract the model boundary must satisfy.

Run: poetry run python -m unittest discover tests
"""
import asyncio
import os
import tempfile
import unittest
import uuid

from tests.api_auth import api_client

from ai_architect.agents.design.graph import quality_gate_stage
from ai_architect.agents.design.state import DesignStatus
from ai_architect.api import sessions as api_sessions_mod
from ai_architect.api.main import create_app
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    QualityGateOutcome,
)
from ai_architect.domain.architecture.models import CostLink
from ai_architect.domain.requirements.models import (
    ClarificationCandidate,
    RequirementImpact,
    RequirementsExtraction,
    ValidationStatus,
)
from ai_architect.domain.requirements.state import (
    RequirementsState,
    SessionStatus,
)
from ai_architect.services.architecture.quality_gate import (
    parse_budget_ceiling,
)
from ai_architect.services.requirements.validator import RequirementsValidator


# The exact budget wording from the original messaging-system request.
BUDGET_SENTENCE = "Target a monthly infrastructure cost below $50."
BUDGET_UNDER_FORM = "under $50 per month"
BUDGET_SLASH_FORM = "Target a monthly infrastructure cost below $50/month."


def _extraction(**overrides) -> RequirementsExtraction:
    payload = {
        "application_description": (
            "Scalable, serverless messaging application similar to "
            "WhatsApp for a small startup."
        ),
        "cloud_provider": "aws",
        "region": "us-east-1",
        "budget_constraints": "",
    }
    payload.update(overrides)
    return RequirementsExtraction(**payload)


def _state(extraction: RequirementsExtraction) -> RequirementsState:
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=str(uuid.uuid4()),
        original_request="design a messaging application",
        extraction=extraction,
        validation_result=validation,
    )


class BudgetCeilingParsingTests(unittest.TestCase):
    """A ceiling is honored only when an explicit dollar amount is stated."""

    def test_slash_month_form_parses(self):
        self.assertEqual(
            parse_budget_ceiling(BUDGET_SLASH_FORM), 50.0
        )

    def test_under_per_month_form_parses(self):
        self.assertEqual(
            parse_budget_ceiling(BUDGET_UNDER_FORM), 50.0
        )

    def test_original_request_wording_parses(self):
        self.assertEqual(
            parse_budget_ceiling(BUDGET_SENTENCE), 50.0
        )

    def test_corrupted_budget_text_is_not_a_ceiling(self):
        # The exact corruption the PowerShell harness produced ("$50"
        # expanded away): "below ." states no amount.
        self.assertIsNone(
            parse_budget_ceiling(
                "Target a monthly infrastructure cost below ."
            )
        )

    def test_missing_and_ambiguous_budgets_are_not_ceilings(self):
        self.assertIsNone(parse_budget_ceiling(""))
        self.assertIsNone(parse_budget_ceiling(None))
        self.assertIsNone(
            parse_budget_ceiling("keep infrastructure cost low")
        )


class BudgetSurvivesRequirementsProcessingTests(unittest.TestCase):
    """Supplied ceilings pass through validation/state verbatim; missing
    budgets stay unresolved and are never fabricated into a value."""

    def test_numeric_ceiling_preserved_verbatim_in_state(self):
        state = _state(_extraction(budget_constraints=BUDGET_SENTENCE))
        self.assertEqual(state.status, SessionStatus.READY)
        self.assertEqual(
            state.extraction.budget_constraints, BUDGET_SENTENCE
        )
        self.assertEqual(
            parse_budget_ceiling(state.extraction.budget_constraints),
            50.0,
        )

    def test_under_form_ceiling_survives_validation(self):
        state = _state(_extraction(budget_constraints=BUDGET_UNDER_FORM))
        self.assertEqual(state.status, SessionStatus.READY)
        self.assertEqual(
            parse_budget_ceiling(state.extraction.budget_constraints),
            50.0,
        )

    def test_missing_budget_stays_unresolved_and_is_not_invented(self):
        state = _state(_extraction(budget_constraints=""))
        self.assertEqual(state.status, SessionStatus.READY)
        self.assertEqual(state.extraction.budget_constraints, "")
        self.assertIsNone(
            parse_budget_ceiling(state.extraction.budget_constraints)
        )
        # No assumption may stand in for a budget the user never stated.
        self.assertEqual(state.extraction.assumptions, [])
        self.assertEqual(state.validation_result.accepted_assumptions, [])

    def test_budget_candidate_does_not_block_or_fabricate(self):
        # Mirrors the real smoke-test extraction: the LLM flagged the
        # missing budget with impact "sizing", which must not block the
        # session and must not produce a value.
        extraction = _extraction(
            budget_constraints="",
            clarification_candidates=[
                ClarificationCandidate(
                    requirement_key="budget_constraints",
                    requirement="budget amount",
                    question=(
                        "What is the maximum monthly budget (numeric "
                        "value) for the infrastructure?"
                    ),
                    reason=(
                        "A concrete budget is needed to evaluate "
                        "cost-effective service choices and sizing."
                    ),
                    impact=RequirementImpact.SIZING,
                )
            ],
        )
        validation = RequirementsValidator().validate(extraction)
        self.assertEqual(validation.status, ValidationStatus.READY)
        self.assertEqual(validation.blocking_clarifications, [])
        self.assertEqual(extraction.budget_constraints, "")


class _FakeArchitecture:
    def __init__(self, totals, artifact_id="artifact-1"):
        self.artifact_id = artifact_id
        self.cost_totals_snapshot = totals
        self.cost_links = [
            CostLink(component_name="Amazon API Gateway (HTTP)", priced=True)
        ]


def _reviewed_critique(artifact_id="artifact-1") -> DesignCritique:
    return DesignCritique(
        artifact_id="critique-1",
        session_id="design-s1",
        reviewed_artifact_id=artifact_id,
        reviewed_version=1,
        findings=[],
        summary="clean review",
    )


class BudgetReachesQualityGateTests(unittest.TestCase):
    """quality_gate_stage must consume requirements_state.extraction.
    budget_constraints and enforce the parsed ceiling deterministically."""

    def _run_stage(self, budget, totals):
        requirements_state = _state(
            _extraction(budget_constraints=budget)
        )
        state = {
            "requirements_state": requirements_state,
            "architecture": _FakeArchitecture(totals=totals),
            "critique": _reviewed_critique(),
            "revision_count": 0,
        }
        return asyncio.run(quality_gate_stage(state))

    def test_budget_from_requirements_state_enforced_within_ceiling(self):
        result = self._run_stage(BUDGET_SENTENCE, {"on_demand": 1.125})
        gate = result["quality_gate"]
        self.assertEqual(result["status"], DesignStatus.READY.value)
        self.assertEqual(
            gate.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertTrue(
            any("$50.00/mo ceiling" in r for r in gate.reasons),
            msg=f"expected enforced ceiling in reasons, got: {gate.reasons}",
        )

    def test_under_per_month_budget_enforced_within_ceiling(self):
        result = self._run_stage(BUDGET_UNDER_FORM, {"on_demand": 1.125})
        gate = result["quality_gate"]
        self.assertEqual(result["status"], DesignStatus.READY.value)
        self.assertTrue(
            any("$50.00/mo ceiling" in r for r in gate.reasons),
            msg=f"expected enforced ceiling in reasons, got: {gate.reasons}",
        )

    def test_budget_breach_blocks_through_stage(self):
        result = self._run_stage(BUDGET_SENTENCE, {"on_demand": 75.0})
        gate = result["quality_gate"]
        self.assertEqual(result["status"], DesignStatus.FAILED.value)
        self.assertEqual(gate.outcome, QualityGateOutcome.BLOCKED)
        self.assertIn("Design quality gate blocked", result["error"])
        self.assertTrue(
            any(
                "exceeds the $50.00/mo ceiling" in r
                for r in gate.reasons
            ),
            msg=f"expected breach reason, got: {gate.reasons}",
        )

    def test_missing_budget_defers_to_review_without_fabrication(self):
        result = self._run_stage("", {"on_demand": 999999.0})
        gate = result["quality_gate"]
        # No ceiling is invented; the gate must say so explicitly rather
        # than blocking on or fabricating a budget.
        self.assertEqual(result["status"], DesignStatus.READY.value)
        self.assertEqual(
            gate.outcome, QualityGateOutcome.APPROVED_FOR_REVIEW
        )
        self.assertTrue(
            any(
                "No parseable budget ceiling stated" in r
                for r in gate.reasons
            ),
            msg=f"expected missing-ceiling reason, got: {gate.reasons}",
        )


class BudgetRequestRoundTripTests(unittest.TestCase):
    """The API must persist and return the request text verbatim: any
    dollar-amount loss between the client's original text and the stored
    session can only originate in the caller, never in serialization."""

    REQUEST = (
        "Design a small messaging app.\n\n"
        "Budget:\n"
        "- Target a monthly infrastructure cost below $50/month.\n"
    )

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

    def _build_state(self, request, session_id=None):
        extraction = _extraction(
            budget_constraints=(
                "Target a monthly infrastructure cost below $50/month."
            )
        )
        validation = RequirementsValidator().validate(extraction)
        return RequirementsState.from_extraction(
            session_id=session_id or str(uuid.uuid4()),
            original_request=request,
            extraction=extraction,
            validation_result=validation,
        )

    def test_dollar_amount_persists_verbatim_and_reaches_view(self):
        from unittest.mock import patch

        with patch.object(
            api_sessions_mod,
            "build_initial_requirements_state",
            side_effect=self._build_state,
        ):
            created = self.client.post(
                "/design/sessions",
                json={"user_request": self.REQUEST},
            )
        self.assertEqual(created.status_code, 200)
        body = created.json()
        session_id = body["session_id"]

        # The exact text, including "$50/month", comes back unmodified.
        self.assertEqual(body["user_request"], self.REQUEST)
        self.assertIn("$50/month", body["user_request"])
        self.assertEqual(
            parse_budget_ceiling(
                body["requirements"]["budget_constraints"]
            ),
            50.0,
        )

        # And it survives the checkpoint round-trip on re-read.
        refetched = self.client.get(
            f"/design/sessions/{session_id}"
        ).json()
        self.assertEqual(refetched["user_request"], self.REQUEST)
        self.assertEqual(
            parse_budget_ceiling(
                refetched["requirements"]["budget_constraints"]
            ),
            50.0,
        )


if __name__ == "__main__":
    unittest.main()
