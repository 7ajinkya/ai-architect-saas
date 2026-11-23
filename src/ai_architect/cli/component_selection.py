import argparse
import asyncio
import sys

from rich.console import Console
from rich.text import Text

from ai_architect.cli.text import sanitize_console_text

from ai_architect.agents.component_selection.run import (
    build_component_query,
    resolve_provider,
    run_component_selection,
)
from ai_architect.domain.capacity.models import CapacityModelInput
from ai_architect.domain.requirements.models import RequirementsExtraction
from ai_architect.services.capacity.calculator import CapacityCalculator
from ai_architect.services.knowledge.contracts import RetrievalFilters


console = Console()


def _safe(text: str) -> str:
    """Make dynamic text safe for legacy Windows consoles (cp1252)."""
    return sanitize_console_text(text)


def _line(text: str) -> Text:
    """Plain-text line: no markup parsing of dynamic content."""
    return Text(_safe(text))


def _kv(label: str, value: str) -> Text:
    """A 'label: value' line with a bold label and literal value."""
    t = Text()
    t.append(f"{label}: ", style="bold")
    t.append(_safe(value))
    return t


def build_whatsapp_requirements(provider: str = "aws") -> RequirementsExtraction:
    """Fixed WhatsApp-like scenario for manual smoke verification."""
    return RequirementsExtraction(
        application_description=(
            "WhatsApp-like messaging system for 1 million daily active users"
        ),
        cloud_provider=provider,
        region="us-east-1",
        deployment_scope="global",
        availability="99.99%",
        latency="<500ms p99",
        target_users_dau="1000000",
        traffic_characteristics="high write throughput, read-heavy, bursty peaks",
        read_write_characteristics="10:1 read:write ratio",
        storage_characteristics="message history plus media attachments",
        retention="365 days",
        compliance_requirements="",
        budget_constraints="",
        managed_service_preference="fully managed",
    )


def build_whatsapp_capacity_input() -> CapacityModelInput:
    """Deterministic WhatsApp-like capacity inputs (no LLM arithmetic)."""
    return CapacityModelInput(
        daily_active_users=1_000_000,
        messages_per_user_per_day=40.0,
        peak_factor=4.0,
        read_write_ratio=10.0,
        average_message_size_bytes=1024,
        connection_factor=0.12,
        storage_retention_days=365,
    )


async def _run_async(args) -> int:
    provider = args.provider.strip().lower()
    if provider not in ("aws", "azure", "gcp"):
        console.print("[red]Component selection requires exactly one provider: aws, azure, or gcp.[/red]")
        return 1

    console.print("[bold blue]AI Architect - Component Selection (Slice 2E smoke)[/bold blue]")

    requirements = build_whatsapp_requirements(provider=provider)
    resolved = resolve_provider(requirements)
    console.print(_kv("Provider", resolved))

    capacity_input = build_whatsapp_capacity_input()
    capacity = CapacityCalculator().calculate(capacity_input)
    console.print("[bold]Capacity (deterministic):[/bold]")
    console.print(_line(f"  peak_write_qps: {capacity.peak_write_qps:.1f}"))
    console.print(_line(f"  peak_read_qps: {capacity.peak_read_qps:.1f}"))
    console.print(_line(f"  concurrent_connections: {capacity.concurrent_connections:.0f}"))
    console.print(_line(f"  storage_growth/day: {capacity.storage_growth_bytes_per_day / 1e9:.1f} GB"))
    console.print(_line(f"  formula_version: {capacity.formula_version}"))

    query = args.query or build_component_query(requirements)
    console.print(_kv("Retrieval query", query))
    console.print(_kv("Top K", str(args.top_k)))

    filters = RetrievalFilters(provider=resolved)
    # Retrieve via RetrievalService directly so provider hard-filter applies.
    from ai_architect.services.knowledge.retrieval import RetrievalService

    service = RetrievalService()
    bundle = await service.retrieve(
        query=query,
        top_k=args.top_k,
        filters=filters,
    )

    console.print(_kv("Evidence", f"{bundle.total_found} item(s) from {bundle.sources_queried}"))
    for ev in bundle.evidence[:5]:
        console.print(_line(
            f"  -  {ev.source} score={ev.score:.4f} :: "
            f"{(ev.title or ev.document_id)[:80]}"
        ))

    result = run_component_selection(
        requirements=requirements,
        capacity=capacity,
        bundle=bundle,
        provider=resolved,
    )

    console.print()
    console.print(f"[bold green]Selected {len(result.selected_components)} component(s):[/bold green]")
    for comp in result.selected_components:
        header = Text()
        header.append("  ", style="")
        header.append(_safe(comp.name), style="cyan")
        header.append(
            _safe(
                f" [{comp.provider}/{comp.category}] "
                f"confidence={comp.confidence} support={comp.evidence_support}"
            )
        )
        console.print(header)
        console.print(_line(f"    role: {comp.role}"))
        console.print(_line(f"    rationale: {comp.rationale[:220]}"))
        refs = ", ".join(comp.evidence_refs) if comp.evidence_refs else "none"
        console.print(_line(f"    evidence: {refs}"))
        if comp.alternatives:
            console.print(_line(f"    alternatives: {', '.join(comp.alternatives)}"))

    if result.rejected_candidates:
        console.print()
        console.print(f"[bold]Rejected ({len(result.rejected_candidates)}):[/bold]")
        for comp in result.rejected_candidates:
            console.print(_line(f"  - {comp.name}: {comp.rationale[:160]}"))

    if result.unresolved_tradeoffs:
        console.print()
        console.print(f"[bold yellow]Unresolved tradeoffs ({len(result.unresolved_tradeoffs)}):[/bold yellow]")
        for t in result.unresolved_tradeoffs:
            console.print(_line(f"  - {t.description[:200]}"))

    if result.assumptions:
        console.print()
        console.print("[bold]Assumptions:[/bold]")
        for a in result.assumptions:
            console.print(_line(f"  - {a[:200]}"))

    if result.warnings:
        console.print()
        console.print("[bold yellow]Warnings:[/bold yellow]")
        for w in result.warnings:
            console.print(_line(f"  - {w[:300]}"))

    console.print()
    console.print(_line(
        f"Evidence references ({len(result.evidence_references)}): "
        f"{', '.join(result.evidence_references[:10])}"
    ))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AI Architect Component Selection smoke test"
    )
    parser.add_argument("--provider", default="aws", help="aws, azure, or gcp")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--query", default=None, help="Override retrieval query")
    args = parser.parse_args()

    try:
        return asyncio.run(_run_async(args))
    except ValueError as e:
        console.print(Text(f"Validation error: {e}", style="red"))
        return 1
    except Exception as e:
        console.print(Text(f"Error during component selection: {e}", style="red"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
