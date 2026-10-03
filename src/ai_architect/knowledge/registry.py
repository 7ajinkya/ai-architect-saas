from pathlib import Path
from ai_architect.knowledge.models import SourceConfig, Provider, DocumentType, SourceFilterConfig


# Common exclusion patterns for repository noise
COMMON_EXCLUDE_PATHS = [
    "**/.github/**",
    "**/.git/**",
    "**/.gitignore",
    "**/.gitattributes",
    "**/.gitmodules",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/.pytest_cache/**",
    "**/.coverage/**",
    "**/coverage/**",
    "**/htmlcov/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/out/**",
    "**/.next/**",
    "**/.nuxt/**",
    "**/.vercel/**",
    "**/.netlify/**",
    "**/vendor/**",
    "**/venv/**",
    "**/.venv/**",
    "**/env/**",
    "**/.env**",
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/requirements.txt",
    "**/requirements-dev.txt",
    "**/Pipfile.lock",
    "**/poetry.lock",
    "**/Cargo.lock",
    "**/go.sum",
    "**/composer.lock",
    "**/Gemfile.lock",
    "**/*.log",
    "**/*.tmp",
    "**/*.temp",
    "**/.DS_Store",
    "**/Thumbs.db",
    "**/*.swp",
    "**/*.swo",
    "**/*~",
    "**/.idea/**",
    "**/.vscode/**",
    "**/*.iml",
    "**/*.suo",
    "**/*.sln.docstates",
]

COMMON_EXCLUDE_EXTENSIONS = [
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".ico",
    ".svg", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".webm", ".mov", ".avi", ".mp3", ".wav", ".ogg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".war", ".ear",
    ".pyc", ".pyo", ".pyd",
]

# CNCF Glossary - English only, exclude translations and Hugo infrastructure
CNCF_GLOSSARY_FILTER = SourceFilterConfig(
    include_paths=["content/en/**/*.md"],
    exclude_paths=[
        "content/bn/**", "content/de/**", "content/es/**", "content/fr/**",
        "content/hi/**", "content/it/**", "content/ja/**", "content/pt-br/**",
        "content/ru/**", "content/tr/**", "content/zh-cn/**", "content/zh-tw/**",
        "layouts/**", "static/**", "themes/**", "archetypes/**", "i18n/**",
        "config.toml", "config.yaml", "config.yml", "package.json", "package-lock.json",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".github/**", "scripts/**", "Makefile", "*.sh",
    ],
    exclude_extensions=COMMON_EXCLUDE_EXTENSIONS + [".toml", ".yaml", ".yml"],
    max_file_size_kb=200,
    heading_aware_chunking=True,
    strip_frontmatter=True,
)

# CNCF Architecture - Case studies and architecture docs only
CNCF_ARCHITECTURE_FILTER = SourceFilterConfig(
    include_paths=["content/en/architectures/**/*.md"],
    exclude_paths=[
        "layouts/**", "static/**", "themes/**", "archetypes/**",
        "assets/**", "images/**", "scripts/**", "config/**", "data/**", "data-definitions/**",
        "informal/**", "meta/**", "packages/**", ".github/**",
        "config.toml", "package.json", "Dockerfile", "docker-compose.yml",
        "*.sh", "Makefile",
    ],
    max_file_size_kb=500,
    heading_aware_chunking=True,
)

# AWS Well-Architected Labs - Lab documentation and CloudFormation templates
AWS_WELL_ARCHITECTED_FILTER = SourceFilterConfig(
    include_paths=[
        "static/Cost/**/*.md",
        "static/Performance/**/*.md",
        "static/Reliability/**/*.md",
        "static/Operations/**/*.md",
        "static/Security/**/*.md",
        "static/Sustainability/**/*.md",
        "content/**/*.md",
        "static/**/*.yaml",
        "static/**/*.yml",
        "static/**/*.json",
    ],
    exclude_paths=[
        "static/**/Images/**", "static/**/images/**",
        "static/**/Code/**/*.py", "static/**/Code/**/*.js", "static/**/Code/**/*.java",
        "static/**/Code/**/*.cs", "static/**/Code/**/*.go", "static/**/Code/**/*.rb",
        "layouts/**", "archetypes/**", "themes/**", "assets/**",
        "deployment/**", ".github/**", "scripts/**", "config/**",
        "config.toml", "package.json", "Dockerfile", "deploy.sh", "netlify.toml",
    ],
    exclude_extensions=COMMON_EXCLUDE_EXTENSIONS,
    max_file_size_kb=1000,
    heading_aware_chunking=True,
    preserve_code_fences=True,
)

