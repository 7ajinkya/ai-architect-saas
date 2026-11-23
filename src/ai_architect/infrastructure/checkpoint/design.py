"""Read-only loader for persisted design-session state.

Design-session identity
-----------------------
The design-session id IS the design graph's existing LangGraph
``thread_id`` (see ``agents.design.run.run_design_workflow``). This
module therefore introduces no new identity, no new database and no new
artifact: it reads the same checkpoints the design workflow already
writes through the same ``create_checkpointer()``.

Why a loader at all: the workflow can persist state, but nothing could
read the composed artifacts back out as typed domain objects. This is
pure deserialization plus checkpoint selection; no domain logic lives
here.

Checkpoint selection
--------------------
A design thread and a requirements thread can share one
``(thread_id, checkpoint_ns)`` key because both default to namespace
``""``. ``aget_tuple`` orders by ``checkpoint_id`` descending, so a
synthetic requirements checkpoint could mask a composed design
checkpoint. To stay correct regardless of write order, this loader
prefers the newest checkpoint that actually carries design channels and
only falls back to a requirements-only checkpoint when the session never
ran past requirements.
"""

from contextlib import aclosing
from datetime import datetime
from typing import Any, Optional
import asyncio
import os
import time
import uuid

import aiosqlite
from pydantic import BaseModel, ConfigDict, ValidationError

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    QualityGateResult,
)
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint import sqlite as _sqlite_module
from ai_architect.infrastructure.checkpoint.sqlite import create_checkpointer

# Channels only the design graph writes. Their presence identifies a
# composed design checkpoint.
DESIGN_CHANNELS = frozenset(
    {
        "capacity_result",
        "component_selection",
        "cost_result",
        "architecture",
        "critique",
        "quality_gate",
        "status",
    }
)

# A single design run writes a handful of checkpoints; this bound only
# guards against pathological threads.
_MAX_CHECKPOINTS = 100

# Channels a newer checkpoint may carry as None (or omit) while an older
# checkpoint still holds the produced value: a terminal FAILED marker
# written after a run produced artifacts blanks the four stage channels,
# and marker checkpoints never carry the critique/gate/record channels.
# Reconstruction back-fills these channel-by-channel from older
# checkpoints (bounded by _MAX_CHECKPOINTS), while status/error/warnings
# always come from the newest checkpoint so a failure stays visible.
_BACKFILL_CHANNELS = (
    "requirements_state",
    "capacity_result",
    "component_selection",
    "cost_result",
    "architecture",
    "critique",
    "revision_count",
    "quality_gate",
    "design_approval",
    "terraform_artifact",
    "provisioning_approval",
    "provisioning_result",
    "optimization_recommendations",
)

# Serializes the read-merge-write in save_session_record: two concurrent
# saves (e.g. an approval landing while a provisioning record is stored)
# would otherwise both read the same base checkpoint and the later write
# would silently drop the earlier record. asyncio.Lock, never a thread
# lock: everything runs on the event loop, and a threading lock would
# deadlock event loops reused across requests/tests.
_SESSION_RECORD_LOCK = asyncio.Lock()

# Upper bound on threads surfaced by session listing. Listing is a
# convenience read path, not pagination infrastructure.
_MAX_LISTED_SESSIONS = 50

# Default page size for the session browser. The UI requests explicit
# pages (limit/offset); this default keeps unbounded callers bounded.
DEFAULT_SESSION_PAGE_SIZE = 10

# Upper bound on threads scanned server-side for one filtered/paged
# listing. The browser never receives more than one page; the server
# scans at most this many threads in recency order to fill it.
_MAX_SCAN_THREADS = 500


class PersistedStateError(ValueError):
    """Stored state could not be reconstructed safely."""


class DesignSessionSnapshot(BaseModel):
    """Typed view of one persisted design session.

    Mirrors ``DesignGraphState`` channels without being the graph's own
    state object: it exists so read paths can carry validated artifacts
    instead of raw checkpoint dicts.
    """

    session_id: str
    user_request: str = ""
    requirements_state: Optional[RequirementsState] = None
    capacity_result: Optional[CapacityModelingResult] = None
    component_selection: Optional[ComponentSelectionResult] = None
    cost_result: Optional[CostEstimationResult] = None
    architecture: Optional[ArchitectureResult] = None
    critique: Optional[DesignCritique] = None
    revision_count: int = 0
    quality_gate: Optional[QualityGateResult] = None
    design_approval: Optional[dict] = None
    terraform_artifact: Optional[dict] = None
    provisioning_approval: Optional[dict] = None
    provisioning_result: Optional[dict] = None
    optimization_recommendations: Optional[dict] = None
    status: str = ""
    error: Optional[str] = None
    warnings: list[str] = []

    model_config = ConfigDict(extra="forbid")

    @property
    def has_design_run(self) -> bool:
        """True when any downstream stage artifact was persisted."""
        return any(
            item is not None
            for item in (
                self.capacity_result,
                self.component_selection,
                self.cost_result,
                self.architecture,
            )
        )


