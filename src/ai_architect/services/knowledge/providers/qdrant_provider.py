from typing import Optional, List
from datetime import datetime

from langchain_core.documents import Document
from qdrant_client.models import Filter, FieldCondition, MatchValue

from ai_architect.infrastructure.qdrant.vector_store import KnowledgeVectorStore
from ai_architect.services.knowledge.contracts import (
    Evidence,
    EvidenceAuthority,
    RetrievalFilters,
    KnowledgeSource,
    KnowledgeSourceType,
)


class QdrantProvider:
    """Qdrant vector store provider for indexed curated knowledge."""

    def __init__(self, vector_store: KnowledgeVectorStore | None = None):
        self.vector_store = vector_store or KnowledgeVectorStore()
        self.source_id = "qdrant-indexed"
        self.source_type = KnowledgeSourceType.QDRANT_INDEXED
        self.provider = "general"
        self.name = "Qdrant Indexed Architecture Knowledge"
        self.authority = "indexed_curated"
        self.is_live = False

    def _build_qdrant_filter(self, filters: Optional[RetrievalFilters]) -> Optional[object]:
        """Build Qdrant filter from retrieval filters."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        conditions = []

        if filters:
            if filters.provider:
                conditions.append(FieldCondition(key="metadata.provider", match=MatchValue(value=filters.provider)))
            if filters.document_type:
                conditions.append(FieldCondition(key="metadata.document_type", match=MatchValue(value=filters.document_type)))
            if filters.source:
                conditions.append(FieldCondition(key="metadata.source", match=MatchValue(value=filters.source)))
            if filters.content_type:
                conditions.append(FieldCondition(key="metadata.content_type", match=MatchValue(value=filters.content_type)))
            if filters.architecture_patterns:
                for pattern in filters.architecture_patterns:
                    conditions.append(FieldCondition(key="metadata.architecture_patterns", match=MatchValue(value=pattern)))
            if filters.knowledge_categories:
                for category in filters.knowledge_categories:
                    conditions.append(FieldCondition(key="metadata.knowledge_categories", match=MatchValue(value=category)))
            if filters.well_architected_pillars:
                for pillar in filters.well_architected_pillars:
                    conditions.append(FieldCondition(key="metadata.well_architected_pillars", match=MatchValue(value=pillar)))
            if filters.provider_specific is not None:
                conditions.append(FieldCondition(key="metadata.provider_specific", match=MatchValue(value=filters.provider_specific)))

        if not conditions:
            return None
        return Filter(must=conditions)

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilters] = None,
    ) -> List[Evidence]:
        """Retrieve evidence from Qdrant vector store."""
        qdrant_filter = self._build_qdrant_filter(filters)
        results = self.vector_store.vector_store.similarity_search_with_score(
            query=query,
            k=top_k,
            filter=qdrant_filter,
        )

        evidence_list = []
        for doc, score in results:
            meta = doc.metadata
            evidence = Evidence(
                evidence_id=f"qdrant:{meta.get('deterministic_id', meta.get('source', '') + str(meta.get('chunk_index', 0)))}",
                content=doc.page_content,
                source="qdrant-indexed",
                source_type="indexed_curated",
                provider=meta.get("provider", "general"),
                document_id=meta.get("deterministic_id", f"chunk_{meta.get('chunk_index', 0)}"),
                title=meta.get("section_path", "").split(" > ")[-1] if meta.get("section_path") else "",
                section_path=meta.get("section_path", ""),
                authority=EvidenceAuthority.INDEXED_CURATED,
                authority_level=2,
                provider_specific=meta.get("provider_specific", False),
                score=score,
                retrieval_time=datetime.utcnow(),
                source_retrieved_at=None,
                version=meta.get("version", ""),
                url=meta.get("source_url", ""),
                metadata={
                    "file_path": meta.get("file_path", ""),
                    "content_type": meta.get("content_type", ""),
                    "knowledge_categories": meta.get("knowledge_categories", []),
                    "architecture_patterns": meta.get("architecture_patterns", []),
                    "service_integrations": meta.get("service_integrations", []),
                    "well_architected_pillars": meta.get("well_architected_pillars", []),
                    "license": meta.get("license", ""),
                    "license_confirmed": meta.get("license_confirmed", False),
                },
            )
            evidence_list.append(evidence)

        return evidence_list

    def is_available(self) -> bool:
        try:
            stats = self.vector_store.get_stats()
            return stats.get("status") == "green"
        except Exception:
            return False

    def get_source_info(self):
        from ai_architect.services.knowledge.contracts import KnowledgeSource, KnowledgeSourceType
        return KnowledgeSource(
            source_id=self.source_id,
            source_type=KnowledgeSourceType.QDRANT_INDEXED,
            provider=self.provider,
            name=self.name,
            authority="indexed_curated",
            is_live=self.is_live,
            last_indexed=None,
            metadata={"collection": self.vector_store.collection_name},
        )