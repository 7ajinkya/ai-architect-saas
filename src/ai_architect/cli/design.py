"""Minimal CLI entry point for the orchestrated design workflow.

Accepts a user request, starts one design session, preserves the existing
Requirements HITL interaction (answered inline), then continues through
Capacity and Component Selection and displays the final result.
"""

import argparse
import asyncio
import sys

from rich.console import Console

from ai_architect.agents.design.run import run_design_workflow
from ai_architect.agents.design.state import DesignStatus
from ai_architect.cli.text import sanitize_console_text


console = Console()


def _safe(text: object) -> str:
    return sanitize_console_text(text)


def _display(value: object) -> str:
    """Render a requirement value, marking genuinely unspecified ones."""
    if value is None:
        return "not specified"
    text = _safe(value).strip()
    return text if text else "not specified"


def _short(text: object, limit: int = 240) -> str:
    """Concise terminal-safe rendering of free-form model text."""
    cleaned = " ".join(_safe(text).split())
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip() + "..."
    return cleaned


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


async def _run_async(args) -> int:
    user_request = args.request

    if not user_request:
        user_request = read_multiline_input()

    if not user_request:
        console.print("[yellow]No request given. Exiting.[/yellow]")
        return 1

    console.print("[bold blue]AI Architect - Design Workflow[/bold blue]")
    console.print(f"Session: {args.session_id or '(new)'}")
    console.print(f"Request: {_safe(user_request[:200])}")

    try:
        result = await run_design_workflow(
            user_request=user_request,
            session_id=args.session_id,
        )
    except RuntimeError as e:
        console.print(f"[red]Refused to start: {_safe(e)}[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Workflow error: {_safe(e)}[/red]")
        return 1

    if result.get("status") == DesignStatus.FAILED.value:
        console.print(f"[red]Design workflow FAILED: {_safe(result.get('error'))}[/red]")
        return 1

    requirements_state = result.get("requirements_state")
    capacity_result = result.get("capacity_result")
    component_selection = result.get("component_selection")

    console.print("\n[bold green]OK Requirements ready[/bold green]")
    if requirements_state is not None and requirements_state.extraction is not None:
        ext = requirements_state.extraction
        console.print(f"  provider: {_display(ext.cloud_provider)}")
        console.print(f"  region: {_display(ext.region)}")
        console.print(f"  dau: {_display(ext.target_users_dau)}")
        console.print(f"  requirements session: {requirements_state.session_id}")

    console.print("\n[bold green]OK Capacity modeled[/bold green]")
    if capacity_result is not None:
        calc = capacity_result.calculation
        console.print(f"  peak_write_qps: {calc.peak_write_qps:.1f}")
        console.print(f"  peak_read_qps: {calc.peak_read_qps:.1f}")
        console.print(f"  concurrent_connections: {calc.concurrent_connections:.0f}")
        console.print(f"  formula_version: {_safe(calc.formula_version)}")

    if component_selection is not None:
        extraction = (
            requirements_state.extraction
            if requirements_state is not None
            else None
        )
        render_component_selection(component_selection, extraction)

    cost_result = result.get("cost_result")
    if cost_result is not None:
        render_cost_estimation(cost_result)

    architecture = result.get("architecture")
    if architecture is not None:
        render_architecture(architecture)

    console.print(f"\nDesign session: {result.get('session_id')}")
    return 0


