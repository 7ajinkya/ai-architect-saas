"""Terraform generation command: approved design -> plan, locally shown.

Usage:
    ai-architect terraform --session-id <id>
        [--set resource_id.param=value ...] [--out-dir DIR]

Requires a recorded positive design approval for the session's
current architecture; anything else is refused before any Terraform
is produced. Never provisions: it prints the static inventory
analysis and validation outcome, and optionally writes the generated
source tree to --out-dir for inspection.
"""

import argparse
import asyncio
from pathlib import Path

from rich.console import Console

from ai_architect.agents.component_selection.run import resolve_provider
from ai_architect.cli.text import sanitize_console_text
from ai_architect.infrastructure.checkpoint.design import (
    load_design_snapshot,
)
from ai_architect.services.approvals import design_approval_allowed
from ai_architect.services.provisioning.plan_builder import (
    build_provisioning_plan,
)

console = Console()


def _safe(text: object) -> str:
    return sanitize_console_text(text)


def _parse_set(values: list[str]) -> dict[str, dict[str, str]]:
    """Parse resource_id.param=value pairs, failing loudly on shape."""
    inputs: dict[str, dict[str, str]] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(
                f"Malformed --set {item!r}: expected "
                "resource_id.param=value."
            )
        left, value = item.split("=", 1)
        if "." not in left or not value:
            raise ValueError(
                f"Malformed --set {item!r}: expected "
                "resource_id.param=value."
            )
        resource_id, _, param = left.partition(".")
        inputs.setdefault(resource_id, {})[param] = value
    return inputs


def render_plan_summary(plan: dict) -> None:
    """Print what would be created, validated, or still decided."""
    console.print(
        f"[bold blue]Terraform plan ({_safe(plan['status'])})[/bold blue] "
        f"spec {_safe(plan['spec_id'][:12])} "
        f"tree {_safe(plan['tree_hash'][:12])}"
    )
    inventory = plan.get("inventory") or {}
    by_kind = inventory.get("by_kind") or {}
    kinds = ", ".join(
        f"{name} x{count}" for name, count in sorted(by_kind.items())
    )
    console.print(
        f"Creates: {inventory.get('creates', 0)}  "
        f"Destroys: {inventory.get('destroys', 0)}  "
        f"Uncovered: {inventory.get('uncovered', 0)}"
    )
    if kinds:
        console.print(f"Kinds: {_safe(kinds)}")
    for resource in plan.get("resources") or []:
        module = (resource.get("module") or {}).get("module_id")
        pending = ", ".join(
            item.get("name", "") for item in resource.get("pending_inputs") or []
        )
        detail = f"module {module}" if module else "no verified module"
        if pending:
            detail += f"; needs human decision: {pending}"
        console.print(
            f"  - {_safe(resource.get('resource_id'))} "
            f"[{_safe(resource.get('kind'))}] -> {_safe(detail)}"
        )
    for entry in plan.get("generated") or []:
        types = ", ".join(entry.get("resource_types") or [])
        sha = str(entry.get("proposal_sha256") or "")[:12]
        console.print(
            f"  - {_safe(entry.get('resource_id'))} "
            f"[LLM-generated: {_safe(types)} sha={_safe(sha)} "
            f"status={_safe(entry.get('status'))}]"
        )
        for note in entry.get("notes") or []:
            console.print(f"      {_safe(note)}")
    validation = plan.get("terraform_validation") or {}
    console.print(
        f"\n[bold]Terraform validation:[/bold] "
        f"{_safe(validation.get('status'))}"
        + (
            f" (terraform {_safe(validation.get('version'))})"
            if validation.get("version")
            else " (terraform not installed; deterministic guard passed)"
            if validation.get("status") in ("not_installed", "not_run")
            else ""
        )
    )
    for diagnostic in validation.get("diagnostics") or []:
        console.print(f"  - {_safe(diagnostic.get('message'))}")
    for message in plan.get("selection_errors") or []:
        console.print(f"  [yellow]selection: {_safe(message)}[/yellow]")
    for warning in plan.get("spec_warnings") or []:
        console.print(f"  [yellow]spec warning: {_safe(warning)}[/yellow]")
    for item in plan.get("spec_diagnostics") or []:
        console.print(f"  [yellow]spec: {_safe(item.get('message'))}[/yellow]")
    for item in plan.get("render_diagnostics") or []:
        console.print(f"  [yellow]render: {_safe(item.get('message'))}[/yellow]")
    console.print(
        "\n[yellow]Static inventory analysis, not a live terraform plan. "
        "Provisioning needs its own explicit approval.[/yellow]"
    )


async def _terraform_async(args) -> int:
    try:
        snapshot = await load_design_snapshot(args.session_id)
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Could not load session: {_safe(e)}[/red]")
        return 1
    if snapshot is None or snapshot.requirements_state is None:
        console.print(
            f"[red]Unknown session: {_safe(args.session_id)}[/red]"
        )
        return 1

    allowed, gate_outcome = design_approval_allowed(snapshot)
    if not allowed:
        console.print(
            "[red]Terraform generation is not available: no approved "
            f"design (gate={_safe(gate_outcome)}). Approve the design "
            "first.[/red]"
        )
        return 1

    try:
        inputs = _parse_set(args.set)
    except ValueError as e:
        console.print(f"[red]{_safe(e)}[/red]")
        return 1

    try:
        provider = resolve_provider(
            snapshot.requirements_state.extraction
        )
    except ValueError as e:
        console.print(f"[red]{_safe(e)}[/red]")
        return 1

    try:
        requirements_state = snapshot.requirements_state
        requirements = (
            requirements_state.extraction
            if requirements_state is not None
            else None
        )
    except Exception:  # noqa: BLE001 - absent requirements mean no derivation
        requirements = None

    try:
        stored_plan = getattr(snapshot, "terraform_artifact", None)
        prior_inputs = (
            stored_plan.get("inputs")
            if isinstance(stored_plan, dict)
            and isinstance(stored_plan.get("inputs"), dict)
            else None
        )
        plan = build_provisioning_plan(
            architecture=snapshot.architecture,
            provider=provider,
            approval=snapshot.design_approval,
            inputs=inputs,
            prior_inputs=prior_inputs,
            requirements=requirements,
        )
    except ValueError as e:
        console.print(f"[red]Terraform generation refused: {_safe(e)}[/red]")
        return 1

    render_plan_summary(plan)

    if args.out_dir:
        out = Path(args.out_dir)
        try:
            for relative, content in sorted(plan["files"].items()):
                target = out / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as e:
            console.print(f"[red]Could not write files: {_safe(e)}[/red]")
            return 1
        console.print(f"\nWrote {len(plan['files'])} file(s) to {_safe(str(out))}")
    return 0


def add_terraform_parser(sub) -> None:
    terraform_parser = sub.add_parser(
        "terraform",
        help="Generate the Terraform plan for an approved design",
    )
    terraform_parser.add_argument(
        "--session-id", required=True, help="Design session id"
    )
    terraform_parser.add_argument(
        "--set",
        action="append",
        default=[],
        help="Required input as resource_id.param=value (repeatable)",
    )
    terraform_parser.add_argument(
        "--out-dir",
        default="",
        help="Write the generated source tree here for inspection",
    )


def main(args) -> int:
    try:
        return asyncio.run(_terraform_async(args))
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Error during Terraform generation: {e}[/red]")
        return 1
