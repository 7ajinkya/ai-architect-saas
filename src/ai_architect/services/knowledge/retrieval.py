from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field
from dataclasses import dataclass

from langchain_core.documents import Document
from qdrant_client.models import Filter, FieldCondition, MatchValue

from ai_architect.infrastructure.qdrant.vector_store import KnowledgeVectorStore
from ai_architect.services.knowledge.contracts import (
    Evidence,
    EvidenceBundle,
    RetrievalFilters,
    EvidenceAuthority,
)
from ai_architect.services.knowledge.providers.qdrant_provider import QdrantProvider
from ai_architect.services.knowledge.providers.aws_provider import AWSProvider


# Backward compatibility: old RetrievedChunk format
@dataclass
class RetrievedChunk:
    content: str
    source: str
    source_url: str
    source_name: str
    provider: str
    topic: str
    document_type: str
    file_path: str
    section_path: str
    content_type: str
    knowledge_categories: list[str]
    architecture_patterns: list[str]
    service_integrations: list[str]
    well_architected_pillars: list[str]
    license: str
    license_confirmed: bool
    score: float
    chunk_index: int
    total_chunks: int
    version: str
    retrieved_at: str


class RetrievalService:
    """High-level retrieval service that coordinates multiple knowledge providers."""

    def __init__(self):
        self.providers = {}
        self._providers_initialized = False
        self._ensure_providers_initialized()

    def _ensure_providers_initialized(self):
        """Lazy initialization of providers.

        Each provider is constructed defensively: several providers
        perform I/O in their constructors (e.g. the Qdrant provider
        verifies its collection on connect), and an unreachable
        dependency must degrade to "provider unavailable" — the same
        outcome as a retrieve-time failure — rather than aborting the
        whole retrieval with a connection error.
        """
        if self._providers_initialized:
            return
        for provider_id, factory in (
            ("qdrant-indexed", QdrantProvider),
            ("aws-mcp", AWSProvider),
        ):
            try:
                self.providers[provider_id] = factory()
            except Exception as e:
                print(
                    f"Warning: Provider {provider_id} initialization "
                    f"failed: {e}"
                )
        self._providers_initialized = True

    async def _ensure_provider_available(self, provider_id: str) -> bool:
        """Ensure a provider is initialized and available."""
        provider = self.providers.get(provider_id)
        if not provider:
            return False
        if not provider.is_available():
            try:
                await provider.initialize()
            except Exception as e:
                print(f"Warning: Provider {provider_id} initialization failed: {e}")
                return False
        return provider.is_available()

    def register_provider(self, provider_id: str, provider):
        """Register a new knowledge provider."""
        self.providers[provider_id] = provider

    def get_provider(self, provider_id: str):
        """Get a specific provider by ID."""
        return self.providers.get(provider_id)

    def list_providers(self) -> list[str]:
        """List available provider IDs."""
        return list(self.providers.keys())

    async def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional["RetrievalFilters"] = None,
        provider_ids: Optional[list[str]] = None,
    ) -> "EvidenceBundle":
        """
        Retrieve evidence from multiple providers and bundle results.

        Args:
            query: The search query
            top_k: Maximum number of results per provider
            filters: Optional retrieval filters
            provider_ids: Optional list of provider IDs to query (None = all available)

        Returns:
            EvidenceBundle with ranked evidence from all queried providers
        """
        from ai_architect.services.knowledge.contracts import EvidenceBundleBuilder, RetrievalFilters

        if filters is None:
            filters = RetrievalFilters()
        elif isinstance(filters, dict):
            filters = RetrievalFilters(**filters)

        # Determine which providers to query
        target_providers = provider_ids or list(self.providers.keys())

        builder = EvidenceBundleBuilder().with_query(query).with_filters(filters)

        for provider_id in target_providers:
            if not await self._ensure_provider_available(provider_id):
                continue
            provider = self.providers.get(provider_id)
            if not provider:
                continue

            try:
                evidence = await provider.retrieve(query=query, top_k=top_k, filters=filters)
                if evidence:
                    # Mark retrieval time
                    retrieval_time = datetime.utcnow()
                    for e in evidence:
                        e.retrieval_time = retrieval_time
                    builder.add_evidence(evidence).add_source_queried(provider_id)
            except Exception as e:
                # Log error but continue with other providers
                print(f"Warning: Provider {provider_id} failed: {e}")

        return builder.build()

    async def retrieve_simple(
        self,
        query: str,
        top_k: int = 5,
        provider: Optional[str] = None,
        document_type: Optional[str] = None,
        source: Optional[str] = None,
        content_type: Optional[str] = None,
        architecture_patterns: Optional[list[str]] = None,
        knowledge_categories: Optional[list[str]] = None,
        well_architected_pillars: Optional[list[str]] = None,
        provider_specific: Optional[bool] = None,
    ) -> "EvidenceBundle":
        """Simple retrieval with individual filter parameters (backward compatibility)."""
        filters = RetrievalFilters(
            provider=provider,
            document_type=document_type,
            source=source,
            content_type=content_type,
            architecture_patterns=architecture_patterns or [],
            knowledge_categories=knowledge_categories or [],
            well_architected_pillars=well_architected_pillars or [],
            provider_specific=provider_specific,
        )
        provider_ids = [provider] if provider else None
        return await self.retrieve(query, top_k, filters, provider_ids)

    async def retrieve_by_source(
        self,
        query: str,
        source: str,
        top_k: int = 5,
    ) -> "EvidenceBundle":
        return await self.retrieve_simple(query=query, top_k=top_k, source=source)

    async def retrieve_by_provider(
        self,
        query: str,
        provider: str,
        top_k: int = 5,
    ) -> "EvidenceBundle":
        return await self.retrieve_simple(query=query, top_k=top_k, provider=provider)

    async def retrieve_by_pattern(
        self,
        query: str,
        architecture_patterns: list[str],
        top_k: int = 5,
    ) -> "EvidenceBundle":
        return await self.retrieve_simple(query=query, top_k=top_k, architecture_patterns=architecture_patterns)

    def get_provider_info(self, provider_id: str) -> Optional[dict]:
        """Get information about a specific provider."""
        provider = self.providers.get(provider_id)
        if not provider:
            return None
        return provider.get_source_info().model_dump() if hasattr(provider, 'get_source_info') else {}