def _config(session_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": session_id,
            "checkpoint_ns": "",
        }
    }


def _rebuild_count(value: Any, field: str) -> int:
    """Reconstruct a bounded counter, failing loudly rather than guessing."""
    if value is None:
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise PersistedStateError(
            f"Stored {field} is invalid and cannot be reconstructed: {exc}"
        ) from exc
    if count < 0:
        raise PersistedStateError(
            f"Stored {field} is invalid and cannot be reconstructed: "
            f"negative counter {count}."
        )
    return count


def _rebuild_record(value: Any, field: str) -> dict | None:
    """Reconstruct a plain-JSON approval record, failing loudly."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PersistedStateError(
            f"Stored {field} is invalid and cannot be reconstructed: "
            f"expected an object, got {type(value).__name__}."
        )
    return {str(key): item for key, item in value.items()}


def _rebuild(model: type, value: Any, field: str):
    """Reconstruct one artifact, failing loudly rather than half-loading."""
    if value is None:
        return None
    try:
        if model is RequirementsState:
            return RequirementsState.ensure(value)
        return model.model_validate(value)
    except (ValidationError, ValueError, TypeError) as exc:
        raise PersistedStateError(
            f"Stored {field} is invalid and cannot be reconstructed: {exc}"
        ) from exc


def _snapshot_from_channels(
    session_id: str, channels: dict[str, Any]
) -> DesignSessionSnapshot:
    requirements_state = _rebuild(
        RequirementsState, channels.get("requirements_state"), "requirements_state"
    )
    if requirements_state is not None:
        user_request = requirements_state.original_request
    else:
        user_request = channels.get("user_request") or ""

    warnings = channels.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]

    return DesignSessionSnapshot(
        session_id=session_id,
        user_request=str(user_request or ""),
        requirements_state=requirements_state,
        capacity_result=_rebuild(
            CapacityModelingResult,
            channels.get("capacity_result"),
            "capacity_result",
        ),
        component_selection=_rebuild(
            ComponentSelectionResult,
            channels.get("component_selection"),
            "component_selection",
        ),
        cost_result=_rebuild(
            CostEstimationResult, channels.get("cost_result"), "cost_result"
        ),
        architecture=_rebuild(
            ArchitectureResult, channels.get("architecture"), "architecture"
        ),
        critique=_rebuild(
            DesignCritique, channels.get("critique"), "critique"
        ),
        revision_count=_rebuild_count(
            channels.get("revision_count"), "revision_count"
        ),
        quality_gate=_rebuild(
            QualityGateResult, channels.get("quality_gate"), "quality_gate"
        ),
        design_approval=_rebuild_record(
            channels.get("design_approval"), "design_approval"
        ),
        terraform_artifact=_rebuild_record(
            channels.get("terraform_artifact"), "terraform_artifact"
        ),
        provisioning_approval=_rebuild_record(
            channels.get("provisioning_approval"), "provisioning_approval"
        ),
        provisioning_result=_rebuild_record(
            channels.get("provisioning_result"), "provisioning_result"
        ),
        optimization_recommendations=_rebuild_record(
            channels.get("optimization_recommendations"),
            "optimization_recommendations",
        ),
        # Deliberately NOT defaulted from the requirements status: a
        # session that only ran requirements has no design-run status, and
        # reporting one would claim a design outcome that never happened.
        status=str(channels.get("status") or ""),
        error=channels.get("error"),
        warnings=[str(w) for w in warnings],
    )


async def load_design_snapshot(
    session_id: str,
) -> Optional[DesignSessionSnapshot]:
    """Load the newest usable checkpoint for a design session.

    Returns ``None`` when the session id has no persisted state at all.
    Raises ``PersistedStateError`` when state exists but cannot be
    validated, so a corrupt artifact surfaces as a clear API error
    instead of a silently empty design.
    """
    if not session_id or not session_id.strip():
        return None

    async with create_checkpointer() as checkpointer:
        design_channels: Optional[dict[str, Any]] = None
        requirements_channels: Optional[dict[str, Any]] = None

        async with aclosing(
            checkpointer.alist(_config(session_id), limit=_MAX_CHECKPOINTS)
        ) as stream:
            async for checkpoint_tuple in stream:
                channels = (
                    (checkpoint_tuple.checkpoint or {}).get("channel_values") or {}
                )
                if not channels:
                    continue
                if requirements_channels is None and (
                    "requirements_state" in channels
                ):
                    requirements_channels = channels
                if not (DESIGN_CHANNELS & set(channels)):
                    continue
                if design_channels is None:
                    # Newest design checkpoint: authoritative for status,
                    # error, warnings, and user_request.
                    design_channels = dict(channels)
                    continue
                for channel in _BACKFILL_CHANNELS:
                    if (
                        design_channels.get(channel) is None
                        and channels.get(channel) is not None
                    ):
                        design_channels[channel] = channels[channel]
                if all(
                    design_channels.get(channel) is not None
                    for channel in _BACKFILL_CHANNELS
                ):
                    break

    chosen = design_channels or requirements_channels
    if chosen is None:
        return None

    return _snapshot_from_channels(session_id, chosen)


async def list_session_threads(
    limit: int = _MAX_LISTED_SESSIONS,
    offset: int = 0,
) -> list[str]:
    """Thread ids with persisted checkpoints, most recently written first.

    Read-only: a raw sqlite query with no deserialization. Returns []
    when no checkpoint database exists yet. Ordering is best-effort
    recency (rowid insertion order in this append-only usage); no
    timestamps are stored anywhere, so none can be reported.
    """
    # Read through the module so tests (and embedders) that redirect
    # CHECKPOINT_DB_PATH keep working; a by-value import would freeze it.
    db_path = _sqlite_module.CHECKPOINT_DB_PATH
    if not os.path.exists(db_path):
        return []
    limit = max(1, min(int(limit), _MAX_SCAN_THREADS))
    offset = max(0, int(offset))
    async with aiosqlite.connect(db_path) as conn:
        try:
            cursor = await conn.execute(
                "SELECT thread_id FROM checkpoints "
                "GROUP BY thread_id ORDER BY MAX(rowid) DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
        except Exception:
            return []
    return [row[0] for row in rows if row and row[0]]


def _matches_query(snapshot: "DesignSessionSnapshot", query: str) -> bool:
    """Case-insensitive substring match over request text + session id.

    The persisted user request remains the human-readable identity; no
    separate name field exists. A partial UUID also matches.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return True
    if needle in (snapshot.session_id or "").lower():
        return True
    return needle in (snapshot.user_request or "").lower()


