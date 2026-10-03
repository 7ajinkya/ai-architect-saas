import asyncio

from ai_architect.infrastructure.checkpoint.requirements import (
    load_requirements_state,
    save_requirements_state,
)


SESSION_ID = "e83f05b4-1dd5-49d3-a472-70229dbc4094"


async def main() -> None:
    state = await load_requirements_state(SESSION_ID)

    if state is None:
        raise RuntimeError("Session not found")

    print("Before:")
    print(state.clarification_items[0].model_dump())

    state.answer_clarification(
        state.clarification_items[0].id,
        "AWS",
    )

    await save_requirements_state(state)

    restored = await load_requirements_state(SESSION_ID)

    if restored is None:
        raise RuntimeError("Session disappeared")

    print("\nAfter:")
    print(restored.clarification_items[0].model_dump())


if __name__ == "__main__":
    asyncio.run(main())