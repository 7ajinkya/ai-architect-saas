from datetime import datetime
from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, ConfigDict, Field


class AuthorityLevel(str, Enum):
    """Authority level of a knowledge source."""
    LIVE_PROVIDER = "live_provider"      # Authoritative, current provider knowledge (e.g., AWS MCP)
    INDEXED_CURATED = "indexed_curated"  # Curated Qdrant/Vector knowledge
    USER_PROVIDED = "user_provided"      # User/project provided knowledge


class KnowledgeSourceType(str, Enum):
    """Type of knowledge source."""
    QDRANT_INDEXED = "qdrant_indexed"        # Our Qdrant vector store
    AWS_MCP = "aws_mcp"                       # AWS MCP Server / Agent Toolkit
    GCP_MCP = "gcp_mcp"                       # GCP MCP (future)
    AZURE_MCP = "azure_mcp"                   # Azure MCP (future)
    USER_DOCUMENT = "user_document"           # User provided document
    MANUAL_ENTRY = "manual_entry"             # Manually entered knowledge


class KnowledgeSource(BaseModel):
    """Descriptor for a knowledge source."""
    source_id: str = Field(description="Unique identifier for this source")
    source_type: KnowledgeSourceType = Field(description="Type of knowledge source")
    provider: str = Field(description="Cloud provider (aws, gcp, azure, general, cncf)")
    name: str = Field(description="Human-readable name")
    description: str = Field(default="", description="Description of this knowledge source")
    authority: str = Field(description="Authority level: live_provider, indexed_curated, user_provided")
    base_url: str = Field(default="", description="Base URL for live sources")
    requires_auth: bool = Field(default=False, description="Whether authentication is required")
    is_live: bool = Field(default=False, description="Whether this source provides live/current data")
    last_indexed: Optional[datetime] = Field(default=None, description="When this source was last indexed (for indexed sources)")
    last_accessed: Optional[datetime] = Field(default=None, description="When this source was last accessed")
    metadata: dict = Field(default_factory=dict, description="Additional source metadata")

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class EvidenceAuthority(str, Enum):
    """Authority level of a piece of evidence."""
    LIVE_PROVIDER = "live_provider"
    INDEXED_CURATED = "indexed_curated"
    USER_PROVIDED = "user_provided"


class Evidence(BaseModel):
    """A single piece of retrieved evidence with full provenance."""
    evidence_id: str = Field(description="Unique identifier for this evidence")
    content: str = Field(description="The evidence content/text")
    source: str = Field(description="Source identifier (e.g., 'aws-mcp', 'cncf-glossary')")
    source_type: str = Field(description="Source type: live_provider, indexed_curated, user_provided")
    provider: str = Field(description="Cloud provider this evidence applies to")
    document_id: str = Field(description="Document/chunk identifier within source")
    title: str = Field(default="", description="Title or section heading")
    section_path: str = Field(default="", description="Section heading path")
    authority: str = Field(description="Authority level: live_provider, indexed_curated, user_provided")
    authority_level: int = Field(default=0, description="Numeric authority level for ranking (higher = more authoritative)")
    provider_specific: bool = Field(default=False, description="Whether this evidence is provider-specific")
    score: float = Field(default=0.0, description="Retrieval relevance score (0-1)")
    retrieval_time: datetime = Field(default_factory=datetime.utcnow, description="When this evidence was retrieved")
    source_retrieved_at: Optional[datetime] = Field(default=None, description="When the source document was originally retrieved/indexed")
    version: str = Field(default="", description="Document version/commit hash")
    url: str = Field(default="", description="URL to source document if available")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class EvidenceBundle(BaseModel):
    """A bundle of evidence retrieved for a query, with query context."""
    query: str = Field(description="The query that was executed")
    evidence: list[Evidence] = Field(default_factory=list, description="Retrieved evidence items")
    total_found: int = Field(default=0, description="Total number of evidence items found")
    retrieval_time: datetime = Field(default_factory=datetime.utcnow, description="When retrieval was performed")
    retrieval_metadata: dict = Field(default_factory=dict, description="Retrieval metadata (filters applied, etc.)")
    sources_queried: list[str] = Field(default_factory=list, description="Sources that were queried")

    model_config = ConfigDict(extra="forbid")