async def list_design_sessions(
    limit: int = DEFAULT_SESSION_PAGE_SIZE,
    offset: int = 0,
    query: str | None = None,
) -> list["DesignSessionSnapshot"]:
    """Newest usable snapshot per persisted thread, most recent first.

    Server-side filtering + pagination: at most ``limit`` snapshots are
    returned and the browser never receives the full history. Threads
    with no loadable state, or whose stored state fails validation, are
    skipped rather than failing the whole listing: one corrupt thread
    must not hide every other session.
    """
    limit = max(1, min(int(limit), _MAX_LISTED_SESSIONS))
    offset = max(0, int(offset))
    needle = (query or "").strip()
    snapshots: list[DesignSessionSnapshot] = []
    skipped = 0
    # Scan threads in recency order, loading lazily and stopping as soon
    # as the requested page is full. Bounded by _MAX_SCAN_THREADS so a
    # large store cannot force unbounded checkpoint reads.
    for thread_id in await list_session_threads(_MAX_SCAN_THREADS):
        try:
            snapshot = await load_design_snapshot(thread_id)
        except PersistedStateError:
            continue
        if snapshot is None or snapshot.requirements_state is None:
            continue
        if needle and not _matches_query(snapshot, needle):
            continue
        if skipped < offset:
            skipped += 1
            continue
        snapshots.append(snapshot)
        if len(snapshots) >= limit:
            break
    return snapshots


# LangGraph selects the newest checkpoint by ordering checkpoint ids as
# text, and it mints time-ordered UUIDv6 ids, so those ids sort by the
# time they were created. A marker written *before* the graph runs must
# therefore carry an earlier timestamp than any id the graph will mint,
# and a marker written *after* a finished run must carry a later one.
#
# The layout below mirrors langgraph.checkpoint.base.id.uuid6, including
# its 1582-epoch offset, because a marker must be a valid UUIDv6: an
# arbitrary string id makes LangGraph fail to resume the thread
# (binascii "Odd-length string" while reading pending writes).
_UUID6_EPOCH_OFFSET_100NS = 0x01B21DD213814000
_MARKER_SHIFT_100NS = 10_000_000  # one second


