import os
import hashlib
import fnmatch
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from git import Repo
from langchain_core.documents import Document

from ai_architect.config.settings import settings
from ai_architect.infrastructure.embeddings.factory import create_embeddings
from ai_architect.knowledge.models import (
    SourceConfig,
    KnowledgeDocumentMetadata,
    KnowledgeChunk,
    Provider,
    DocumentType,
    SourceFilterConfig,
)
from ai_architect.services.knowledge.chunking import create_markdown_splitter


def compute_deterministic_chunk_id(
    source_name: str,
    file_path: str,
    chunk_index: int,
    content: str,
) -> str:
    """
    Compute a deterministic chunk ID from stable chunk identity.

    The ID is derived from:
    - source name
    - relative file path within source
    - chunk index within the file
    - content hash (SHA-256 truncated to 16 chars)

    This ensures the same logical chunk always gets the same ID,
    while changes to content produce a different ID.
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    identity = f"{source_name}:{file_path}:{chunk_index}:{content_hash}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


class SourceFetcher:
    def fetch(self, source: SourceConfig, local_base: Path) -> Path:
        if source.source_type == "git":
            return self._fetch_git(source, local_base)
        elif source.source_type == "web":
            return self._fetch_web(source, local_base)
        else:
            raise ValueError(f"Unsupported source type: {source.source_type}")

    def _fetch_git(self, source: SourceConfig, local_base: Path) -> Path:
        local_path = local_base / source.name
        if local_path.exists():
            repo = Repo(local_path)
            repo.remotes.origin.pull()
        else:
            Repo.clone_from(source.url, local_path, branch=source.branch)
        return local_path

    def _fetch_web(self, source: SourceConfig, local_base: Path) -> Path:
        raise NotImplementedError("Web source fetching not implemented in this slice. Use local snapshots.")


class DocumentParser:
    def __init__(self):
        self._common_exclude_extensions = {
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".ico",
            ".svg", ".woff", ".woff2", ".ttf", ".eot", ".otf",
            ".mp4", ".webm", ".mov", ".avi", ".mp3", ".wav", ".ogg",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".zip", ".tar", ".gz", ".rar", ".7z",
            ".exe", ".dll", ".so", ".dylib",
            ".class", ".jar", ".war", ".ear",
            ".pyc", ".pyo", ".pyd",
        }

    def parse(self, source: SourceConfig, source_path: Path) -> Iterator[tuple[str, Path]]:
        filter_config = source.filter_config

        # Build include patterns
        include_patterns = filter_config.include_paths if filter_config.include_paths else source.include_patterns
        if not include_patterns:
            include_patterns = ["**/*.md", "**/*.txt", "**/*.rst", "**/*.yaml", "**/*.yml", "**/*.tf", "**/*.tfvars"]

        # Build exclude patterns
        exclude_patterns = list(filter_config.exclude_paths) + list(source.exclude_patterns)

        # Determine allowed extensions
        allowed_extensions = set(filter_config.include_extensions) if filter_config.include_extensions else None
        exclude_extensions = set(filter_config.exclude_extensions) | self._common_exclude_extensions

        for pattern in include_patterns:
            # Use the full pattern with rglob - Path.rglob supports ** patterns
            for file_path in source_path.rglob(pattern):
                if not file_path.is_file():
                    continue

                # Check exclude patterns
                if self._should_exclude(file_path, source_path, exclude_patterns):
                    continue

                # Check extension
                ext = file_path.suffix.lower()
                if ext in exclude_extensions:
                    continue
                if allowed_extensions and ext not in allowed_extensions:
                    continue

                # Check file size
                try:
                    file_size_kb = file_path.stat().st_size / 1024
                    if file_size_kb > filter_config.max_file_size_kb:
                        continue
                except Exception:
                    continue

                # Read content
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if content.strip():
                        yield content, file_path.relative_to(source_path)
                except Exception:
                    continue

    def _should_exclude(self, file_path: Path, source_path: Path, exclude_patterns: list[str]) -> bool:
        try:
            rel_path = file_path.relative_to(source_path)
            rel_str = str(rel_path).replace("\\", "/")
        except Exception:
            return True

        for pattern in exclude_patterns:
            # Normalize pattern for matching
            norm_pattern = pattern.replace("**/", "**/").replace("\\", "/")
            if fnmatch.fnmatch(rel_str, norm_pattern) or fnmatch.fnmatch(file_path.name, norm_pattern):
                return True
        return False


class DocumentNormalizer:
    def normalize(
        self,
        content: str,
        source: SourceConfig,
        rel_path: Path,
        source_path: Path,
    ) -> KnowledgeDocumentMetadata:
        commit_hash = self._get_commit_hash(source_path) if source.source_type == "git" else ""

        # Extract basic metadata from content
        topic = self._extract_topic(content, source)
        content_type = self._classify_content_type(content, source)
        architecture_patterns = self._extract_architecture_patterns(content)
        knowledge_categories = self._extract_knowledge_categories(content)
        well_architected_pillars = self._extract_well_architected_pillars(content)
        service_integrations = self._extract_service_integrations(content)

        return KnowledgeDocumentMetadata(
            source=source.name,
            source_url=source.url,
            source_name=source.name.replace("-", " ").title(),
            provider=source.provider,
            topic=topic,
            document_type=source.document_type,
            license=source.license,
            attribution=source.attribution,
            version=commit_hash[:8] if commit_hash else "",
            retrieved_at=datetime.utcnow(),
            file_path=str(rel_path),
            content_type=content_type,
            architecture_patterns=architecture_patterns,
            knowledge_categories=knowledge_categories,
            provider_specific=source.provider != Provider.GENERAL and source.provider != Provider.CNCF,
            well_architected_pillars=well_architected_pillars,
            service_integrations=service_integrations,
            license_confirmed=True,  # All approved sources have verified licenses
        )

    def _get_commit_hash(self, repo_path: Path) -> str:
        try:
            repo = Repo(repo_path)
            return repo.head.commit.hexsha
        except Exception:
            return ""

    def _extract_topic(self, content: str, source: SourceConfig) -> str:
        # Use source topic as base, try to refine from content
        base_topic = source.topic
        # Look for common topic indicators in headings
        heading_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        if heading_match:
            return heading_match.group(1).strip()[:100]
        return base_topic

    def _classify_content_type(self, content: str, source: SourceConfig) -> str:
        content_lower = content.lower()
        if source.document_type == DocumentType.GLOSSARY:
            return "concept"
        if source.document_type == DocumentType.LAB:
            return "lab"
        if source.document_type == DocumentType.ARCHITECTURE_PATTERN:
            return "pattern"
        if source.document_type == DocumentType.REFERENCE_ARCHITECTURE:
            if "case study" in content_lower or "case_study" in content_lower:
                return "case_study"
            return "reference_architecture"
        if "tutorial" in content_lower or "how to" in content_lower or "step by step" in content_lower:
            return "tutorial"
        if re.search(r'```(terraform|bicep|yaml|yml|json)', content):
            return "template"
        if re.search(r'```(python|javascript|typescript|csharp|java|go)', content):
            return "code_sample"
        return "concept"

    def _extract_architecture_patterns(self, content: str) -> list[str]:
        patterns = []
        pattern_keywords = {
            "microservices": ["microservice", "micro-service"],
            "event-driven": ["event-driven", "event driven", "event sourcing", "event sourcing"],
            "caching": ["cache", "caching", "redis", "memcached"],
            "sharding": ["shard", "sharding"],
            "load-balancing": ["load balanc", "load-balanc"],
            "circuit-breaker": ["circuit breaker", "circuit-breaker"],
            "retry": ["retry", "retry policy", "retry logic"],
            "timeout": ["timeout", "timeout policy"],
            "bulkhead": ["bulkhead"],
            "sidecar": ["sidecar"],
            "ambassador": ["ambassador pattern"],
            "gatekeeper": ["gatekeeper"],
            "claim-check": ["claim check", "claim-check"],
            "pipes-and-filters": ["pipes and filters", "pipes-and-filters"],
            "choreography": ["choreography"],
            "orchestration": ["orchestration"],
            "saga": ["saga pattern", "saga"],
            "cqrs": ["cqrs", "command query responsibility"],
            "event-sourcing": ["event sourcing"],
            "cell-based": ["cell-based", "cell based"],
            "gitops": ["gitops", "git-ops"],
            "blue-green": ["blue-green", "blue/green", "blue green deployment"],
            "canary": ["canary deployment", "canary release"],
            "rolling-update": ["rolling update", "rolling deployment"],
            "feature-flag": ["feature flag", "feature toggle"],
            "strangler-fig": ["strangler fig"],
            "api-gateway": ["api gateway", "api-gateway"],
            "bff": ["backend for frontend", "bff"],
            "service-mesh": ["service mesh", "istio", "linkerd"],
            "serverless": ["serverless", "lambda", "cloud function", "cloud run"],
            "container": ["container", "kubernetes", "k8s", "docker"],
            "multi-region": ["multi-region", "multi region", "cross-region"],
            "active-active": ["active-active", "active/active"],
            "active-passive": ["active-passive", "active/passive"],
            "disaster-recovery": ["disaster recovery", "dr plan", "rpo", "rto"],
            "backup-restore": ["backup", "restore", "point-in-time recovery"],
        }

        content_lower = content.lower()
        for pattern, keywords in pattern_keywords.items():
            if any(kw in content_lower for kw in keywords):
                patterns.append(pattern)

        return patterns

    def _extract_knowledge_categories(self, content: str) -> list[str]:
        categories = []
        category_keywords = {
            "compute": ["compute", "ec2", "vm", "instance", "lambda", "function", "container", "pod", "kubernetes", "k8s", "ecs", "fargate", "cloud run"],
            "storage": ["storage", "s3", "blob", "ebs", "efs", "persistent volume", "pvc", "object storage", "file storage", "block storage"],
            "database": ["database", "db", "sql", "nosql", "dynamodb", "cosmos", "rds", "aurora", "postgres", "mysql", "cassandra", "mongodb", "redis", "cosmosdb"],
            "networking": ["network", "vpc", "subnet", "vnet", "load balancer", "api gateway", "dns", "cdn", "firewall", "security group", "nacl", "private link", "peering"],
            "messaging": ["queue", "topic", "pub/sub", "pubsub", "service bus", "event bridge", "eventbridge", "kafka", "rabbitmq", "sns", "sqs", "event grid"],
            "security": ["security", "iam", "rbac", "encryption", "key vault", "kms", "secret", "certificate", "tls", "ssl", "mfa", "identity", "authentication", "authorization", "zero trust"],
            "observability": ["monitoring", "logging", "tracing", "metrics", "alerting", "dashboard", "grafana", "prometheus", "cloudwatch", "app insights", "datadog", "otel", "open telemetry"],
            "cost": ["cost", "pricing", "billing", "finops", "optimization", "savings", "budget", "anomaly"],
            "reliability": ["reliability", "availability", "ha", "high availability", "fault tolerance", "resilience", "chaos", "failover", "rpo", "rto", "sla", "slo"],
            "scaling": ["scaling", "autoscaling", "horizontal scaling", "vertical scaling", "scale out", "scale up", "elasticity", "throughput"],
            "integration": ["integration", "webhook", "api", "event-driven", "async", "synchronous", "rest", "grpc", "graphql"],
        }

        content_lower = content.lower()
        for category, keywords in category_keywords.items():
            if any(kw in content_lower for kw in keywords):
                categories.append(category)

        return categories

    def _extract_well_architected_pillars(self, content: str) -> list[str]:
        pillars = []
        pillar_keywords = {
            "cost-optimization": ["cost optimization", "cost-optimization", "finops", "cost saving", "cost reduction"],
            "performance": ["performance", "latency", "throughput", "optimization", "efficiency"],
            "reliability": ["reliability", "availability", "fault tolerance", "resilience", "chaos engineering"],
            "security": ["security", "encryption", "iam", "identity", "compliance", "zero trust"],
            "operational-excellence": ["operational excellence", "ops", "monitoring", "alerting", "automation", "runbook", "incident"],
            "sustainability": ["sustainability", "carbon", "green", "energy efficiency"],
        }

        content_lower = content.lower()
        for pillar, keywords in pillar_keywords.items():
            if any(kw in content_lower for kw in keywords):
                pillars.append(pillar)

        return pillars

    def _extract_service_integrations(self, content: str) -> list[str]:
        integrations = []
        # Common service integration patterns
        integration_patterns = {
            "API Gateway→SQS": ["api gateway", "sqs"],
            "API Gateway→Lambda": ["api gateway", "lambda"],
            "EventBridge→Lambda": ["eventbridge", "event bridge", "lambda"],
            "S3→Lambda": ["s3", "lambda"],
            "DynamoDB→Lambda": ["dynamodb", "lambda"],
            "EventBridge→Step Functions": ["eventbridge", "step functions"],
            "API Gateway→EventBridge": ["api gateway", "eventbridge"],
            "AKS+Service Bus+Redis": ["aks", "service bus", "redis"],
            "AKS+Cosmos DB": ["aks", "cosmos db", "cosmosdb"],
            "GKE+Cloud SQL": ["gke", "cloud sql"],
            "GKE+Pub/Sub": ["gke", "pub/sub", "pubsub"],
            "Cloud Run+Cloud SQL": ["cloud run", "cloud sql"],
            "Cloud Functions+Firestore": ["cloud function", "firestore"],
            "API Gateway→DynamoDB": ["api gateway", "dynamodb"],
            "Lambda→SNS": ["lambda", "sns"],
            "Lambda→EventBridge": ["lambda", "eventbridge"],
            "Kinesis→Lambda": ["kinesis", "lambda"],
            "SQS→Lambda": ["sqs", "lambda"],
            "API Gateway→Kinesis": ["api gateway", "kinesis"],
        }

        content_lower = content.lower()
        for integration, keywords in integration_patterns.items():
            if all(kw in content_lower for kw in keywords):
                integrations.append(integration)

        return integrations


class DocumentChunker:
    def __init__(self, source: SourceConfig):
        self.source = source
        filter_config = source.filter_config
        self.splitter = create_markdown_splitter(
            chunk_size=filter_config.max_file_size_kb if filter_config.max_file_size_kb > 100 else 1000,
            chunk_overlap=200,
            heading_aware=filter_config.heading_aware_chunking,
            preserve_code_fences=filter_config.preserve_code_fences,
            strip_frontmatter=filter_config.strip_frontmatter,
        )

    def chunk(self, content: str, metadata: KnowledgeDocumentMetadata, rel_path: Path) -> list[KnowledgeChunk]:
        # Add file path to metadata
        base_metadata = {
            "file_path": str(rel_path),
            "section_path": "",
            "has_code_fences": False,
        }

        docs = self.splitter.split_text(content, metadata=base_metadata)
        chunks = []

        for i, doc in enumerate(docs):
            chunk_meta = metadata.model_copy()
            # Update section_path from splitter metadata
            if "section_path" in doc.metadata:
                chunk_meta.section_path = doc.metadata["section_path"]
            chunk_content = doc.page_content
            deterministic_id = compute_deterministic_chunk_id(
                source_name=self.source.name,
                file_path=str(rel_path),
                chunk_index=i,
                content=chunk_content,
            )
            chunks.append(KnowledgeChunk(
                content=chunk_content,
                metadata=chunk_meta,
                chunk_index=i,
                total_chunks=len(docs),
                deterministic_id=deterministic_id,
            ))
        return chunks


class IngestionPipeline:
    def __init__(self, local_base: Path):
        self.local_base = local_base
        self.fetcher = SourceFetcher()
        self.parser = DocumentParser()
        self.normalizer = DocumentNormalizer()
        self.embeddings = create_embeddings()

    def process_source(self, source: SourceConfig) -> list[KnowledgeChunk]:
        source_path = self.fetcher.fetch(source, self.local_base)
        all_chunks = []

        chunker = DocumentChunker(source)

        for content, rel_path in self.parser.parse(source, source_path):
            metadata = self.normalizer.normalize(content, source, rel_path, source_path)
            chunks = chunker.chunk(content, metadata, rel_path)
            all_chunks.extend(chunks)

        return all_chunks

    def process_sources(self, sources: list[SourceConfig]) -> dict[str, list[KnowledgeChunk]]:
        results = {}
        for source in sources:
            print(f"Processing source: {source.name}...")
            chunks = self.process_source(source)
            results[source.name] = chunks
            print(f"  Created {len(chunks)} chunks from {source.name}")
        return results


def compute_chunk_id(chunk: KnowledgeChunk) -> str:
    content_hash = hashlib.sha256(chunk.content.encode()).hexdigest()[:16]
    source_id = hashlib.sha256(chunk.metadata.source.encode()).hexdigest()[:8]
    return f"{source_id}-{chunk.chunk_index}-{content_hash}"


# Import re for regex operations in normalizer
import re