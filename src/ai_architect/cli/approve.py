"""Design approval command: explicit human boundary before provisioning.

Usage:
    ai-architect approve --session-id <id> [--note "..."] [--yes]

Shows the review summary (architecture, components, cost, critique,
gate verdict), then records an explicit verdict. Nothing is
provisioned by approval alone. Refuses loudly when the session has no
passing quality-gate verdict, using the same rule as the HTTP API.
"""

import argparse
import asyncio

from rich.console import Console

from ai_architect.cli.text import sanitize_console_text
from ai_architect.infrastructure.checkpoint.design import (
    load_design_snapshot,
    save_session_record,
)
from ai_architect.services.approvals import (
    build_design_approval,
    design_approval_allowed,
)

console = Console()


def _safe(text: object) -> str:
    return sanitize_console_text(text)


def _short(text: object, limit: int = 200) -> str:
    cleaned = " ".join(_safe(text).split())
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip() + "..."
    return cleaned


def render_review_summary(snapshot) -> None:
    """Print the approval review: what the user is accepting."""
    arch = snapshot.architecture
    cost = snapshot.cost_result
    critique = snapshot.critique
    gate = snapshot.quality_gate

    console.print("[bold blue]Architecture ready for provisioning.[/bold blue]")
    console.print(f"Request: {_short(snapshot.user_request, 160)}")
    if arch is not None:
        console.print(
            f"\n[bold]Architecture[/bold] v{arch.version} "
            f"({len(arch.nodes)} nodes, {len(arch.edges)} edges, "
            f"{len(arch.decisions)} decisions)"
        )
        for node in arch.nodes:
            console.print(
                f"  - {_short(node.component_name)} [{_short(node.category)}]"
            )
        if arch.assumptions:
            console.print("\n[bold]Assumptions:[/bold]")
            for assumption in arch.assumptions:
                console.print(f"  - {_short(assumption)}")
        if arch.warnings:
            console.print("\n[bold]Warnings:[/bold]")
            for warning in arch.warnings:
                console.print(f"  - {_short(warning)}")
    if cost is not None:
        totals = ", ".join(
            f"{key}: ${value:,.2f}/mo"
            for key, value in sorted(cost.totals_usd_per_month.items())
        )
        console.print(f"\n[bold]Cost:[/bold] {totals or '(no priced totals)'}")
        if cost.unpriced:
            console.print(
                f"  Unpriced: {len(cost.unpriced)} component(s) "
                "(excluded from totals)"
            )
    if critique is not None:
        criticals = critique.criticals
        console.print(
            f"\n[bold]Critic findings:[/bold] "
            f"{len(criticals)} critical, "
            f"{len(critique.findings) - len(criticals)} other"
        )
        for finding in critique.findings:
            console.print(
                f"  - [{finding.severity}] {_short(finding.statement)}"
            )
    if gate is not None:
        console.print(f"\n[bold]Quality gate:[/bold] {gate.outcome}")
        for reason in gate.reasons:
            console.print(f"  - {_short(reason)}")


async def _approve_async(args) -> int:
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
            "[red]Design approval is not available: the session has no "
            f"passing quality-gate verdict (gate={_safe(gate_outcome)})."
            "[/red]"
        )
        return 1

    render_review_summary(snapshot)

    approved = bool(args.yes)
    if not approved:
        console.print(
            "\nType [bold]approve[/bold] to accept this design "
            "(anything else aborts; nothing is provisioned either way)."
        )
        try:
            answer = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Aborted. No approval recorded.[/yellow]")
            return 1
        approved = answer == "approve"
        if not approved:
            console.print("[yellow]Aborted. No approval recorded.[/yellow]")
            return 1

    record = build_design_approval(
        approved=True,
        note=args.note or "",
        artifact_id=snapshot.architecture.artifact_id,
        gate_outcome=gate_outcome,
    )
    try:
        await save_session_record(
            args.session_id, field="design_approval", record=record
        )
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Could not record approval: {_safe(e)}[/red]")
        return 1

    console.print(
        "[green]Design approval recorded. Terraform generation is now "
        "unlocked; provisioning still requires its own approval.[/green]"
    )
    return 0


