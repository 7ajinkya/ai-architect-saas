"""Read-only session listing: GET /design/sessions.

Covers the recent-designs contract:
  1. no database yet -> empty list (never an error);
  2. requirements-only and design threads each appear once with
     truthful fields, most recently active first;
  3. a thread whose stored state fails validation is skipped rather
     than failing the whole listing.

Persisted state is written through the real checkpointer into an
isolated temp database, so loader selection logic is exercised
rather than mocked. No LLM calls.

Run: poetry run python -m unittest discover tests
"""
import os
import tempfile
import unittest
import uuid

from tests.api_auth import api_client

from ai_architect.api.main import create_app
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from ai_architect.infrastructure.checkpoint.design import save_design_run_marker
from ai_architect.infrastructure.checkpoint.requirements import (
    save_requirements_state,
)
from ai_architect.services.requirements.validator import RequirementsValidator


def _extraction() -> RequirementsExtraction:
    return RequirementsExtraction(
        application_description="generic multi-tier workload",
        cloud_provider="aws",
        region="us-east-1",
        target_users_dau="10000",
        storage_characteristics="durable block storage",
        retention="90 days",
        availability="99.9%",
    )


def _requirements_state(session_id: str, request: str) -> RequirementsState:
    extraction = _extraction()
    validation = RequirementsValidator().validate(extraction)
    return RequirementsState.from_extraction(
        session_id=session_id,
        original_request=request,
        extraction=extraction,
        validation_result=validation,
    )


class SessionListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self.client = api_client(create_app())

    def tearDown(self):
        ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()

    def test_empty_store_lists_nothing(self):
        response = self.client.get("/design/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_requirements_and_design_threads_listed_recent_first(self):
        import asyncio

        req_id = str(uuid.uuid4())
        asyncio.run(
            save_requirements_state(
                _requirements_state(req_id, "design a generic workload")
            )
        )
        design_id = str(uuid.uuid4())
        asyncio.run(
            save_design_run_marker(
                design_id,
                status="failed",
                user_request="design another workload",
                requirements_state=_requirements_state(
                    str(uuid.uuid4()), "design another workload"
                ),
                error="boom",
            )
        )

        response = self.client.get("/design/sessions")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 2)
        # Most recently written thread first.
        self.assertEqual(body[0]["session_id"], design_id)
        self.assertEqual(body[0]["user_request"], "design another workload")
        # A bare run marker carries no stage artifacts yet; the failure
        # surfaces through design_status/has_error instead.
        self.assertEqual(body[0]["design_status"], "failed")
        self.assertTrue(body[0]["has_error"])
        self.assertEqual(body[1]["session_id"], req_id)
        self.assertEqual(body[1]["user_request"], "design a generic workload")
        self.assertFalse(body[1]["has_design_run"])
        self.assertFalse(body[1]["has_error"])
        self.assertEqual(body[1]["requirements_status"], "ready")

    def test_corrupt_thread_is_skipped_not_fatal(self):
        import asyncio
        from unittest.mock import patch

        from ai_architect.infrastructure.checkpoint import design as design_ckpt

        req_id = str(uuid.uuid4())
        asyncio.run(
            save_requirements_state(
                _requirements_state(req_id, "design a generic workload")
            )
        )
        real_loader = design_ckpt.load_design_snapshot

        async def flaky_loader(session_id: str):
            if session_id == req_id:
                from ai_architect.infrastructure.checkpoint.design import (
                    PersistedStateError,
                )

                raise PersistedStateError("stored state is invalid")
            return await real_loader(session_id)

        with patch.object(design_ckpt, "load_design_snapshot", flaky_loader):
            response = self.client.get("/design/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


if __name__ == "__main__":
    unittest.main()
