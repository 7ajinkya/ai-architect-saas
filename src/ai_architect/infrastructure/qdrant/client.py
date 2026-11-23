import logging

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from ai_architect.config.settings import settings


logger = logging.getLogger(__name__)


def create_qdrant_client() -> QdrantClient:
    # Passing an empty api_key against a local http:// URL triggers a
    # qdrant-client "insecure connection" UserWarning, so only send a key
    # when one is actually configured. Behavior with a real key is unchanged.
    kwargs: dict = {"url": settings.qdrant_url}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def ensure_collection(client: QdrantClient, collection_name: str | None = None, vector_size: int = 384) -> None:
    name = collection_name or settings.qdrant_collection_name
    collections = client.get_collections().collections
    exists = any(c.name == name for c in collections)

    if not exists:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        logger.debug("Created collection: %s", name)
    else:
        logger.debug("Collection already exists: %s", name)


def get_collection_info(client: QdrantClient, collection_name: str | None = None):
    name = collection_name or settings.qdrant_collection_name
    return client.get_collection(name)