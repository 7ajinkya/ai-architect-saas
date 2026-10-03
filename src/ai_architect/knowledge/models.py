from datetime import datetime
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class DocumentType(str, Enum):
    SPECIFICATION = "specification"
    ARCHITECTURE_PATTERN = "architecture_pattern"
    DESIGN_GUIDE = "design_guide"
    BEST_PRACTICE = "best_practice"
    REFERENCE_ARCHITECTURE = "reference_architecture"
    GLOSSARY = "glossary"
    TUTORIAL = "tutorial"
    CODE_EXAMPLE = "code_example"
    LAB = "lab"
    UNKNOWN = "unknown"


class Provider(str, Enum):
    GENERAL = "general"
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    CNCF = "cncf"
    UNKNOWN = "unknown"


class KnowledgeDocumentMetadata(BaseModel):
    source: str = Field(description="Source identifier (e.g., github repo name)")
    source_url: str = Field(description="Original source URL")
    source_name: str = Field(description="Human-readable source name")
    provider: Provider = Field(default=Provider.UNKNOWN, description="Cloud provider or general")
    topic: str = Field(default="", description="Primary topic/category")
    document_type: DocumentType = Field(default=DocumentType.UNKNOWN, description="Type of document")
    license: str = Field(default="unknown", description="License information")
    attribution: str = Field(default="", description="Attribution requirements")
    version: str = Field(default="", description="Document version/commit hash")
    retrieved_at: datetime = Field(default_factory=datetime.utcnow, description="When this was retrieved")
    file_path: str = Field(default="", description="Relative path within source repository")
    section_path: str = Field(default="", description="Heading hierarchy path (e.g., 'Architecture > Components > Networking')")
    content_type: str = Field(default="", description="Content classification: concept, pattern, case_study, reference_architecture, lab, template, code_sample")
    architecture_patterns: list[str] = Field(default_factory=list, description="Architecture pattern tags: microservices, event-driven, caching, sharding, load-balancing, etc.")
    knowledge_categories: list[str] = Field(default_factory=list, description="Knowledge categories: compute, storage, database, networking, messaging, security, observability, cost, reliability, scaling, integration")
    provider_specific: bool = Field(default=False, description="True if content is provider-specific (AWS/GCP/Azure) vs cross-cloud")
    well_architected_pillars: list[str] = Field(default_factory=list, description="Well-Architected pillars: cost-optimization, performance, reliability, security, operational-excellence, sustainability")
    service_integrations: list[str] = Field(default_factory=list, description="Service integrations demonstrated: e.g., 'API Gateway→SQS', 'AKS+Service Bus+Redis'")
    license_confirmed: bool = Field(default=False, description="Whether commercial-use license was verified")

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class KnowledgeChunk(BaseModel):
    content: str
    metadata: KnowledgeDocumentMetadata
    chunk_index: int = 0
    total_chunks: int = 1
    deterministic_id: str = ""

    model_config = ConfigDict(extra="forbid")


class SourceFilterConfig(BaseModel):
    include_paths: list[str] = Field(default_factory=list, description="Glob patterns to include")
    exclude_paths: list[str] = Field(default_factory=list, description="Glob patterns to exclude")
    include_extensions: list[str] = Field(default_factory=list, description="File extensions to include")
    exclude_extensions: list[str] = Field(default_factory=list, description="File extensions to exclude")
    max_file_size_kb: int = Field(default=500, description="Maximum file size in KB")
    require_license_clearance: bool = Field(default=True, description="Gate on license clearance")
    heading_aware_chunking: bool = Field(default=False, description="Use heading-aware chunking for markdown")
    preserve_code_fences: bool = Field(default=False, description="Preserve code fences as atomic chunks")
    strip_frontmatter: bool = Field(default=False, description="Strip YAML frontmatter before chunking")

    model_config = ConfigDict(extra="forbid")


class SourceConfig(BaseModel):
    name: str
    source_type: str = "git"
    url: str
    local_path: str | None = None
    provider: Provider = Provider.UNKNOWN
    topic: str = ""
    document_type: DocumentType = DocumentType.UNKNOWN
    license: str = "unknown"
    attribution: str = ""
    include_patterns: list[str] = Field(default_factory=lambda: ["**/*.md", "**/*.txt", "**/*.rst"])
    exclude_patterns: list[str] = Field(default_factory=lambda: ["**/node_modules/**", "**/.git/**", "**/__pycache__/**"])
    branch: str = "main"
    enabled: bool = True
    filter_config: SourceFilterConfig = Field(default_factory=SourceFilterConfig, description="Per-source filtering and chunking configuration")

    model_config = ConfigDict(extra="forbid", use_enum_values=True)