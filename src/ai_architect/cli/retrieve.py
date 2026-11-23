import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ai_architect.config.settings import settings
from ai_architect.infrastructure.qdrant.vector_store import KnowledgeVectorStore
from ai_architect.services.knowledge.retrieval import RetrievalService, LegacyRetrievalService


console = Console()


def main():
    parser = argparse.ArgumentParser(description="AI Architect Knowledge Retrieval")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of results to return (default: 5)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        help="Filter by provider (aws, azure, gcp, cncf, general)",
    )
    parser.add_argument(
        "--document-type",
        type=str,
        help="Filter by document type (glossary, lab, architecture_pattern, reference_architecture, etc.)",
    )
    parser.add_argument(
        "--source",
        type=str,
        help="Filter by source name",
    )
    parser.add_argument(
        "--content-type",
        type=str,
        help="Filter by content type (concept, pattern, lab, reference_architecture, case_study, template, code_sample)",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        action="append",
        help="Filter by architecture pattern (can specify multiple)",
    )
    parser.add_argument(
        "--category",
        type=str,
        action="append",
        help="Filter by knowledge category (compute, storage, networking, etc.)",
    )
    parser.add_argument(
        "--pillar",
        type=str,
        action="append",
        help="Filter by Well-Architected pillar (cost-optimization, performance, reliability, security, operational-excellence, sustainability)",
    )
    parser.add_argument(
        "--provider-specific",
        action="store_true",
        help="Filter to only provider-specific content",
    )
    parser.add_argument(
        "--no-provider-specific",
        action="store_true",
        help="Filter to only cross-cloud content",
    )
    parser.add_argument(
        "--show-metadata",
        action="store_true",
        help="Show full metadata for each result",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show Qdrant collection statistics",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Use legacy retrieval format (RetrievedChunk)",
    )
    args = parser.parse_args()

    console.print("[bold blue]AI Architect - Knowledge Retrieval[/bold blue]")
    console.print(f"Qdrant URL: {settings.qdrant_url}")
    console.print(f"Collection: {settings.qdrant_collection_name}")
    console.print()

    vector_store = KnowledgeVectorStore()
    retrieval = RetrievalService()

    if args.stats:
        stats = vector_store.get_stats()
        console.print("[bold]Qdrant Collection Stats:[/bold]")
        console.print(f"  Collection: {stats['collection_name']}")
        console.print(f"  Points: {stats['points_count']}")
        console.print(f"  Status: {stats['status']}")
        return 0

    if not args.query:
        console.print("[yellow]Query is required unless --stats is specified.[/yellow]")
        return 1

    provider_specific = None
    if args.provider_specific:
        provider_specific = True
    elif args.no_provider_specific:
        provider_specific = False

    console.print(f"[bold]Query:[/bold] {args.query}")
    console.print(f"[bold]Top K:[/bold] {args.top_k}")
    if args.provider:
        console.print(f"[bold]Provider filter:[/bold] {args.provider}")
    if args.document_type:
        console.print(f"[bold]Document type filter:[/bold] {args.document_type}")
    if args.source:
        console.print(f"[bold]Source filter:[/bold] {args.source}")
    if args.content_type:
        console.print(f"[bold]Content type filter:[/bold] {args.content_type}")
    if args.pattern:
        console.print(f"[bold]Pattern filters:[/bold] {', '.join(args.pattern)}")
    if args.category:
        console.print(f"[bold]Category filters:[/bold] {', '.join(args.category)}")
    if args.pillar:
        console.print(f"[bold]Pillar filters:[/bold] {', '.join(args.pillar)}")
    if provider_specific is not None:
        console.print(f"[bold]Provider specific:[/bold] {provider_specific}")
    console.print()

    try:
        if args.legacy:
            # Use legacy service for backward compatibility
            legacy_service = LegacyRetrievalService(vector_store)
            results = legacy_service.retrieve(
                query=args.query,
                top_k=args.top_k,
                provider=args.provider,
                document_type=args.document_type,
                source=args.source,
                content_type=args.content_type,
                architecture_patterns=args.pattern,
                knowledge_categories=args.category,
                well_architected_pillars=args.pillar,
                provider_specific=provider_specific,
            )

            if not results:
                console.print("[yellow]No results found.[/yellow]")
                return 0

            console.print(f"[bold green]Found {len(results)} result(s):[/bold green]")
            console.print()

            for i, chunk in enumerate(results, 1):
                console.print(f"[bold cyan]--- Result {i} (score: {chunk.score:.4f}) ---[/bold cyan]")
                console.print(f"[bold]Source:[/bold] {chunk.source} ({chunk.source_name})")
                console.print(f"[bold]Provider:[/bold] {chunk.provider}")
                console.print(f"[bold]Document Type:[/bold] {chunk.document_type}")
                console.print(f"[bold]Content Type:[/bold] {chunk.content_type}")
                console.print(f"[bold]File:[/bold] {chunk.file_path}")
                if chunk.section_path:
                    console.print(f"[bold]Section:[/bold] {chunk.section_path}")
                console.print(f"[bold]Categories:[/bold] {', '.join(chunk.knowledge_categories) if chunk.knowledge_categories else 'N/A'}")
                console.print(f"[bold]Patterns:[/bold] {', '.join(chunk.architecture_patterns) if chunk.architecture_patterns else 'N/A'}")
                console.print(f"[bold]Pillars:[/bold] {', '.join(chunk.well_architected_pillars) if chunk.well_architected_pillars else 'N/A'}")
                console.print(f"[bold]License:[/bold] {chunk.license} (confirmed: {chunk.license_confirmed})")
                console.print(f"[bold]Score:[/bold] {chunk.score:.4f}")
                console.print()
                console.print(f"[bold]Content:[/bold]")
                console.print(chunk.content[:800])
                if len(chunk.content) > 800:
                    console.print("... [truncated]")
                console.print()

                if args.show_metadata:
                    console.print("[dim]Full Metadata:[/dim]")
                    console.print(f"  source_url: {chunk.source_url}")
                    console.print(f"  source_name: {chunk.source_name}")
                    console.print(f"  file_path: {chunk.file_path}")
                    console.print(f"  section_path: {chunk.section_path}")
                    console.print(f"  content_type: {chunk.content_type}")
                    console.print(f"  knowledge_categories: {chunk.knowledge_categories}")
                    console.print(f"  architecture_patterns: {chunk.architecture_patterns}")
                    console.print(f"  service_integrations: {chunk.service_integrations}")
                    console.print(f"  well_architected_pillars: {chunk.well_architected_pillars}")
                    console.print(f"  provider_specific: {chunk.provider_specific}")
                    console.print(f"  license: {chunk.license} (confirmed: {chunk.license_confirmed})")
                    console.print(f"  chunk_index: {chunk.chunk_index}/{chunk.total_chunks}")
                    console.print(f"  version: {chunk.version}")
                    console.print(f"  retrieved_at: {chunk.retrieved_at}")
                    console.print(f"  score: {chunk.score:.4f}")
                    console.print()
        else:
            # Use new EvidenceBundle-based retrieval
            from ai_architect.services.knowledge.contracts import RetrievalFilters

            filters = RetrievalFilters(
                provider=args.provider,
                document_type=args.document_type,
                source=args.source,
                content_type=args.content_type,
                architecture_patterns=args.pattern or [],
                knowledge_categories=args.category or [],
                well_architected_pillars=args.pillar or [],
                provider_specific=provider_specific,
            )

            bundle = asyncio.run(RetrievalService().retrieve(
                query=args.query,
                top_k=args.top_k,
                filters=filters,
            ))

            if not bundle.evidence:
                console.print("[yellow]No results found.[/yellow]")
                return 0

            console.print(f"[bold green]Found {len(bundle.evidence)} result(s):[/bold green]")
            console.print()

            for i, evidence in enumerate(bundle.evidence, 1):
                console.print(f"[bold cyan]--- Result {i} (score: {evidence.score:.4f}) ---[/bold cyan]")
                console.print(f"[bold]Source:[/bold] {evidence.source} ({evidence.source_type})")
                console.print(f"[bold]Provider:[/bold] {evidence.provider}")
                console.print(f"[bold]Authority:[/bold] {evidence.authority} (level: {evidence.authority_level})")
                console.print(f"[bold]Document Type:[/bold] {evidence.document_id}")
                console.print(f"[bold]Content Type:[/bold] {evidence.metadata.get('content_type', 'N/A')}")
                console.print(f"[bold]File:[/bold] {evidence.metadata.get('file_path', 'N/A')}")
                if evidence.section_path:
                    console.print(f"[bold]Section:[/bold] {evidence.section_path}")
                console.print(f"[bold]Categories:[/bold] {', '.join(evidence.metadata.get('knowledge_categories', [])) if evidence.metadata.get('knowledge_categories') else 'N/A'}")
                console.print(f"[bold]Patterns:[/bold] {', '.join(evidence.metadata.get('architecture_patterns', [])) if evidence.metadata.get('architecture_patterns') else 'N/A'}")
                console.print(f"[bold]Pillars:[/bold] {', '.join(evidence.metadata.get('well_architected_pillars', [])) if evidence.metadata.get('well_architected_pillars') else 'N/A'}")
                console.print(f"[bold]License:[/bold] {evidence.metadata.get('license', 'N/A')} (confirmed: {evidence.metadata.get('license_confirmed', False)})")
                console.print(f"[bold]Score:[/bold] {evidence.score:.4f}")
                console.print()
                console.print(f"[bold]Content:[/bold]")
                console.print(evidence.content[:800])
                if len(evidence.content) > 800:
                    console.print("... [truncated]")
                console.print()

                if args.show_metadata:
                    console.print("[dim]Full Metadata:[/dim]")
                    console.print(f"  source: {evidence.source}")
                    console.print(f"  source_type: {evidence.source_type}")
                    console.print(f"  source_url: {evidence.url}")
                    console.print(f"  provider: {evidence.provider}")
                    console.print(f"  authority: {evidence.authority} (level: {evidence.authority_level})")
                    console.print(f"  document_id: {evidence.document_id}")
                    console.print(f"  file_path: {evidence.metadata.get('file_path', 'N/A')}")
                    console.print(f"  section_path: {evidence.section_path}")
                    console.print(f"  content_type: {evidence.metadata.get('content_type', 'N/A')}")
                    console.print(f"  knowledge_categories: {evidence.metadata.get('knowledge_categories', [])}")
                    console.print(f"  architecture_patterns: {evidence.metadata.get('architecture_patterns', [])}")
                    console.print(f"  service_integrations: {evidence.metadata.get('service_integrations', [])}")
                    console.print(f"  well_architected_pillars: {evidence.metadata.get('well_architected_pillars', [])}")
                    console.print(f"  provider_specific: {evidence.provider_specific}")
                    console.print(f"  license: {evidence.metadata.get('license', 'N/A')} (confirmed: {evidence.metadata.get('license_confirmed', False)})")
                    console.print(f"  version: {evidence.version}")
                    console.print(f"  retrieved_at: {evidence.retrieval_time.isoformat() if evidence.retrieval_time else 'N/A'}")
                    console.print(f"  score: {evidence.score:.4f}")
                    console.print()

    except Exception as e:
        console.print(f"[red]Error during retrieval:[/red] {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())