def render_component_selection(component_selection, extraction=None) -> None:
    """Render the full ComponentSelectionResult: selected, rejected, tradeoffs.

    Presentation only: every string comes from fields already present on
    the result (or the requirements extraction, for extraction assumptions).
    Empty collections render as (none); absent optional text
    (e.g. recommended_resolution) is omitted rather than fabricated.
    """
    console.print("\n[bold green]OK Components selected[/bold green]")
    console.print(
        f"  Selected {len(component_selection.selected_components)} component(s), "
        f"{len(component_selection.rejected_candidates)} rejected, "
        f"{len(component_selection.unresolved_tradeoffs)} tradeoff(s)"
    )

    console.print("\n  [bold]Selected:[/bold]")
    if not component_selection.selected_components:
        console.print("    (none)")
    for comp in component_selection.selected_components:
        console.print(
            f"    - {_safe(comp.name)} [{_safe(comp.provider)}/{_safe(comp.category)}] "
            f"confidence={_safe(comp.confidence)}"
        )
        console.print(f"      Why: {_short(comp.rationale)}")
        if comp.evidence_refs:
            console.print(
                f"      Evidence: {', '.join(_safe(r) for r in comp.evidence_refs)}"
            )
        else:
            console.print("      Evidence: (none)")
        if comp.assumptions:
            console.print("      Assumptions:")
            for assumption in comp.assumptions:
                console.print(f"        - {_short(assumption)}")

    console.print("\n  [bold]Assumptions:[/bold]")
    extraction_assumptions = (
        list(extraction.assumptions)
        if extraction is not None and extraction.assumptions
        else []
    )
    if not component_selection.assumptions and not extraction_assumptions:
        console.print("    (none)")
    for assumption in component_selection.assumptions:
        console.print(f"    - {_short(assumption)}")
    for assumption in extraction_assumptions:
        console.print(f"    - {_short(assumption.name)}")
        if _safe(assumption.value).strip():
            console.print(f"      Value: {_short(assumption.value)}")
        if _safe(assumption.source).strip():
            console.print(f"      Source: {_short(assumption.source)}")
        if _safe(assumption.rationale).strip():
            console.print(f"      Rationale: {_short(assumption.rationale)}")

    console.print("\n  [bold]Rejected:[/bold]")
    if not component_selection.rejected_candidates:
        console.print("    (none)")
    for comp in component_selection.rejected_candidates:
        console.print(f"    - {_safe(comp.name)}")
        console.print(f"      Reason: {_short(comp.rationale)}")

    console.print("\n  [bold]Tradeoffs:[/bold]")
    if not component_selection.unresolved_tradeoffs:
        console.print("    (none)")
    for tradeoff in component_selection.unresolved_tradeoffs:
        console.print(f"    - {_short(tradeoff.description)}")
        options = [_short(o, 120) for o in tradeoff.options]
        console.print(f"      Options: {', '.join(options) if options else '(none)'}")
        if _safe(tradeoff.impact).strip():
            console.print(f"      Impact: {_short(tradeoff.impact)}")
        if tradeoff.recommended_resolution:
            console.print(
                f"      Recommendation: {_short(tradeoff.recommended_resolution)}"
            )


