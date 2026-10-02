from rich.console import Console

from ai_architect.agents.requirements.agent import create_requirements_agent
from ai_architect.agents.requirements.prompt import REQUIREMENTS_SYSTEM_PROMPT
from ai_architect.domain.requirements.models import (
    ExtractedRequirements,
    RequirementValue,
    RequirementsSpec,
)


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


def to_requirement_value(value: str) -> RequirementValue:
    if not value:
        return RequirementValue(
            value=None,
            source="unknown",
            evidence=None,
        )

    return RequirementValue(
        value=value,
        source="user_provided",
        evidence=value,
    )


def build_requirements_spec(
    extracted: ExtractedRequirements,
) -> RequirementsSpec:

    return RequirementsSpec(
        application_description=extracted.application_description,
        cloud_provider=to_requirement_value(
            extracted.cloud_provider
        ),
        region=to_requirement_value(
            extracted.region
        ),
        target_users_dau=to_requirement_value(
            extracted.target_users_dau
        ),
        availability_target=to_requirement_value(
            extracted.availability_target
        ),
        latency_requirements=to_requirement_value(
            extracted.latency_requirements
        ),
        traffic_characteristics=to_requirement_value(
            extracted.traffic_characteristics
        ),
        read_write_characteristics=to_requirement_value(
            extracted.read_write_characteristics
        ),
        storage_characteristics=to_requirement_value(
            extracted.storage_characteristics
        ),
        retention=to_requirement_value(
            extracted.retention
        ),
        compliance_requirements=(
            [extracted.compliance_requirements]
            if extracted.compliance_requirements
            else []
        ),
        budget_constraints=to_requirement_value(
            extracted.budget_constraints
        ),
        managed_service_preference=to_requirement_value(
            extracted.managed_service_preference
        ),
    )


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

    result = build_requirements_spec(extracted)

    print("\nExtracted Requirements:\n")
    console.print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
