"""Session browser: search + pagination over GET /design/sessions.

Covers:
  1. empty history -> []
  2. pagination (limit/offset, bounded page)
  3. query filtering over user_request (case-insensitive)
  4. session-id substring matching (partial UUID)
  5. combined query + pagination
  6. corrupt-thread handling (skipped, not fatal)
  7. opening a selected session via GET /design/sessions/{id}

Persisted state is written through the real checkpointer into an
isolated temp database. No LLM calls.
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


class SessionSearchPaginationTests(unittest.TestCase):
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

    def _seed(self, requests: list[str]) -> list[str]:
        import asyncio

        ids: list[str] = []
        for text in requests:
            sid = str(uuid.uuid4())
            asyncio.run(
                save_requirements_state(_requirements_state(sid, text))
            )
            ids.append(sid)
        return ids

    def test_empty_history(self):
        response = self.client.get("/design/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_pagination_bounded_pages(self):
        self._seed([f"workload {i}" for i in range(5)])
        page1 = self.client.get("/design/sessions?limit=2&offset=0")
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(len(page1.json()), 2)
        page2 = self.client.get("/design/sessions?limit=2&offset=2")
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.json()), 2)
        page3 = self.client.get("/design/sessions?limit=2&offset=4")
        self.assertEqual(page3.status_code, 200)
        self.assertEqual(len(page3.json()), 1)
        # Pages do not overlap.
        ids1 = {r["session_id"] for r in page1.json()}
        ids2 = {r["session_id"] for r in page2.json()}
        ids3 = {r["session_id"] for r in page3.json()}
        self.assertEqual(len(ids1 | ids2 | ids3), 5)

    def test_query_filters_request_text_case_insensitive(self):
        self._seed(
            [
                "IoT telemetry pipeline",
                "ecommerce checkout service",
                "iot sensor ingestion",
            ]
        )
        response = self.client.get("/design/sessions?query=iot")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 2)
        for row in body:
            self.assertIn("iot", row["user_request"].lower())

    def test_query_matches_partial_session_id(self):
        ids = self._seed(["alpha workload", "beta workload"])
        partial = ids[0][:8]
        response = self.client.get(f"/design/sessions?query={partial}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(any(r["session_id"] == ids[0] for r in body))

    def test_combined_query_and_pagination(self):
        self._seed([f"IoT device fleet {i}" for i in range(4)] + ["other"])
        page1 = self.client.get("/design/sessions?query=iot&limit=2&offset=0")
        page2 = self.client.get("/design/sessions?query=iot&limit=2&offset=2")
        self.assertEqual(len(page1.json()), 2)
        self.assertEqual(len(page2.json()), 2)
        ids1 = {r["session_id"] for r in page1.json()}
        ids2 = {r["session_id"] for r in page2.json()}
        self.assertEqual(len(ids1 & ids2), 0)

    def test_corrupt_thread_skipped_with_query(self):
        import asyncio
        from unittest.mock import patch

        from ai_architect.infrastructure.checkpoint import design as design_ckpt

        good_id = self._seed(["IoT healthy design"])[0]
        real_loader = design_ckpt.load_design_snapshot

        async def flaky_loader(session_id: str):
            if session_id == good_id:
                from ai_architect.infrastructure.checkpoint.design import (
                    PersistedStateError,
                )

                raise PersistedStateError("stored state is invalid")
            return await real_loader(session_id)

        with patch.object(design_ckpt, "load_design_snapshot", flaky_loader):
            response = self.client.get("/design/sessions?query=iot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_open_selected_session(self):
        ids = self._seed(["open me: url shortener"])
        response = self.client.get(f"/design/sessions/{ids[0]}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], ids[0])
        self.assertIn("url shortener", body["user_request"])


if __name__ == "__main__":
    unittest.main()
