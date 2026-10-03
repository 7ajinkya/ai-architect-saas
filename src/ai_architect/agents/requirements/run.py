import asyncio
import uuid

from rich.console import Console
from langgraph.types import Command

from ai_architect.agents.requirements.agent import create_requirements_agent
from ai_architect.agents.requirements.graph import build_requirements_graph
from ai_architect.agents.requirements.prompt import REQUIREMENTS_SYSTEM_PROMPT
from ai_architect.cli.progress import RequirementProgress
from ai_architect.domain.requirements.state import RequirementsState
from ai_architect.infrastructure.checkpoint.requirements import (
    save_requirements_state,
)
from ai_architect.infrastructure.checkpoint.sqlite import create_checkpointer
from ai_architect.services.requirements.validator import RequirementsValidator


console = Console()


def read_multiline_input() -> str | None:
    print(
        "\nDescribe the system you want to build.\n"
        "Enter multiple lines if needed.\n"
        "Press Enter on an empty line when finished.\n"
        "Type 'exit' or 'quit' to cancel.\n"
    )

    lines = []

    while True:
        line = input("> ")

        if not lines and line.strip().lower() in {"exit", "quit"}:
            return None

        if line == "":
            break

        lines.append(line)

    return "\n".join(lines).strip()


def display_clarification(interrupt_value: dict) -> None:
    console.print(
        "\n[bold yellow]Clarification needed[/bold yellow]"
    )

    console.print(
        f"\n[bold]{interrupt_value['question']}[/bold]"
    )


async def run_requirements_flow(
    state: RequirementsState,
) -> RequirementsState:
    async with create_checkpointer() as checkpointer:
        graph = build_requirements_graph(checkpointer)

        config = {
            "configurable": {
                "thread_id": state.session_id,
            }
        }

        graph_state = {
            "requirements_state": state,
            "clarification_answer": None,
        }

        result = await graph.ainvoke(
            graph_state,
            config=config,
        )

        while "__interrupt__" in result:
            interrupt_info = result["__interrupt__"][0]
            interrupt_value = interrupt_info.value

            display_clarification(interrupt_value)

            answer = input("\nYour answer: ").strip()

            if not answer:
                console.print(
                    "\n[yellow]Answer cannot be empty.[/yellow]"
                )
                continue

            result = await graph.ainvoke(
                Command(resume=answer),
                config=config,
            )

        return result["requirements_state"]


async def main() -> None:
    agent = create_requirements_agent()

    user_request = read_multiline_input()

    if user_request is None:
        console.print(
            "\n[yellow]Exiting AI Architect. Goodbye! 👋[/yellow]"
        )
        return

    progress = RequirementProgress()
    progress.start()

    try:
        progress.activate(0)

        extracted = agent.invoke(
            [
                ("system", REQUIREMENTS_SYSTEM_PROMPT),
                ("human", user_request),
            ]
        )

        progress.complete(0)
        progress.activate(1)

        validation = RequirementsValidator().validate(
            extracted
        )

        session_id = str(uuid.uuid4())

        state = RequirementsState.from_extraction(
            session_id=session_id,
            original_request=user_request,
            extraction=extracted,
            validation_result=validation,
        )

        progress.complete(1)

    finally:
        progress.stop()

    console.print(
        f"\n[bold cyan]Session ID:[/bold cyan] {session_id}"
    )

    state = await run_requirements_flow(state)

    await save_requirements_state(state)

    console.print(
        "\n[bold]Final Requirements State:[/bold]"
    )

    console.print(
        state.model_dump_json(indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())