def render_cost_estimation(cost_result) -> None:
    """Render the deterministic CostEstimationResult.

    Presentation only: every number comes from the calculator over live
    Price List facts. Unpriced components render with their explicit
    reason; nothing is estimated silently. Pricing coverage gaps
    (dimensions billed by the service but outside this pipeline) are
    disclosed explicitly.
    """
    console.print("\n[bold green]OK Cost estimated (deterministic)[/bold green]")
    if cost_result.lines:
        for line in cost_result.lines:
            console.print(
                f"    - {_safe(line.component_name)} [{_safe(line.pricing_model)}] "
                f"${line.monthly_cost_usd:,.2f}/mo "
                f"({line.monthly_usage:,.1f} {line.unit} @ "
                f"${line.unit_price_usd:.10f})"
            )
            console.print(f"      Source: {_safe(line.pricing_source)}")
            console.print(
                f"      Retrieved: {line.pricing_retrieved_at.isoformat()} "
                f"| {_safe(line.region_code)}"
            )
    else:
        console.print("    (no priced lines)")
    if cost_result.totals_usd_per_month:
        for model, total in sorted(cost_result.totals_usd_per_month.items()):
            console.print(f"  Estimated priced subtotal ({model}): ${total:,.2f}/mo")
    completeness_raw = getattr(cost_result, "completeness", None)
    completeness = _safe(
        completeness_raw.value
        if hasattr(completeness_raw, "value")
        else str(completeness_raw or "")
    )
    if completeness:
        best_estimate = getattr(cost_result, 'best_estimate_total_usd_per_month', None)
        if best_estimate is not None:
            console.print('  Best-estimate total (' + completeness + '): $' + format(best_estimate, ',.2f') + '/mo')
        else:
            unresolved = int(getattr(cost_result, 'unresolved_dimension_count', 0) or 0)
            console.print('  Completeness: ' + completeness + ' - ' + str(unresolved) + ' dimension(s) unresolved; no total is published while the estimate is partial.')
    if cost_result.estimated_lines:
        pass
    estimates = getattr(cost_result, 'estimates', None)
    if estimates:
        pass
    range_value = getattr(cost_result, 'estimated_range_usd_per_month', None)
    if range_value is not None:
        pass
        pass
    if cost_result.unpriced:
        console.print('\n  [bold]Unpriced:[/bold]')
        for item in cost_result.unpriced:
            console.print('    - ' + str(item.component_name))
    coverage_gaps = [entry for entry in getattr(cost_result, 'coverage', []) if str(entry.status) not in ('priced', 'not_applicable', 'estimated')]
    if coverage_gaps:
        console.print('\n  [bold]Pricing coverage gaps:[/bold]')
        for est in estimates:
            console.print(
                f"    - {_safe(est.component_name)} / "
                f"{_safe(est.dimension)}: "
                f"{est.low_quantity:,.3f} / "
                f"{est.typical_quantity:,.3f} / "
                f"{est.high_quantity:,.3f} {est.unit} "
                f"(low / typical / high), confidence {est.confidence}, "
                f"provenance {est.provenance}"
            )
            if est.rationale:
                console.print(f"      Rationale: {_short(est.rationale)}")
    range_value = getattr(cost_result, "estimated_range_usd_per_month", None)
    if range_value is not None:
        console.print(
            "  Illustrative range: "
            f"${range_value.low_usd:,.2f} â€“ ${range_value.high_usd:,.2f}/mo "
            "(not a contractual bound)"
        )
    if cost_result.unpriced:
        console.print("\n  [bold]Unpriced:[/bold]")
        for item in cost_result.unpriced:
            console.print(f"    - {_safe(item.component_name)}")
            console.print(f"      Reason: {_short(item.reason)}")
    coverage_gaps = [
        entry
        for entry in getattr(cost_result, "coverage", [])
        if _safe(str(entry.status)) not in ("priced", "not_applicable", "estimated")
    ]
    if coverage_gaps:
        console.print("\n  [bold]Pricing coverage gaps:[/bold]")
        for entry in coverage_gaps:
            # The status is copied verbatim; the amount is never
            # invented and the entry never carries a dollar value.
            console.print(
                f"    - {_safe(entry.component_name)} / "
                f"{_safe(entry.label)} [{_safe(str(entry.status))}]"
            )
            console.print(f"      Reason: {_short(entry.reason)}")
    if cost_result.assumptions:
        console.print("\n  [bold]Cost assumptions:[/bold]")
        for assumption in cost_result.assumptions:
            console.print(f"    - {_short(assumption)}")
    if cost_result.warnings:
        console.print("\n  [bold]Cost warnings:[/bold]")
        for warning in cost_result.warnings:
            console.print(f"    - {_short(warning)}")


