"""API boundary for assisted provisioning-input suggestions.

POST /design/sessions/{session_id}/provisioning/suggest-inputs returns
advisory values for a stored plan's pending human decisions. The
endpoint must:

1. refuse a session with no stored Terraform plan (there is nothing to
   suggest for), rather than guessing;
2. return the suggestions and the skipped inputs with their reasons;
3. change nothing: the stored plan is untouched and no value is
   applied to it.

The suggestion service itself is patched here; its behaviour is covered
by tests/test_provisioning_input_suggestions.py. No LLM, AWS or
network access.

Run: python -m unittest discover -s tests
"""
import asyncio
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.api_auth import api_client

from ai_architect.api.main import create_app
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from ai_architect.infrastructure.checkpoint.design import (
    load_design_snapshot,
    save_design_run_marker,
    save_session_record,
)
from ai_architect.services.provisioning.input_suggestions import (
    InputSuggestionResult,
    InputSuggestion,
    SkippedInput,
)
from ai_architect.services.requirements.validator import RequirementsValidator


def _requirements_state(session_id: str) -> RequirementsState:
    extraction = RequirementsExtraction(
        application_description="a multi-tenant clinic platform",
        cloud_provider="aws",
        region="us-east-1",
        target_users_dau="40000",
        storage_characteristics="referral PDFs",
        retention="seven years",
        availability="99.9%",
        budget_constraints="$450 per month",
    )
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=session_id,
        original_request="design a clinic platform",
        extraction=extraction,
        validation_result=validation,
    )


def _plan() -> dict:
    return {
        "status": "incomplete",
        "artifact_id": "artifact-1",
        "spec_id": "spec-1",
        "tree_hash": "tree-1",
        "provider": "aws",
        "region_code": "us-east-1",
        "resources": [
            {
                "resource_id": "s3-amazon-s3",
                "node_id": "static",
                "kind": "s3",
                "component_name": "Amazon S3",
                "module": None,
                "pending_inputs": [
                    {"name": "bucket_name", "reason": "needs a human decision"},
                    {"name": "versioning", "reason": "needs a human decision"},
                ],
            },
        ],
        "modules": [],
        "selection_errors": [],
        "spec_diagnostics": [],
        "render_status": "unrepresentable",
        "render_diagnostics": [],
        "files": {},
        "resource_types": [],
        "terraform_validation": {
            "status": "not_run",
            "installed": False,
            "version": None,
            "exit_codes": {},
            "diagnostics": [],
        },
        "inventory": {
            "analysis": "static_inventory",
            "creates": 0,
            "destroys": None,
            "uncovered": 1,
            "by_kind": {"s3": 1},
            "note": "static inventory",
        },
        "spec_warnings": [],
        "artifacts": [],
        "generated": [],
    }


class SuggestInputsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self.client = api_client(create_app())
        self.session_id = str(uuid.uuid4())

    def tearDown(self):
        ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()

    def _session_with_plan(self) -> str:
        asyncio.run(save_design_run_marker(
            self.session_id,
            status="ready",
            user_request="design a clinic platform",
            requirements_state=_requirements_state(str(uuid.uuid4())),
        ))
        asyncio.run(save_session_record(
            self.session_id,
            field="terraform_artifact",
            record=_plan(),
        ))
        return self.session_id

    def test_refuses_a_session_without_a_stored_plan(self):
        asyncio.run(save_design_run_marker(
            self.session_id,
            status="ready",
            user_request="design a clinic platform",
            requirements_state=_requirements_state(str(uuid.uuid4())),
        ))
        response = self.client.post(
            f"/design/sessions/{self.session_id}/provisioning/suggest-inputs"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "terraform_not_available"
        )

    def test_returns_suggestions_and_skipped_inputs(self):
        self._session_with_plan()
        result = InputSuggestionResult(
            suggestions=(
                InputSuggestion(
                    resource_id="s3-amazon-s3",
                    name="versioning",
                    value="enabled",
                    rationale="Recoverable uploads.",
                    source="ai_suggestion",
                ),
            ),
            skipped=(
                SkippedInput(
                    resource_id="s3-amazon-s3",
                    name="bucket_name",
                    reason="A bucket name is globally unique in S3.",
                ),
            ),
            notes=("note",),
        )
        with patch(
            "ai_architect.services.provisioning.input_suggestions."
            "suggest_pending_inputs",
            return_value=result,
        ) as suggest:
            response = self.client.post(
                f"/design/sessions/{self.session_id}"
                "/provisioning/suggest-inputs"
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["suggestions"]), 1)
        self.assertEqual(body["suggestions"][0]["value"], "enabled")
        self.assertEqual(body["suggestions"][0]["source"], "ai_suggestion")
        self.assertEqual(len(body["skipped"]), 1)
        self.assertIn("globally unique", body["skipped"][0]["reason"])
        self.assertEqual(body["notes"], ["note"])
        # The pending inputs reached the service, and the artifact was
        # not written to.
        pending = suggest.call_args.kwargs["pending"]
        self.assertEqual(
            sorted((item.resource_id, item.name) for item in pending),
            [
                ("s3-amazon-s3", "bucket_name"),
                ("s3-amazon-s3", "versioning"),
            ],
        )
        snapshot = asyncio.run(load_design_snapshot(self.session_id))
        self.assertEqual(snapshot.terraform_artifact, _plan())

    def test_a_service_failure_is_reported_as_a_conflict(self):
        self._session_with_plan()
        from ai_architect.services.provisioning.input_suggestions import (
            SuggestionError,
        )

        with patch(
            "ai_architect.services.provisioning.input_suggestions."
            "suggest_pending_inputs",
            side_effect=SuggestionError("model unavailable"),
        ):
            response = self.client.post(
                f"/design/sessions/{self.session_id}"
                "/provisioning/suggest-inputs"
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"], "input_suggestions_unavailable"
        )


if __name__ == "__main__":
    unittest.main()
