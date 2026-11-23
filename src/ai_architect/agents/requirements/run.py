import asyncio
import uuid

from rich.console import Console
from rich.panel import Panel
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from ai_architect.agents.requirements.agent import (
    create_requirements_agent_raw,
    create_requirements_agent_with_raw,
)
from ai_architect.agents.requirements.graph import build_requirements_graph
from ai_architect.agents.requirements.prompt import REQUIREMENTS_SYSTEM_PROMPT
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
        Panel(
            f"[bold]{interrupt_value['question']}[/bold]",
            title="Clarification needed",
            border_style="yellow",
            expand=False,
        )
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


def _parse_text_envelope(content: str):
    """Parse a text-envelope model response against the extraction schema.

    Used ONLY when the model answered in plain JSON text instead of
    emitting the requested function call. The text must still validate
    against the same RequirementsExtraction contract; anything else
    raises loudly. No defaults are invented beyond the schema's own
    empty-value representation, and no missing requirement values are
    fabricated.
    """
    import json as _json

    from ai_architect.domain.requirements.models import RequirementsExtraction

    text = (content or "").strip()
    if not text:
        raise ValueError(
            "Requirements agent returned no function call and empty text; "
            "nothing to parse."
        )
    try:
        return RequirementsExtraction.model_validate_json(text)
    except Exception:
        pass
    try:
        start = text.index("{")
        obj, _ = _json.JSONDecoder().raw_decode(text[start:])
        return RequirementsExtraction.model_validate(obj)
    except Exception as e:
        raise ValueError(
            "Requirements agent did not emit a function call and its "
            "text output is not valid RequirementsExtraction JSON: "
            f"{e}. Preview: {text[:300]!r}"
        ) from e


async def advance_requirements_flow(
    state: RequirementsState,
) -> tuple[RequirementsState, dict | None]:
    """Single non-blocking step of the requirements HITL flow.

    Adapter entry point for non-interactive callers (HTTP sessions).
    It drives the SAME requirements graph and validator as
    run_requirements_flow, but never reads stdin: it returns either the
    next pending clarification or the finished state.

    Re-entering with the caller-owned state (instead of resuming the
    interrupt) keeps RequirementsState authoritative in the caller, so
    recorded assumptions survive across separate requests.
    """
    async with create_checkpointer() as checkpointer:
        graph = build_requirements_graph(checkpointer)

        config = {
            "configurable": {
                "thread_id": state.session_id,
            }
        }

        result = await graph.ainvoke(
            {
                "requirements_state": state,
                "clarification_answer": None,
            },
            config=config,
        )

    if "__interrupt__" in result:
        interrupt_info = result["__interrupt__"][0]
        return result["requirements_state"], dict(interrupt_info.value)

    return result["requirements_state"], None


# Bounds for requirements extraction recovery: the initial attempt plus
# exactly one recovery attempt. A second failure of any kind fails loudly
# with both attempts identified; there is no third attempt and no loop.
MAX_REQUIREMENTS_ATTEMPTS = 2

STRICT_FORMAT_RETRY_PROMPT = (
    "Your previous response could not be validated against the "
    "RequirementsExtraction schema. Respond again with ONLY the "
    "RequirementsExtraction function call and no surrounding prose, "
    "tables, Markdown, or explanations, using the exact field names of "
    "the schema. The schema is flat: every top-level field is a string "
    "(empty string when information was not provided), plus three arrays: "
    "additional_requirements, assumptions, and clarification_candidates. "
    "additional_requirements items use only {name, value, unit, category, "
    "objective, scope, source, evidence}; assumptions items use only "
    "{name, value, unit, category, objective, scope, source, rationale, "
    "confidence, evidence}; clarification_candidates items use only "
    "{requirement_key, requirement, question, reason, impact}. Do not emit "
    "any field that is not present in the schema; additional or unknown "
    "fields are rejected and will fail validation. Preserve the user's "
    "information exactly; do not invent numerical values, providers, "
    "regions, budgets, or dates."
)