def render_architecture(architecture) -> None:
    """Render the structured ArchitectureResult.

    Presentation only: every string and number comes from fields
    already present on the result. No recalculation, no validation,
    no Terraform output. Empty collections render as (none).
    """
    console.print("\n[bold green]OK Architecture composed[/bold green]")
    console.print(
        f"  Artifact: {_safe(architecture.artifact_id)} "
        f"v{architecture.version} [{_safe(architecture.schema_version)}]"
    )
    console.print(
        f"  Nodes: {len(architecture.nodes)}  "
        f"Edges: {len(architecture.edges)}  "
        f"Decisions: {len(architecture.decisions)}"
    )
    console.print(
        f"  Inputs hash: {_safe(architecture.inputs_ref.upstream_inputs_hash[:12])}... "
        f"| region {_safe(architecture.inputs_ref.region_code)}"
    )

    console.print("\n  [bold]Nodes:[/bold]")
    if not architecture.nodes:
        console.print("    (none)")
    for node in architecture.nodes:
        console.print(
            f"    - {_safe(node.node_id)}: {_safe(node.component_name)} "
            f"[{_safe(node.category)}]"
        )
        console.print(f"      Role: {_short(node.role_in_system)}")
        if node.capacity:
            for link in node.capacity:
                console.print(
                    f"      Load: {_safe(link.metric)} = {link.value:,.3f} "
                    f"{_safe(link.unit)}"
                )
        else:
            console.print("      Load: (none)")
        if _safe(node.scaling_note).strip():
            console.print(f"      Scaling: {_short(node.scaling_note)}")
        if _safe(node.availability_note).strip():
            console.print(f"      Availability: {_short(node.availability_note)}")
        if node.evidence_refs:
            console.print(
                f"      Evidence: {', '.join(_safe(r) for r in node.evidence_refs)}"
            )

    console.print("\n  [bold]Flows:[/bold]")
    if not architecture.edges:
        console.print("    (none)")
    for edge in architecture.edges:
        console.print(
            f"    - {_safe(edge.edge_id)}: {_safe(edge.from_node)} "
            f"-> {_safe(edge.to_node)} [{_safe(edge.kind)}]"
        )
        if _safe(edge.protocol).strip():
            console.print(f"      Protocol: {_safe(edge.protocol)}")
        console.print(f"      Purpose: {_short(edge.description)}")
        if edge.load is not None:
            console.print(
                f"      Load: {_safe(edge.load.metric)} = "
                f"{edge.load.value:,.3f} {_safe(edge.load.unit)}"
            )

    console.print("\n  [bold]Entry points:[/bold]")
    console.print(
        f"    {', '.join(_safe(e) for e in architecture.entry_points) or '(none)'}"
    )
    console.print("  [bold]Data stores:[/bold]")
    console.print(
        "    "
        f"{', '.join(_safe(d) for d in architecture.data_store_node_ids) or '(none)'}"
    )
    if architecture.boundaries:
        console.print("  [bold]Boundaries:[/bold]")
        for boundary in architecture.boundaries:
            console.print(f"    - {_short(boundary)}")

    console.print("\n  [bold]Cost links:[/bold]")
    if not architecture.cost_links:
        console.print("    (none)")
    for link in architecture.cost_links:
        if link.priced:
            console.print(
                f"    - {_safe(link.component_name)} "
                f"${link.monthly_cost_usd:,.2f}/mo "
                f"[{_safe(link.pricing_model)}]"
            )
        else:
            console.print(f"    - {_safe(link.component_name)} (unpriced)")
            console.print(f"      Reason: {_short(link.unpriced_reason or '')}")
    if architecture.cost_totals_snapshot:
        for model, total in sorted(architecture.cost_totals_snapshot.items()):
            console.print(f"  Total {model}: ${total:,.2f}/mo")

    if architecture.decisions:
        console.print("\n  [bold]Decisions:[/bold]")
        for decision in architecture.decisions:
            console.print(f"    - {_short(decision.decision)}")
            console.print(f"      Rationale: {_short(decision.rationale)}")

    if architecture.assumptions:
        console.print("\n  [bold]Architecture assumptions:[/bold]")
        for assumption in architecture.assumptions:
            console.print(f"    - {_short(assumption)}")
    if architecture.warnings:
        console.print("\n  [bold]Architecture warnings:[/bold]")
        for warning in architecture.warnings:
            console.print(f"    - {_short(warning)}")
    if architecture.unresolved_tradeoffs:
        console.print("\n  [bold]Unresolved tradeoffs:[/bold]")
        for tradeoff in architecture.unresolved_tradeoffs:
            console.print(f"    - {_short(tradeoff.description)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI Architect orchestrated design workflow"
    )
    parser.add_argument(
        "--request",
        default=None,
        help="Natural-language infrastructure request (else prompted)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Design session id (default: new uuid)",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run_async(args))
    except Exception as e:
        console.print(f"[red]Error during design workflow: {e}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
