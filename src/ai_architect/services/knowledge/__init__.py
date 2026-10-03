# Knowledge service package
from ai_architect.services.knowledge.ingestion import (
    SourceFetcher,
    DocumentParser,
    DocumentNormalizer,
    DocumentChunker,
    IngestionPipeline,
    compute_chunk_id,
)
from ai_architect.services.knowledge.chunking import (
    HeadingAwareMarkdownSplitter,
    create_markdown_splitter,
    MarkdownHeading,
    CodeFence,
    extract_code_fences,
    restore_code_fences,
    parse_markdown_headings,
    get_section_path,
)
from ai_architect.services.knowledge.retrieval import (
    RetrievalService,
    LegacyRetrievalService,
    RetrievedChunk,
    get_retrieval_service,
)
from ai_architect.services.knowledge.contracts import (
    AuthorityLevel,
    KnowledgeSourceType,
    KnowledgeSource,
    EvidenceAuthority,
    Evidence,
    EvidenceBundle,
    RetrievalFilters,
    KnowledgeProvider,
    EvidenceBundleBuilder,
)
from ai_architect.services.knowledge.providers.qdrant_provider import QdrantProvider

__all__ = [
    "SourceFetcher",
    "DocumentParser",
    "DocumentNormalizer",
    "DocumentChunker",
    "IngestionPipeline",
    "compute_chunk_id",
    "HeadingAwareMarkdownSplitter",
    "create_markdown_splitter",
    "MarkdownHeading",
    "CodeFence",
    "extract_code_fences",
    "restore_code_fences",
    "parse_markdown_headings",
    "get_section_path",
    "RetrievalService",
    "LegacyRetrievalService",
    "RetrievedChunk",
    "get_retrieval_service",
    "AuthorityLevel",
    "KnowledgeSourceType",
    "KnowledgeSource",
    "EvidenceAuthority",
    "Evidence",
    "EvidenceBundle",
    "RetrievalFilters",
    "KnowledgeProvider",
    "EvidenceBundleBuilder",
    "QdrantProvider",
]