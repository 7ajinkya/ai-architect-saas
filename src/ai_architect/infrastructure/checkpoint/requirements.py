from typing import Any

from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint.sqlite import create_checkpointer


def _config(session_id: str) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": session_id,
            "checkpoint_ns": "",
        }
    }


async def save_requirements_state(
    state: RequirementsState,
) -> None:
    async with create_checkpointer() as checkpointer:
        config = _config(state.session_id)

        checkpoint = {
            "v": 1,
            "id": state.session_id,
            "ts": "",
            "channel_values": {
                "requirements_state": state.model_dump(mode="json"),
            },
            "channel_versions": {},
            "versions_seen": {},
            "updated_channels": ["requirements_state"],
        }

        metadata = {
            "source": "update",
            "step": 0,
            "writes": {
                "requirements_state": state.session_id,
            },
        }

        await checkpointer.aput(
            config,
            checkpoint,
            metadata,
            {},
        )


async def load_requirements_state(
    session_id: str,
) -> RequirementsState | None:
    async with create_checkpointer() as checkpointer:
        config = _config(session_id)

        checkpoint_tuple = await checkpointer.aget_tuple(config)

        if checkpoint_tuple is None:
            return None

        state_data = checkpoint_tuple.checkpoint[
            "channel_values"
        ].get("requirements_state")

        if state_data is None:
            return None

        return RequirementsState.model_validate(state_data)
