"""
AWS Knowledge Provider - Live AWS knowledge via AWS Agent Toolkit MCP Server.

This provider connects to the AWS Agent Toolkit managed MCP Server to retrieve
live, authoritative AWS documentation and service information.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_architect.services.knowledge.contracts import (
        Evidence,
        EvidenceBundle,
        RetrievalFilters,
        KnowledgeSource,
        KnowledgeSourceType,
        EvidenceAuthority,
        RetrievalFilters,
    )

from ai_architect.services.knowledge.contracts import (
    Evidence,
    EvidenceBundle,
    RetrievalFilters,
    KnowledgeSource,
    KnowledgeSourceType,
    EvidenceAuthority,
    EvidenceBundleBuilder,
    KnowledgeProvider,
)
from ai_architect.services.knowledge.providers.aws_mcp_adapter import AWSMCPAdapter, MCPToolResult


class AWSProvider:
    """
    AWS Live Knowledge Provider.
    
    Connects to AWS Agent Toolkit managed MCP Server to retrieve
    live, authoritative AWS documentation and service information.
    
    This provider:
    - Connects to AWS MCP Server via mcp-proxy-for-aws or OAuth
    - Searches AWS documentation and reads pages
    - Retrieves AWS skills and service information
    - Checks regional availability
    - Returns Evidence with LIVE_PROVIDER authority
    """
    
    def __init__(
        self,
        mcp_endpoint: str = "https://aws-mcp.us-east-1.api.aws/mcp",
        aws_region: str = "us-east-1",
        use_oauth: bool = True,
        proxy_version: str = "latest",
    ):
        self.mcp_endpoint = mcp_endpoint
        self.aws_region = aws_region
        self.use_oauth = use_oauth
        self.proxy_version = proxy_version
        
        self._adapter: Optional[AWSMCPAdapter] = None
        self._initialized = False
        
        # Provider metadata
        self.source_id = "aws-mcp"
        self.source_type = "aws_mcp"
        self.provider = "aws"
        self.name = "AWS Agent Toolkit MCP Server"
        self.authority = "live_provider"
        self.authority_level = 3
        self.is_live = True
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized and self._adapter is not None and self._adapter.is_connected
    
    def _get_adapter(self) -> "AWSMCPAdapter":
        """Lazy initialization of the MCP adapter."""
        if self._adapter is None:
            self._adapter = AWSMCPAdapter(
                mcp_endpoint=self.mcp_endpoint,
                aws_region=self.aws_region,
            )
        return self._adapter
    
    async def initialize(self) -> bool:
        """Initialize the connection to AWS MCP Server."""
        try:
            self._adapter = AWSMCPAdapter(
                mcp_endpoint=self.mcp_endpoint,
                aws_region=self.aws_region,
            )
            await self._adapter.connect()
            self._initialized = True
            return True
        except Exception as e:
            self._initialized = False
            raise ConnectionError(f"Failed to initialize AWS Provider: {e}")
    
    async def _ensure_connected(self):
        """Ensure the adapter is connected."""
        if not self._initialized:
            await self.initialize()
    
    def _tool_result_to_evidence(
        self,
        result: "MCPToolResult",
        query: str,
        source_type: str = "live_provider",
    ) -> "Evidence":
        """Convert an MCP tool result to an Evidence object."""
        from ai_architect.services.knowledge.contracts import Evidence, EvidenceAuthority
        from datetime import datetime
        import json
        
        content = ""
        metadata = {"tool": result.tool_name, "success": result.success}
        
        if result.success and result.result:
            # Convert result to string content
            if isinstance(result.result, str):
                content = result.result
            elif isinstance(result.result, dict):
                content = json.dumps(result.result, indent=2)
            else:
                content = str(result.result)
            metadata["mcp_result"] = result.result
        else:
            content = f"Error: {result.error}"
        
        return Evidence(
            evidence_id=f"aws-mcp:{result.tool_name}:{datetime.utcnow().timestamp()}",
            content=content,
            source="aws-mcp",
            source_type="live_provider",
            provider="aws",
            document_id=f"{result.tool_name}:{hash(str(result.result))}" if result.success else f"error:{result.tool_name}",
            title=f"AWS MCP: {result.tool_name}",
            section_path="",
            authority="live_provider",
            authority_level=3,  # Highest authority
            provider_specific=True,
            score=1.0 if result.success else 0.0,
            retrieval_time=datetime.utcnow(),
            source_retrieved_at=None,
            version="",
            url="",
            metadata={
                "tool": result.tool_name,
                "success": result.success,
                "error": result.error,
                "raw_result": result.result if result.success else None,
            },
        )
    
    async def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional["RetrievalFilters"] = None,
    ) -> List["Evidence"]:
        """Main retrieve method implementing KnowledgeProvider interface."""
        from ai_architect.services.knowledge.contracts import RetrievalFilters
        
        await self._ensure_connected()
        
        # Apply provider filter
        if filters and filters.provider and filters.provider.lower() != "aws":
            return []  # This provider only serves AWS
        
        return await self._retrieve_internal(query, top_k, filters)
    
    async def _retrieve_internal(
        self,
        query: str,
        top_k: int,
        filters: Optional["RetrievalFilters"] = None,
    ) -> List["Evidence"]:
        """Internal retrieval implementation."""
        await self._ensure_connected()
        adapter = self._get_adapter()
        
        evidence_list = []
        
        # Step 1: Search documentation
        search_result = await self._adapter.search_documentation(query)
        if search_result.success:
            evidence = self._tool_result_to_evidence(search_result, query)
            evidence.source = "aws-mcp-search"
            # Limit content length
            if len(evidence.content) > 5000:
                evidence.content = evidence.content[:5000] + "... [truncated]"
            evidence_list.append(evidence)
        
        # Step 2: Check for relevant skills
        skills_result = await self._adapter.search_skills(query)
        if skills_result.success:
            evidence = self._tool_result_to_evidence(skills_result, query)
            evidence.source = "aws-mcp-skills"
            if len(evidence.content) > 5000:
                evidence.content = evidence.content[:5000] + "... [truncated]"
            evidence_list.append(evidence)
        
        # Step 3: Check regional availability for services mentioned in query
        import re
        aws_services = [
            "EC2", "S3", "Lambda", "DynamoDB", "RDS", "SQS", "SNS", 
            "EventBridge", "Step Functions", "API Gateway", "CloudFront",
            "CloudWatch", "CloudTrail", "IAM", "KMS", "Secrets Manager",
            "Parameter Store", "SSM", "ECS", "EKS", "Fargate", "App Runner",
            "CloudFront Functions", "ElastiCache", "MemoryDB", "DocumentDB",
            "Neptune", "Redshift", "Athena", "EMR", "Glue", "Kinesis",
            "MSK", "SNS", "SQS", "ElastiCache", "MemoryDB", "DocumentDB",
            "Neptune", "Redshift", "Athena", "EMR", "Glue", "Kinesis",
            "MSK", "SageMaker", "Bedrock", "SageMaker", "OpenSearch",
            "S3 Vectors", "Aurora DSQL", "Bedrock AgentCore"
        ]
        
        mentioned_services = set()
        query_upper = query.upper()
        for service in aws_services:
            if service.upper() in query_upper:
                mentioned_services.add(service)
        
        for service in mentioned_services:
            avail_result = await self._adapter.get_regional_availability(service)
            if avail_result.success:
                evidence = self._tool_result_to_evidence(avail_result, query)
                evidence.source = "aws-mcp-availability"
                evidence.metadata["service"] = service
                evidence_list.append(evidence)
        
        # Limit results
        return evidence_list[:max(1, top_k)]
    
    async def _ensure_connected(self):
        if not self._initialized:
            await self.initialize()
    
    async def initialize(self) -> bool:
        """Initialize the connection to AWS MCP Server."""
        try:
            self._adapter = AWSMCPAdapter(
                mcp_endpoint=self.mcp_endpoint,
                aws_region=self.aws_region,
            )
            await self._adapter.connect()
            self._initialized = True
            return True
        except Exception as e:
            self._initialized = False
            raise ConnectionError(f"Failed to initialize AWS Provider: {e}")
    
    def is_available(self) -> bool:
        try:
            return self._initialized and self._adapter is not None and self._adapter.is_connected
        except Exception:
            return False
    
    def get_source_info(self):
        from ai_architect.services.knowledge.contracts import KnowledgeSource, KnowledgeSourceType
        return KnowledgeSource(
            source_id=self.source_id,
            source_type="aws_mcp",
            provider="aws",
            name=self.name,
            authority="live_provider",
            is_live=True,
            metadata={
                "mcp_endpoint": self.mcp_endpoint,
                "aws_region": self.aws_region,
            },
        )