# Backward compatibility: keep the old interface working
class LegacyRetrievalService:
    """Backward compatible retrieval service returning old format."""

    def __init__(self, vector_store=None):
        self.service = RetrievalService()

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        provider: str | None = None,
        document_type: str | None = None,
        source: str | None = None,
        content_type: str | None = None,
        architecture_patterns: list[str] | None = None,
        knowledge_categories: list[str] | None = None,
        well_architected_pillars: list[str] | None = None,
        provider_specific: bool | None = None,
    ):
        import asyncio
        bundle = asyncio.run(self.service.retrieve_simple(
            query=query,
            top_k=top_k,
            provider=provider,
            document_type=document_type,
            source=source,
            content_type=content_type,
            architecture_patterns=architecture_patterns,
            knowledge_categories=knowledge_categories,
            well_architected_pillars=well_architected_pillars,
            provider_specific=provider_specific,
        ))

        # Convert EvidenceBundle to old RetrievedChunk format for backward compat
        from dataclasses import dataclass

        @dataclass
        class RetrievedChunk:
            content: str
            source: str
            source_url: str
            source_name: str
            provider: str
            topic: str
            document_type: str
            file_path: str
            section_path: str
            content_type: str
            knowledge_categories: list[str]
            architecture_patterns: list[str]
            service_integrations: list[str]
            well_architected_pillars: list[str]
            license: str
            license_confirmed: bool
            score: float
            chunk_index: int
            total_chunks: int
            version: str
            retrieved_at: str

        results = []
        for evidence in bundle.evidence:
            results.append(RetrievedChunk(
                content=evidence.content,
                source=evidence.source,
                source_url=evidence.url,
                source_name=evidence.source,
                provider=evidence.provider,
                topic="",
                document_type=evidence.document_id,
                file_path=evidence.metadata.get("file_path", ""),
                section_path=evidence.section_path,
                content_type=evidence.metadata.get("content_type", ""),
                knowledge_categories=evidence.metadata.get("knowledge_categories", []),
                architecture_patterns=evidence.metadata.get("architecture_patterns", []),
                service_integrations=[],
                well_architected_pillars=evidence.metadata.get("well_architected_pillars", []),
                license=evidence.metadata.get("license", ""),
                license_confirmed=evidence.metadata.get("license_confirmed", False),
                score=evidence.score,
                chunk_index=evidence.metadata.get("chunk_index", 0),
                total_chunks=evidence.metadata.get("total_chunks", 1),
                version=evidence.version,
                retrieved_at=evidence.retrieval_time.isoformat() if evidence.retrieval_time else "",
            ))
        return results


# Factory function for easy access
def get_retrieval_service() -> RetrievalService:
    """Get the global retrieval service instance."""
    if not hasattr(get_retrieval_service, "_instance"):
        get_retrieval_service._instance = RetrievalService()
    return get_retrieval_service._instance