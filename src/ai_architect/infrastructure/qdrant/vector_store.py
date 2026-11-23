import logging

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, Filter, PointStruct
from langchain_qdrant import QdrantVectorStore
from langchain_core.documents import Document

from ai_architect.config.settings import settings
from ai_architect.infrastructure.embeddings.factory import create_embeddings
from ai_architect.infrastructure.qdrant.client import create_qdrant_client, ensure_collection
from ai_architect.knowledge.models import KnowledgeChunk


logger = logging.getLogger(__name__)


class KnowledgeVectorStore:
    def __init__(self, collection_name: str | None = None):
        self.collection_name = collection_name or settings.qdrant_collection_name
        self.embeddings = create_embeddings()
        self.client = create_qdrant_client()

        sample_embedding = self.embeddings.embed_query("test")
        ensure_collection(self.client, self.collection_name, len(sample_embedding))

        self.vector_store = QdrantVectorStore(
            client=self.client,
            collection_name=self.collection_name,
            embedding=self.embeddings,
        )

    def index_chunks(self, chunks: list[KnowledgeChunk], skip_existing: bool = True) -> int:
        if skip_existing and self._collection_has_data():
            logger.debug(
                "Collection %s already has data. Skipping full re-index.",
                self.collection_name,
            )
            return 0

        points = []
        for chunk in chunks:
            # Use deterministic ID if available, otherwise generate one
            point_id = chunk.deterministic_id if chunk.deterministic_id else self._generate_fallback_id(chunk)
            
            embedding = self.embeddings.embed_query(chunk.content)
            
            payload = {
                "page_content": chunk.content,
                "metadata": {
                    "source": chunk.metadata.source,
                    "source_url": chunk.metadata.source_url,
                    "source_name": chunk.metadata.source_name,
                    "provider": chunk.metadata.provider,
                    "topic": chunk.metadata.topic,
                    "document_type": chunk.metadata.document_type,
                    "license": chunk.metadata.license,
                    "attribution": chunk.metadata.attribution,
                    "version": chunk.metadata.version,
                    "retrieved_at": chunk.metadata.retrieved_at.isoformat(),
                    "chunk_index": chunk.chunk_index,
                    "total_chunks": chunk.total_chunks,
                    "file_path": chunk.metadata.file_path,
                    "section_path": chunk.metadata.section_path,
                    "content_type": chunk.metadata.content_type,
                    "knowledge_categories": chunk.metadata.knowledge_categories,
                    "architecture_patterns": chunk.metadata.architecture_patterns,
                    "service_integrations": chunk.metadata.service_integrations,
                    "well_architected_pillars": chunk.metadata.well_architected_pillars,
                    "provider_specific": chunk.metadata.provider_specific,
                    "license": chunk.metadata.license,
                    "license_confirmed": chunk.metadata.license_confirmed,
                },
            }
            
            points.append(PointStruct(
                id=point_id,
                vector=embedding,
                payload=payload,
            ))

        # Use upsert with deterministic IDs - replaces existing points with same ID
        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )
        return len(points)

    def _generate_fallback_id(self, chunk: KnowledgeChunk) -> str:
        """Generate a fallback ID for chunks without deterministic_id (backward compatibility)."""
        import hashlib
        identity = f"{chunk.metadata.source}:{chunk.metadata.file_path}:{chunk.chunk_index}:{chunk.content[:100]}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def _collection_has_data(self) -> bool:
        try:
            info = self.client.get_collection(self.collection_name)
            return info.points_count > 0
        except Exception:
            return False

    def get_retriever(self, k: int = 5):
        return self.vector_store.as_retriever(search_kwargs={"k": k})

    def similarity_search(self, query: str, k: int = 5, filter: Filter | None = None) -> list:
        return self.vector_store.similarity_search(query, k=k, filter=filter)

    def similarity_search_with_score(
        self, query: str, k: int = 5, filter: Filter | None = None
    ) -> list[tuple[Document, float]]:
        return self.vector_store.similarity_search_with_score(query, k=k, filter=filter)

    def get_stats(self) -> dict:
        info = self.client.get_collection(self.collection_name)
        return {
            "collection_name": self.collection_name,
            "points_count": info.points_count,
            "status": info.status,
        }