def add_approve_parser(sub) -> None:
    approve_parser = sub.add_parser(
        "approve",
        help="Review a gated design and record explicit approval",
    )
    approve_parser.add_argument(
        "--session-id", required=True, help="Design session id"
    )
    approve_parser.add_argument(
        "--note", default="", help="Accepted trade-offs or remarks"
    )
    approve_parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve without the interactive prompt",
    )


def main(args) -> int:
    try:
        return asyncio.run(_approve_async(args))
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Error during approval: {e}[/red]")
        return 1


async def _approve_provisioning_async(args) -> int:
    from ai_architect.services.provisioning.executor import (
        ExecutionRefused,
        build_provisioning_approval,
        require_provisioning_review,
        workspace_fingerprint,
    )

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

    plan = snapshot.terraform_artifact
    if (
        not isinstance(plan, dict)
        or not plan.get("tree_hash")
        or not plan.get("spec_id")
        or not plan.get("artifact_id")
    ):
        console.print(
            "[red]Provisioning approval is not available: the session "
            "has no stored Terraform plan. Generate it first.[/red]"
        )
        return 1
    architecture = getattr(snapshot, "architecture", None)
    architecture_id = str(getattr(architecture, "artifact_id", "") or "")
    if not architecture_id or plan.get("artifact_id") != architecture_id:
        console.print(
            "[red]Provisioning approval is not available: the stored "
            "Terraform plan does not match the session's current "
            "architecture artifact. Regenerate the plan first.[/red]"
        )
        return 1
    try:
        review = require_provisioning_review(
            snapshot.provisioning_result, plan
        )
    except ExecutionRefused as e:
        console.print(f"[red]{_safe(e)}[/red]")
        return 1
    workspace_hash = workspace_fingerprint(
        dict(plan.get("files") or {}), plan.get("artifacts")
    )

    console.print("[bold blue]Terraform plan pending approval.[/bold blue]")
    console.print(f"Spec: {_safe(plan.get('spec_id', '')[:12])} "
                  f"tree {_safe(plan.get('tree_hash', '')[:12])}")
    inventory = plan.get("inventory") or {}
    console.print(
        f"Creates: {inventory.get('creates', 0)}  "
        f"Destroys: {inventory.get('destroys', 0)}  "
        f"Uncovered: {inventory.get('uncovered', 0)}"
    )
    console.print(
        "[yellow]Approving authorizes future apply runs against exactly "
        "this tree and the reviewed saved plan. It executes "
        "nothing.[/yellow]"
    )
    console.print(
        f"Reviewed saved plan: {_safe(review['plan_file']['name'])} "
        f"sha256 {_safe(review['plan_file']['sha256'][:12])}"
    )

    approved = bool(args.yes)
    if not approved:
        console.print(
            "\nType [bold]approve[/bold] to accept this plan "
            "(anything else aborts)."
        )
        try:
            answer = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Aborted. No approval recorded.[/yellow]")
            return 1
        approved = answer == "approve"
        if not approved:
            console.print("[yellow]Aborted. No approval recorded.[/yellow]")
            return 1

    record = build_provisioning_approval(
        approved=True,
        note=args.note or "",
        tree_hash=plan["tree_hash"],
        spec_id=plan["spec_id"],
        artifact_id=plan["artifact_id"],
        artifacts=plan.get("artifacts"),
        session_id=args.session_id,
        plan_file=review["plan_file"],
        workspace_hash=workspace_hash,
    )
    try:
        await save_session_record(
            args.session_id, field="provisioning_approval", record=record
        )
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Could not record approval: {_safe(e)}[/red]")
        return 1

    console.print(
        "[green]Provisioning approval recorded. The provision command "
        "re-validates it against the stored plan before running.[/green]"
    )
    return 0


def add_approve_provisioning_parser(sub) -> None:
    provisioning_parser = sub.add_parser(
        "approve-provisioning",
        help="Review a stored Terraform plan and record approval",
    )
    provisioning_parser.add_argument(
        "--session-id", required=True, help="Design session id"
    )
    provisioning_parser.add_argument(
        "--note", default="", help="Remarks on the accepted plan"
    )
    provisioning_parser.add_argument(
        "--yes",
        action="store_true",
        help="Approve without the interactive prompt",
    )


def main_provisioning(args) -> int:
    try:
        return asyncio.run(_approve_provisioning_async(args))
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        console.print(f"[red]Error during approval: {e}[/red]")
        return 1
