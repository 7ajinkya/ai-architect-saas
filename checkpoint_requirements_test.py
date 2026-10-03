import asyncio

from ai_architect.infrastructure.checkpoint.requirements import (
    load_requirements_state,
)


async def main() -> None:
    print("Loading real CLI state...")

    restored_state = await load_requirements_state("test-session")

    print()
    print("Restored state:")
    print(restored_state.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())