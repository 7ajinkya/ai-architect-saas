"""Slice 5 tests: design checkpoint reconstruction.

A terminal FAILED marker written after artifacts exist must not erase
them: reconstruction back-fills artifact channels from older checkpoints,
with the newest checkpoint authoritative for status/error/warnings, and
never inventing a value no checkpoint holds. Session-record writes are
serialized so concurrent saves cannot drop each other's fields.

Run: poetry run python -m unittest discover tests
"""
import asyncio
import os
import tempfile
import unittest

from ai_architect.agents.design.state import DesignStatus
from ai_architect.infrastructure.checkpoint import design as design_ckpt
from ai_architect.infrastructure.checkpoint import sqlite as ckpt_sqlite
from ai_architect.infrastructure.checkpoint.design import (
    load_design_snapshot,
    save_design_run_marker,
    save_session_record,
)
from ai_architect.infrastructure.checkpoint.sqlite import create_checkpointer


def _graph_checkpoint_id() -> str:
    """The id generator the design graph itself mints."""
    from langgraph.checkpoint.base.id import uuid6

    return str(uuid6())


async def _write_checkpoint(session_id: str, channels: dict) -> str:
    """Write a graph-style checkpoint (full channel_values snapshot)."""
    checkpoint_id = _graph_checkpoint_id()
    checkpoint = {
        "v": 4,
        "id": checkpoint_id,
        "ts": "",
        "channel_values": channels,
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": sorted(channels),
    }
    async with create_checkpointer() as checkpointer:
        await checkpointer.aput(
            {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "update", "step": 0, "writes": {}},
            {},
        )
    return checkpoint_id


def _channels(session_id: str, **overrides) -> dict:
    channels = {
        "session_id": session_id,
        "user_request": "design a generic pipeline",
        "requirements_state": None,
        "capacity_result": None,
        "component_selection": None,
        "cost_result": None,
        "architecture": None,
        "status": DesignStatus.READY.value,
        "error": None,
        "warnings": [],
    }
    channels.update(overrides)
    return channels


class _CheckpointFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = ckpt_sqlite.CHECKPOINT_DB_PATH
        ckpt_sqlite.CHECKPOINT_DB_PATH = os.path.join(
            self._tmp.name, "checkpoints.sqlite"
        )
        self.addCleanup(self._restore)

    def _restore(self):
        ckpt_sqlite.CHECKPOINT_DB_PATH = self._orig_path
        self._tmp.cleanup()


class FailedMarkerBackfillTests(_CheckpointFixture):
    """A FAILED marker must not erase artifacts an older checkpoint holds."""

    def test_01_failed_marker_keeps_earlier_channels_and_failed_status(self):
        session_id = "design-backfill-1"
        asyncio.run(
            _write_checkpoint(
                session_id,
                _channels(
                    session_id,
                    revision_count=2,
                    design_approval={"approved_by": "designer", "mode": "design"},
                    terraform_artifact={"tree_hash": "abc123", "mode": "plan"},
                ),
            )
        )
        asyncio.run(
            save_design_run_marker(
                session_id,
                status=DesignStatus.FAILED.value,
                user_request="design a generic pipeline",
                error="stage failed",
                after_graph=True,
            )
        )

        snapshot = asyncio.run(load_design_snapshot(session_id))

        # The newest checkpoint stays authoritative for run state...
        self.assertEqual(snapshot.status, DesignStatus.FAILED.value)
        self.assertEqual(snapshot.error, "stage failed")
        # ...while artifact channels survive from the older checkpoint.
        self.assertEqual(snapshot.revision_count, 2)
        self.assertEqual(snapshot.design_approval["approved_by"], "designer")
        self.assertEqual(snapshot.terraform_artifact["tree_hash"], "abc123")
        # Nothing ever produced these: they must stay None, not invented.
        self.assertIsNone(snapshot.capacity_result)
        self.assertIsNone(snapshot.architecture)

    def test_02_newest_non_none_value_never_lost_to_an_older_one(self):
        session_id = "design-backfill-2"
        asyncio.run(
            _write_checkpoint(
                session_id,
                _channels(
                    session_id,
                    revision_count=1,
                    design_approval={"version": "older"},
                ),
            )
        )
        asyncio.run(
            _write_checkpoint(
                session_id,
                _channels(
                    session_id,
                    revision_count=3,
                    design_approval={"version": "newer"},
                    terraform_artifact={"version": "newer"},
                ),
            )
        )

        snapshot = asyncio.run(load_design_snapshot(session_id))

        self.assertEqual(snapshot.revision_count, 3)
        self.assertEqual(snapshot.design_approval["version"], "newer")
        self.assertEqual(snapshot.terraform_artifact["version"], "newer")

    def test_03_lone_running_marker_invents_nothing(self):
        session_id = "design-backfill-3"
        asyncio.run(
            save_design_run_marker(
                session_id,
                status=DesignStatus.RUNNING.value,
                user_request="design a generic pipeline",
            )
        )

        snapshot = asyncio.run(load_design_snapshot(session_id))

        self.assertEqual(snapshot.status, DesignStatus.RUNNING.value)
        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.revision_count, 0)
        self.assertIsNone(snapshot.capacity_result)
        self.assertIsNone(snapshot.component_selection)
        self.assertIsNone(snapshot.cost_result)
        self.assertIsNone(snapshot.architecture)
        self.assertIsNone(snapshot.design_approval)
        self.assertIsNone(snapshot.terraform_artifact)
        self.assertIsNone(snapshot.provisioning_approval)
        self.assertIsNone(snapshot.provisioning_result)


class SessionRecordConcurrencyTests(_CheckpointFixture):
    """Concurrent record saves merge instead of overwriting each other."""

    def _restore(self):
        super()._restore()
        # The record lock may have been contended on this test's event
        # loop; rebind it so later suites start on a clean lock.
        design_ckpt._SESSION_RECORD_LOCK = asyncio.Lock()

    def test_01_concurrent_saves_merge_without_losing_fields(self):
        session_id = "design-concurrent-saves"
        asyncio.run(
            save_design_run_marker(
                session_id,
                status=DesignStatus.RUNNING.value,
                user_request="design a generic pipeline",
            )
        )

        async def scenario():
            await asyncio.gather(
                save_session_record(
                    session_id,
                    field="design_approval",
                    record={"approved_by": "designer"},
                ),
                save_session_record(
                    session_id,
                    field="provisioning_approval",
                    record={"approved_by": "operator"},
                ),
            )

        asyncio.run(scenario())

        snapshot = asyncio.run(load_design_snapshot(session_id))
        self.assertEqual(snapshot.design_approval["approved_by"], "designer")
        self.assertEqual(
            snapshot.provisioning_approval["approved_by"], "operator"
        )
        self.assertEqual(snapshot.status, DesignStatus.RUNNING.value)

    def test_02_sequential_saves_accumulate_on_top_of_concurrency_result(self):
        session_id = "design-sequential-saves"
        asyncio.run(
            save_design_run_marker(
                session_id,
                status=DesignStatus.RUNNING.value,
                user_request="design a generic pipeline",
            )
        )

        async def scenario():
            await save_session_record(
                session_id,
                field="design_approval",
                record={"approved_by": "designer"},
            )
            await save_session_record(
                session_id,
                field="provisioning_approval",
                record={"approved_by": "operator"},
            )

        asyncio.run(scenario())

        snapshot = asyncio.run(load_design_snapshot(session_id))
        self.assertEqual(snapshot.design_approval["approved_by"], "designer")
        self.assertEqual(
            snapshot.provisioning_approval["approved_by"], "operator"
        )


if __name__ == "__main__":
    unittest.main()