# AWS Serverless Patterns - Pattern READMEs and SAM templates
AWS_SERVERLESS_PATTERNS_FILTER = SourceFilterConfig(
    include_paths=["*/README.md", "*/template.yaml", "*/template.yml", "*/samconfig.toml"],
    exclude_paths=[
        "*/src/**", "*/tests/**", "*/test/**", "*/.github/**", "*/_scripts/**",
        "*/.idea/**", "*/.vscode/**", "*/*.csproj", "*/*.sln", "*/pom.xml",
        "*/build.gradle", "*/package.json", "*/requirements.txt",
    ],
    include_extensions=[".md", ".yaml", ".yml", ".toml"],
    max_file_size_kb=500,
    heading_aware_chunking=True,
    preserve_code_fences=True,
)

# AWS Serverless Samples - Selective: only architectural READMEs
AWS_SERVERLESS_SAMPLES_FILTER = SourceFilterConfig(
    include_paths=[
        "apigw-eventbridge-schema-validation/README.md",
        "multiregional-private-api/README.md",
        "queue-based-ingestion/README.md",
        "streaming-serverless-fraud-detection/README.md",
        "multi-account-private-apigw-ecs-sam/README.md",
        "apigw-mtls-multi-tenant/README.md",
        "owasp-api-security-controls-demo/README.md",
        "apigw-response-streaming/README.md",
        "apigw-header-routing/README.md",
        "fargate-private-api/README.md",
        "apigw-log-analytic/README.md",
        "apigw-ws-integrations/README.md",
        "apigw-readme-integration/README.md",
        "fargate-rest-api/README.md",
        "lambda-ecs-dual-deploy/README.md",
    ],
    exclude_paths=["**/*"],
    max_file_size_kb=500,
    heading_aware_chunking=True,
)

# GCP Architecture Samples - Architecture READMEs + key Terraform
GCP_ARCHITECTURE_FILTER = SourceFilterConfig(
    include_paths=[
        "*/README.md",
        "gen-ai-rag-vertex-ai-vector-search/*.tf",
        "oracle-ebs-framework/*.tf",
        "oracle-ebs-gemini-enterprise-mcp/*.tf",
        "oracle-jde-framework/*.tf",
        "oracle-peoplesoft-framework/*.tf",
        "terraform-google-multi-tenant-agentic-ai/*.tf",
    ],
    exclude_paths=[
        "*/.github/**", "*/scripts/**", "*/tests/**", "*/test/**",
        "*/.idea/**", "*/.vscode/**", "*/*.pyc", "*/__pycache__/**",
    ],
    include_extensions=[".md", ".tf", ".tfvars"],
    max_file_size_kb=500,
    heading_aware_chunking=True,
    preserve_code_fences=True,
)

# Azure Architecture Center - Patterns, reference architectures, best practices
AZURE_ARCHITECTURE_FILTER = SourceFilterConfig(
    include_paths=[
        "docs/patterns/*.md",
        "docs/reference-architectures/**/*.md",
        "docs/best-practices/*.md",
        "docs/guide/architecture-styles/*.md",
        "docs/guide/technology-choices/*.md",
        "docs/guide/devops/*.md",
        "docs/guide/security/*.md",
        "docs/guide/networking/*.md",
        "docs/guide/cost-optimization/*.md",
        "docs/guide/reliability/*.md",
        "docs/guide/performance-efficiency/*.md",
    ],
    exclude_paths=[
        "includes/**", "scripts/**", "_images/**", "_bread/**",
        "aws-professional/**", "gcp-professional/**",
        "virtual-desktop/**", "virtual-machines/**",
        "web-apps/**", "containers/**", "databases/**",
        "analytics/**", "ai-ml/**", "identity/**", "landing-zones/**",
        "migration/**", "operator-guides/**", "pattern-implementations/**",
        "solution-ideas/**", "storage/**", "hybrid/**",
        ".docutune/**", ".github/**", "*.sh", "Makefile",
    ],
    max_file_size_kb=1000,
    heading_aware_chunking=True,
)

