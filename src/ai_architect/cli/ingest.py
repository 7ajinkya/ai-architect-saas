import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ai_architect.config.settings import settings
from ai_architect.infrastructure.qdrant.vector_store import KnowledgeVectorStore
from ai_architect.knowledge.registry import load_source_registry, get_enabled_sources, DEFAULT_SOURCES
from ai_architect.services.knowledge.ingestion import IngestionPipeline, compute_chunk_id


console = Console()


def main():
    parser = argparse.ArgumentParser(description="AI Architect Knowledge Ingestion Pipeline")
    parser.add_argument(
        "--sources-dir",
        type=Path,
        default=Path("./data/sources"),
        help="Local directory for source repositories",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to sources configuration YAML",
    )
    
    parser.add_argument(
        "--force-reindex",
        action="store_true",
        help="Force re-index even if collection has data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Process sources but do not index to Qdrant",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="List configured sources and exit",
    )
    args = parser.parse_args()

    console.print("[bold blue]AI Architect - Knowledge Ingestion Pipeline[/bold blue]")
    console.print(f"Qdrant URL: {settings.qdrant_url}")
    console.print(f"Collection: {settings.qdrant_collection_name}")
    console.print(f"Embedding Provider: {settings.embedding_provider}")
    console.print(f"Embedding Model: {settings.embedding_model}")
    console.print()

    sources = load_source_registry(args.config)
    enabled_sources = get_enabled_sources(sources)

    if args.list_sources:
        table = Table(title="Configured Sources")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="green")
        table.add_column("Provider", style="yellow")
        table.add_column("Topic", style="magenta")
        table.add_column("Enabled", justify="center")
        table.add_column("URL", style="dim")

        for s in sources:
            table.add_row(
                s.name,
                s.source_type,
                s.provider,
                s.topic or "-",
                "Yes" if s.enabled else "No",
                s.url[:60] + ("..." if len(s.url) > 60 else ""),
            )
        console.print(table)
        return 0

    if not enabled_sources:
        console.print("[yellow]No enabled sources found. Enable sources in config or use --config.[/yellow]")
        return 1

    console.print(f"Processing {len(enabled_sources)} enabled source(s)...")
    console.print()

    pipeline = IngestionPipeline(
        local_base=args.sources_dir,
    )

    vector_store = None
    if not args.dry_run:
        vector_store = KnowledgeVectorStore()

    total_chunks = 0
    total_indexed = 0
    sources_processed = 0
    errors = []

    for source in enabled_sources:
        try:
            chunks = pipeline.process_source(source)
            total_chunks += len(chunks)

            if not args.dry_run:
                if args.force_reindex:
                    indexed = vector_store.index_chunks(chunks, skip_existing=False)
                else:
                    indexed = vector_store.index_chunks(chunks, skip_existing=True)
                total_indexed += indexed

            sources_processed += 1
            console.print(f"  [green]OK[/green] {source.name}: {len(chunks)} chunks" + (f", {indexed} indexed" if not args.dry_run else ""))

        except Exception as e:
            errors.append((source.name, str(e)))
            console.print(f"  [red]FAIL[/red] {source.name}: {e}")

    console.print()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  Sources processed: {sources_processed}/{len(enabled_sources)}")
    console.print(f"  Total chunks created: {total_chunks}")
    if not args.dry_run:
        console.print(f"  Total chunks indexed: {total_indexed}")

    if errors:
        console.print()
        console.print("[bold red]Errors:[/bold red]")
        for source_name, error in errors:
            console.print(f"  {source_name}: {error}")

    if not args.dry_run and vector_store:
        stats = vector_store.get_stats()
        console.print()
        console.print("[bold]Qdrant Collection Stats:[/bold]")
        console.print(f"  Collection: {stats['collection_name']}")
        console.print(f"  Points: {stats['points_count']}")
        console.print(f"  Status: {stats['status']}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())