class RetrievalFilters(BaseModel):
    """Filters for retrieval operations."""
    provider: Optional[str] = Field(default=None, description="Filter by cloud provider")
    source: Optional[str] = Field(default=None, description="Filter by source identifier")
    document_type: Optional[str] = Field(default=None, description="Filter by document type")
    content_type: Optional[str] = Field(default=None, description="Filter by content type")
    architecture_patterns: list[str] = Field(default_factory=list, description="Filter by architecture patterns")
    knowledge_categories: list[str] = Field(default_factory=list, description="Filter by knowledge categories")
    well_architected_pillars: list[str] = Field(default_factory=list, description="Filter by Well-Architected pillars")
    provider_specific: Optional[bool] = Field(default=None, description="Filter by provider-specific flag")
    authority: Optional[str] = Field(default=None, description="Filter by authority level")
    source_types: list[str] = Field(default_factory=list, description="Filter by source types (qdrant_indexed, aws_mcp, etc.)")
    max_age_days: Optional[int] = Field(default=None, description="Maximum age of evidence in days")

    model_config = ConfigDict(extra="forbid")


class KnowledgeProvider(BaseModel):
    """Protocol/interface for knowledge providers."""
    source_id: str = Field(description="Unique identifier for this provider")
    source_type: KnowledgeSourceType = Field(description="Type of knowledge source")
    provider: str = Field(description="Cloud provider this source covers")
    name: str = Field(description="Human-readable name")
    is_live: bool = Field(default=False, description="Whether this provider offers live data")
    authority: str = Field(description="Authority level: live_provider, indexed_curated, user_provided")

    class Config:
        arbitrary_types_allowed = True

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilters] = None,
    ) -> list[Evidence]:
        """Retrieve evidence from this provider."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """Check if this provider is available/healthy."""
        raise NotImplementedError

    def get_source_info(self) -> "KnowledgeSource":
        """Get source descriptor for this provider."""
        return KnowledgeSource(
            source_id=self.source_id,
            source_type=self.source_type,
            provider=self.provider,
            name=self.name,
            authority=self.authority,
        )


class EvidenceBundleBuilder:
    """Builder for constructing EvidenceBundles from multiple providers."""

    def __init__(self):
        self._evidence: list[Evidence] = []
        self._query: str = ""
        self._filters: Optional[RetrievalFilters] = None
        self._sources_queried: list[str] = []

    def with_query(self, query: str) -> "EvidenceBundleBuilder":
        self._query = query
        return self

    def with_filters(self, filters: RetrievalFilters) -> "EvidenceBundleBuilder":
        self._filters = filters
        return self

    def add_evidence(self, evidence: list[Evidence]) -> "EvidenceBundleBuilder":
        self._evidence.extend(evidence)
        return self

    def add_source_queried(self, source_id: str) -> "EvidenceBundleBuilder":
        if source_id not in self._sources_queried:
            self._sources_queried.append(source_id)
        return self

    def build(self) -> "EvidenceBundle":
        # Sort by authority level (higher first) then by score
        authority_rank = {
            "live_provider": 3,
            "indexed_curated": 2,
            "user_provided": 1,
        }
        sorted_evidence = sorted(
            self._evidence,
            key=lambda e: (authority_rank.get(e.source_type, 0), e.score),
            reverse=True,
        )
        return EvidenceBundle(
            query=self._query,
            evidence=sorted_evidence,
            total_found=len(sorted_evidence),
            retrieval_metadata=self._filters.model_dump() if self._filters else {},
            sources_queried=self._sources_queried,
        )