# Azure Cloud Design Patterns - Pattern READMEs only
AZURE_DESIGN_PATTERNS_FILTER = SourceFilterConfig(
    include_paths=["*/README.md"],
    exclude_paths=[
        "*/src/**", "*/.devcontainer/**", "*/.github/**",
        "*/.idea/**", "*/.vscode/**", "*/*.csproj", "*/*.sln",
        "*/pom.xml", "*/build.gradle", "*/package.json",
    ],
    include_extensions=[".md"],
    max_file_size_kb=200,
    heading_aware_chunking=True,
)

DEFAULT_SOURCES = [
    SourceConfig(
        name="cncf-glossary",
        source_type="git",
        url="https://github.com/cncf/glossary",
        provider=Provider.CNCF,
        topic="cloud_native_concepts",
        document_type=DocumentType.GLOSSARY,
        license="Apache-2.0",
        attribution="CNCF",
        filter_config=CNCF_GLOSSARY_FILTER,
    ),
    SourceConfig(
        name="cncf-architecture",
        source_type="git",
        url="https://github.com/cncf/architecture",
        provider=Provider.CNCF,
        topic="cloud_native_architecture",
        document_type=DocumentType.REFERENCE_ARCHITECTURE,
        license="Apache-2.0",
        attribution="CNCF",
        filter_config=CNCF_ARCHITECTURE_FILTER,
    ),
    SourceConfig(
        name="aws-well-architected-labs",
        source_type="git",
        url="https://github.com/awslabs/aws-well-architected-labs",
        provider=Provider.AWS,
        topic="aws_well_architected",
        document_type=DocumentType.LAB,
        license="Apache-2.0",
        attribution="AWS Labs",
        branch="main",
        filter_config=AWS_WELL_ARCHITECTED_FILTER,
    ),
    SourceConfig(
        name="aws-serverless-patterns",
        source_type="git",
        url="https://github.com/aws-samples/serverless-patterns",
        provider=Provider.AWS,
        topic="aws_serverless_patterns",
        document_type=DocumentType.ARCHITECTURE_PATTERN,
        license="MIT-0",
        attribution="AWS Serverless Developer Advocacy",
        filter_config=AWS_SERVERLESS_PATTERNS_FILTER,
    ),
    SourceConfig(
        name="aws-serverless-samples",
        source_type="git",
        url="https://github.com/aws-samples/serverless-samples",
        provider=Provider.AWS,
        topic="aws_architecture_examples",
        document_type=DocumentType.REFERENCE_ARCHITECTURE,
        license="MIT-0",
        attribution="AWS Samples",
        filter_config=AWS_SERVERLESS_SAMPLES_FILTER,
    ),
    SourceConfig(
        name="gcp-architecture-samples",
        source_type="git",
        url="https://github.com/GoogleCloudPlatform/architecture-center-samples",
        provider=Provider.GCP,
        topic="gcp_architecture_patterns",
        document_type=DocumentType.REFERENCE_ARCHITECTURE,
        license="Apache-2.0",
        attribution="Google Cloud",
        filter_config=GCP_ARCHITECTURE_FILTER,
    ),
    SourceConfig(
        name="azure-architecture-center",
        source_type="git",
        url="https://github.com/MicrosoftDocs/architecture-center",
        provider=Provider.AZURE,
        topic="azure_architecture_patterns",
        document_type=DocumentType.REFERENCE_ARCHITECTURE,
        license="MIT",
        attribution="Microsoft Azure",
        filter_config=AZURE_ARCHITECTURE_FILTER,
    ),
    SourceConfig(
        name="azure-cloud-design-patterns",
        source_type="git",
        url="https://github.com/Azure-Samples/cloud-design-patterns",
        provider=Provider.AZURE,
        topic="azure_design_patterns",
        document_type=DocumentType.ARCHITECTURE_PATTERN,
        license="MIT",
        attribution="Azure Patterns & Practices",
        filter_config=AZURE_DESIGN_PATTERNS_FILTER,
    ),
]


def load_source_registry(config_path: Path | None = None) -> list[SourceConfig]:
    if config_path and config_path.exists():
        import yaml
        with open(config_path) as f:
            data = yaml.safe_load(f)
        sources = [SourceConfig(**s) for s in data.get("sources", [])]
        return sources
    return DEFAULT_SOURCES


def get_enabled_sources(sources: list[SourceConfig]) -> list[SourceConfig]:
    return [s for s in sources if s.enabled]