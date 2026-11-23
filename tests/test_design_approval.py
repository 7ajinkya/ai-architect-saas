"""Design approval boundary tests: shared rule, API, CLI, persistence.

No LLM calls. Approval records are plain-JSON checkpoint channels;
persistence round-trips through a temp checkpoint database.

Run: poetry run python -m unittest discover tests
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.api_auth import api_client

from ai_architect.api import main as main_module
from ai_architect.api.main import create_app
from ai_architect.domain.architecture.critique import (
    QualityGateOutcome,
    QualityGateResult,
)
from ai_architect.services.approvals import (
    APPROVABLE_GATE_OUTCOMES,
    build_design_approval,
    design_approval_allowed,
)


def _gate(outcome):
    return QualityGateResult(
        outcome=outcome,
        reasons=["reason"],
        critical_count=0,
        warning_count=0,
        evaluated_artifact_id="a1",
    )


def _snapshot(gate=None, architecture=True):
    return SimpleNamespace(
        session_id="design-s1",
        user_request="build it",
        requirements_state=object(),
        architecture=(
            SimpleNamespace(artifact_id="a1") if architecture else None
        ),
        quality_gate=gate,
        critique=None,
        cost_result=None,
        warnings=[],
        status="ready",
        error=None,
    )


class ApprovalRuleTests(unittest.TestCase):
    def test_approved_gate_is_approvable(self):
        allowed, outcome = design_approval_allowed(
            _snapshot(_gate(QualityGateOutcome.APPROVED_FOR_REVIEW))
        )
        self.assertTrue(allowed)
        self.assertEqual(outcome, "approved_for_review")

    def test_user_decision_gate_is_approvable(self):
        allowed, outcome = design_approval_allowed(
            _snapshot(_gate(QualityGateOutcome.REQUIRES_USER_DECISION))
        )
        self.assertTrue(allowed)
        self.assertEqual(outcome, "requires_user_decision")

    def test_blocked_and_missing_gates_refuse(self):
        for gate in (
            None,
            _gate(QualityGateOutcome.BLOCKED),
            _gate(QualityGateOutcome.REQUIRES_REVISION),
        ):
            allowed, _ = design_approval_allowed(_snapshot(gate))
            self.assertFalse(allowed)

    def test_missing_architecture_refuses(self):
        allowed, _ = design_approval_allowed(_snapshot(
            _gate(QualityGateOutcome.APPROVED_FOR_REVIEW),
            architecture=False,
        ))
        self.assertFalse(allowed)

    def test_record_builder_is_plain_json(self):
        record = build_design_approval(
            approved=True, note="accept", artifact_id="a1",
            gate_outcome="approved_for_review",
        )
        self.assertTrue(record["approved"])
        self.assertEqual(record["artifact_id"], "a1")
        import json

        json.dumps(record)

    def test_approvable_outcomes_are_exactly_two(self):
        self.assertEqual(
            set(APPROVABLE_GATE_OUTCOMES),
            {"approved_for_review", "requires_user_decision"},
        )


class ApprovalApiTests(unittest.TestCase):
    def setUp(self):
        self.client = api_client(create_app())

    def test_approve_records_verdict_on_passing_gate(self):
        saved = {}

        async def fake_save(session_id, *, field, record):
            saved["args"] = (session_id, field, record)

        with (
            patch.object(
                main_module, "load_design_snapshot",
                return_value=_snapshot(
                    _gate(QualityGateOutcome.APPROVED_FOR_REVIEW)
                ),
            ),
            patch.object(main_module, "save_session_record", fake_save),
        ):
            response = self.client.post(
                "/design/sessions/design-s1/approve-design",
                json={"approved": True, "note": "accept trade-offs"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["approved"])
        self.assertEqual(body["note"], "accept trade-offs")
        self.assertEqual(body["artifact_id"], "a1")
        self.assertEqual(body["gate_outcome"], "approved_for_review")
        self.assertEqual(saved["args"][1], "design_approval")
        self.assertTrue(saved["args"][2]["approved"])

    def test_approve_refused_without_passing_gate(self):
        with patch.object(
            main_module, "load_design_snapshot",
            return_value=_snapshot(
                _gate(QualityGateOutcome.REQUIRES_REVISION)
            ),
        ):
            response = self.client.post(
                "/design/sessions/design-s1/approve-design",
                json={"approved": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "approval_not_allowed"
        )

    def test_critique_endpoint_serves_stored_critique(self):
        from ai_architect.domain.architecture.critique import DesignCritique

        critique = DesignCritique(
            artifact_id="c1",
            session_id="design-s1",
            reviewed_artifact_id="a1",
            reviewed_version=1,
            findings=[],
            summary="clean",
        )
        snapshot = _snapshot(_gate(QualityGateOutcome.APPROVED_FOR_REVIEW))
        snapshot.critique = critique
        with patch.object(
            main_module, "load_design_snapshot", return_value=snapshot
        ):
            response = self.client.get(
                "/design/sessions/design-s1/critique"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["artifact_id"], "c1")

    def test_critique_missing_is_404(self):
        with patch.object(
            main_module, "load_design_snapshot",
            return_value=_snapshot(
                _gate(QualityGateOutcome.APPROVED_FOR_REVIEW)
            ),
        ):
            response = self.client.get(
                "/design/sessions/design-s1/critique"
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"]["code"], "critique_not_available"
        )

    def test_quality_gate_endpoint_serves_stored_verdict(self):
        with patch.object(
            main_module, "load_design_snapshot",
            return_value=_snapshot(
                _gate(QualityGateOutcome.APPROVED_FOR_REVIEW)
            ),
        ):
            response = self.client.get(
                "/design/sessions/design-s1/quality-gate"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["outcome"], "approved_for_review"
        )


class ApprovalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        from ai_architect.infrastructure.checkpoint import sqlite as ckpt

        self._orig = ckpt.CHECKPOINT_DB_PATH
        ckpt.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self._ckpt = ckpt

    def tearDown(self):
        self._ckpt.CHECKPOINT_DB_PATH = self._orig
        self._tmp.cleanup()

    def test_save_and_reload_approval_record(self):
        import asyncio

        from ai_architect.infrastructure.checkpoint.design import (
            load_design_snapshot,
            save_design_run_marker,
            save_session_record,
        )

        async def _run():
            await save_design_run_marker(
                "design-s1", status="ready", user_request="build it"
            )
            await save_session_record(
                "design-s1",
                field="design_approval",
                record=build_design_approval(
                    approved=True, note="ok", artifact_id="a1",
                    gate_outcome="approved_for_review",
                ),
            )
            return await load_design_snapshot("design-s1")

        snapshot = asyncio.run(_run())
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.design_approval["approved"])
        self.assertEqual(snapshot.design_approval["artifact_id"], "a1")

    def test_save_on_unknown_session_fails_loudly(self):
        import asyncio

        from ai_architect.infrastructure.checkpoint.design import (
            PersistedStateError,
            save_session_record,
        )

        async def _run():
            await save_session_record(
                "nope", field="design_approval", record={"approved": True}
            )

        with self.assertRaises(PersistedStateError):
            asyncio.run(_run())


class ApprovalCliTests(unittest.TestCase):
    def test_parser_wires_approve_subcommand(self):
        from ai_architect.cli.main import build_parser

        args = build_parser().parse_args(
            ["approve", "--session-id", "design-s1", "--note", "ok"]
        )
        self.assertEqual(args.command, "approve")
        self.assertEqual(args.session_id, "design-s1")
        self.assertEqual(args.note, "ok")
        self.assertFalse(args.yes)

    def test_approve_command_records_with_yes_flag(self):
        import asyncio

        from ai_architect.cli import approve as approve_cli

        snapshot = _snapshot(_gate(QualityGateOutcome.APPROVED_FOR_REVIEW))
        snapshot.user_request = "build it"
        snapshot.requirements_state = object()
        snapshot.cost_result = None
        snapshot.critique = None
        snapshot.architecture = SimpleNamespace(
            artifact_id="a1", version=1, nodes=[], edges=[], decisions=[],
            assumptions=[], warnings=[],
        )
        saved = {}

        async def fake_save(session_id, *, field, record):
            saved["record"] = record

        async def fake_load(session_id):
            return snapshot

        args = SimpleNamespace(
            session_id="design-s1", note="ship it", yes=True
        )
        with (
            patch.object(approve_cli, "load_design_snapshot", fake_load),
            patch.object(approve_cli, "save_session_record", fake_save),
        ):
            code = asyncio.run(approve_cli._approve_async(args))
        self.assertEqual(code, 0)
        self.assertTrue(saved["record"]["approved"])

    def test_approve_command_refuses_without_gate(self):
        import asyncio

        from ai_architect.cli import approve as approve_cli

        async def fake_load(session_id):
            return _snapshot(_gate(QualityGateOutcome.BLOCKED))

        args = SimpleNamespace(
            session_id="design-s1", note="", yes=True
        )
        with patch.object(
            approve_cli, "load_design_snapshot", fake_load
        ):
            code = asyncio.run(approve_cli._approve_async(args))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