def marker_checkpoint_id(*, after_graph: bool) -> str:
    """Mint a UUIDv6 that sorts before or after same-thread graph ids.

    The version and variant bits are written into the integer directly
    rather than passed to ``uuid.UUID(version=...)``: CPython 3.12 only
    accepts versions 1-5 there, while UUIDv6 is exactly what LangGraph
    uses for checkpoint ids.
    """
    now_100ns = time.time_ns() // 100 + _UUID6_EPOCH_OFFSET_100NS
    timestamp = (
        now_100ns + _MARKER_SHIFT_100NS
        if after_graph
        else now_100ns - _MARKER_SHIFT_100NS
    )

    time_high_and_time_mid = (timestamp >> 12) & 0xFFFFFFFFFFFF
    time_low_and_version = timestamp & 0x0FFF
    node = int.from_bytes(os.urandom(6), "big") & 0xFFFFFFFFFFFF

    # Bit layout of a UUIDv6 integer: version 6 occupies bits 76-79, the
    # variant (0b10) occupies bits 62-63, and the 14-bit clock sequence
    # sits in bits 48-61. The highest clock sequence is used so two
    # markers created in the same tick never tie.
    value = time_high_and_time_mid << 80
    value |= time_low_and_version << 64
    value |= 0x6 << 76
    value |= 0x8000 << 48
    value |= 0x0FFF << 48
    value |= node

    return str(uuid.UUID(int=value))


async def save_session_record(
    session_id: str,
    *,
    field: str,
    record: dict,
) -> None:
    """Persist one plain-JSON record channel onto a session thread.

    Reads the newest checkpoint carrying channel values, merges the
    single field, and writes a full checkpoint that sorts after the
    graph's own ids (same marker scheme as save_design_run_marker).
    Only plain-JSON records may be stored this way; typed artifacts
    keep their own loaders. Raises PersistedStateError when the
    session has no persisted state at all.
    """
    if not session_id or not session_id.strip():
        raise PersistedStateError(f"Unknown session: {session_id}")
    if not isinstance(record, dict):
        raise PersistedStateError(
            f"Cannot record {field}: expected a plain-JSON object."
        )

    # Serialized read-merge-write: concurrent record saves must not drop
    # each other's fields (last-writer-wins on the same field is fine;
    # losing a different field is not).
    async with _SESSION_RECORD_LOCK:
        async with create_checkpointer() as checkpointer:
            channels: dict[str, Any] | None = None
            async with aclosing(
                checkpointer.alist(_config(session_id), limit=_MAX_CHECKPOINTS)
            ) as stream:
                async for checkpoint_tuple in stream:
                    candidate = (
                        (checkpoint_tuple.checkpoint or {}).get("channel_values")
                        or {}
                    )
                    if candidate:
                        channels = dict(candidate)
                        break
            if channels is None:
                raise PersistedStateError(f"Unknown session: {session_id}")
            channels[field] = dict(record)

            checkpoint = {
                "v": 4,
                "id": marker_checkpoint_id(after_graph=True),
                "ts": datetime.utcnow().isoformat(),
                "channel_values": channels,
                "channel_versions": {},
                "versions_seen": {},
                "updated_channels": sorted(channels),
            }
            await checkpointer.aput(
                _config(session_id),
                checkpoint,
                {"source": "update", "step": 0, "writes": {}},
                {},
            )


async def save_design_run_marker(
    session_id: str,
    *,
    status: str,
    user_request: str = "",
    requirements_state: Optional[RequirementsState] = None,
    error: Optional[str] = None,
    after_graph: bool = False,
) -> None:
    """Persist a minimal design checkpoint for a run that has not
    produced artifacts yet (started, or failed before any stage wrote).

    This writes only the design graph's own channels; it stores no
    derived architecture data and invents no result. It exists so a
    caller polling the session endpoint sees a truthful RUNNING or
    FAILED state instead of a 404 while the workflow is still working.
    """
    channels: dict[str, Any] = {
        "session_id": session_id,
        "user_request": user_request,
        "requirements_state": (
            requirements_state.model_dump(mode="json")
            if requirements_state is not None
            else None
        ),
        "capacity_result": None,
        "component_selection": None,
        "cost_result": None,
        "architecture": None,
        "status": status,
        "error": error,
        "warnings": [],
    }

    checkpoint = {
        "v": 4,
        "id": marker_checkpoint_id(after_graph=after_graph),
        "ts": datetime.utcnow().isoformat(),
        "channel_values": channels,
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": sorted(channels),
    }

    async with create_checkpointer() as checkpointer:
        await checkpointer.aput(
            _config(session_id),
            checkpoint,
            {"source": "update", "step": 0, "writes": {}},
            {},
        )