def _attempt_requirements_parsing(agent, raw_agent, messages):
    """One attempt: function call, empty-response retry, envelope fallback.

    Returns a validated RequirementsExtraction. Raises ValueError when the
    model output cannot be validated against the extraction contract. The
    empty-response retry (text envelope, no tools) is internal to this
    attempt and does not consume a second attempt.
    """
    import json as _json

    from ai_architect.domain.requirements.models import RequirementsExtraction

    outcome = agent.invoke(messages)
    raw = outcome.get("raw") if isinstance(outcome, dict) else None
    parsed = outcome.get("parsed") if isinstance(outcome, dict) else outcome
    parsing_error = (
        outcome.get("parsing_error") if isinstance(outcome, dict) else None
    )

    if parsed is None and parsing_error is None:
        content = getattr(raw, "content", "") or ""
        if not content.strip():
            # Objectively empty response with tools bound: retry without
            # tools and parse the text through the same extraction contract.
            retry_msg = raw_agent.invoke(messages)
            content = getattr(retry_msg, "content", "") or ""
        # Non-empty text here (original or retried) must still satisfy the
        # same boundary schema; empty/invalid text raises loudly.
        parsed = _parse_text_envelope(content)

    if isinstance(parsed, dict):
        parsed = RequirementsExtraction(**parsed)

    if not isinstance(parsed, RequirementsExtraction):
        raise ValueError(
            "Requirements LLM boundary failure: the model returned no "
            "usable structured RequirementsExtraction "
            f"({parsing_error or type(parsed).__name__}); cannot validate "
            "or build requirements state from nothing."
        )
    return parsed


def build_initial_requirements_state(
    user_request: str,
    session_id: str | None = None,
) -> RequirementsState:
    """Run extraction + initial validation and build the session state.

    Reusable entry for both the standalone requirements CLI and the
    top-level design orchestrator. Single source of truth for how an
    initial RequirementsState is created from a user request.
    """
    from ai_architect.domain.requirements.models import RequirementsExtraction

    agent = create_requirements_agent_with_raw()
    raw_agent = create_requirements_agent_raw()

    messages = [
        ("system", REQUIREMENTS_SYSTEM_PROMPT),
        ("human", user_request),
    ]

    first_error: ValueError | None = None
    attempt = 1
    while True:
        try:
            extracted = _attempt_requirements_parsing(
                agent, raw_agent, messages
            )
        except ValueError as parse_error:
            if (
                attempt >= MAX_REQUIREMENTS_ATTEMPTS
                and first_error is not None
            ):
                raise ValueError(
                    "Requirements LLM boundary failure: the model returned "
                    "no usable structured RequirementsExtraction, even after "
                    "one bounded correction retry. "
                    f"First: {first_error} Retry: {parse_error}"
                ) from parse_error
            first_error = parse_error
            attempt = MAX_REQUIREMENTS_ATTEMPTS
            messages = messages + [
                HumanMessage(content=STRICT_FORMAT_RETRY_PROMPT)
            ]
            continue

        validation = RequirementsValidator().validate(extracted)

        session_id = session_id or str(uuid.uuid4())

        return RequirementsState.from_extraction(
            session_id=session_id,
            original_request=user_request,
            extraction=extracted,
            validation_result=validation,
        )


async def main() -> None:
    user_request = read_multiline_input()

    if user_request is None:
        console.print(
            "\n[yellow]Exiting AI Architect. Goodbye! 👋[/yellow]"
        )
        return

    state = build_initial_requirements_state(user_request)

    console.print("[green]✓ Requirements extracted[/green]")

    console.print(
        f"\n[bold cyan]Session ID:[/bold cyan] {state.session_id}"
    )

    state = await run_requirements_flow(state)

    await save_requirements_state(state)

    console.print("[green]✓ Requirements ready[/green]")

    console.print(
        "\n[bold]Final Requirements State:[/bold]"
    )

    console.print(
        state.model_dump_json(indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())