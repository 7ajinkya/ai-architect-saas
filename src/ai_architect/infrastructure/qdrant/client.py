from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from ai_architect.config.settings import settings


def create_qdrant_client() -> QdrantClient:
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
    )


def ensure_collection(client: QdrantClient, collection_name: str | None = None, vector_size: int = 384) -> None:
    name = collection_name or settings.qdrant_collection_name
    collections = client.get_collections().collections
    exists = any(c.name == name for c in collections)

    if not exists:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        print(f"Created collection: {name}")
    else:
        print(f"Collection already exists: {name}")


def get_collection_info(client: QdrantClient, collection_name: str | None = None):
    name = collection_name or settings.qdrant_collection_name
    return client.get_collection(name)