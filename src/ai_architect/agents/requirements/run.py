from rich.console import Console

from ai_architect.agents.requirements.agent import create_requirements_agent
from ai_architect.agents.requirements.prompt import REQUIREMENTS_SYSTEM_PROMPT


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


def main() -> None:
    agent = create_requirements_agent()

    user_request = read_multiline_input()

    if user_request is None:
        console.print(
            "\n[yellow]Exiting AI Architect. Goodbye! 👋[/yellow]"
        )
        return

    with console.status(
        "[bold green]Extracting requirements...[/bold green]",
        spinner="dots",
    ):
        extracted = agent.invoke(
            [
                ("system", REQUIREMENTS_SYSTEM_PROMPT),
                ("human", user_request),
            ]
        )

    console.print("[green]✓ Requirements extracted[/green]")

    print("\nRequirements Extraction:\n")

    console.print(
        extracted.model_dump_json(indent=2)
    )


if __name__ == "__main__":
    main()